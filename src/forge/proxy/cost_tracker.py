"""Spend cap enforcement with JSONL-bootstrapped tracking.

On proxy startup, reads the current (and previous) month's cost JSONL
logs to initialize in-memory spend counters. Caps are enforced per
request via check_cap().

Two enforcement modes:
  post   -- block once accumulated spend already exceeds the cap
  strict -- estimate incoming request cost and block if projected total exceeds
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_MICROS_PER_DOLLAR = 1_000_000
_24H_SECONDS = 86400


@dataclass
class CapResult:
    """Result of a spend cap check."""

    exceeded: bool
    cap_type: str | None = None  # "daily" or "monthly"
    current_micros: int = 0
    limit_micros: int = 0
    projected: bool = False  # True if this is a pre-flight estimate


class CostTracker:
    """In-memory spend tracking with cap enforcement.

    Thread-safe via the proxy's single-threaded async event loop
    (all calls happen on the main thread in FastAPI/uvicorn).
    """

    def __init__(
        self,
        *,
        daily_cap_usd: float | None = None,
        monthly_cap_usd: float | None = None,
        cap_mode: str = "post",
        on_cap_hit: str = "reject",
    ) -> None:
        self.daily_cap_micros = int(daily_cap_usd * _MICROS_PER_DOLLAR) if daily_cap_usd is not None else None
        self.monthly_cap_micros = int(monthly_cap_usd * _MICROS_PER_DOLLAR) if monthly_cap_usd is not None else None
        self.cap_mode = cap_mode
        self.on_cap_hit = on_cap_hit

        self._daily_window: deque[tuple[float, int]] = deque()
        self._monthly_total: int = 0
        self._monthly_key: str = ""

    @property
    def has_caps(self) -> bool:
        return self.daily_cap_micros is not None or self.monthly_cap_micros is not None

    def bootstrap_from_logs(self, log_dir: Path) -> None:
        """Read existing cost logs to initialize spend counters.

        Reads current month + previous month (for rolling 24h window
        at month boundaries). Scans all PID shards.
        """
        if not log_dir.is_dir():
            return

        now = datetime.now(timezone.utc)
        current_month = now.strftime("%Y-%m")
        self._monthly_key = current_month

        if now.month == 1:
            prev_month = f"{now.year - 1}-12"
        else:
            prev_month = f"{now.year}-{now.month - 1:02d}"

        cutoff = time.time() - _24H_SECONDS

        for path in sorted(log_dir.glob("*.jsonl")):
            fname = path.stem  # e.g., "2026-05_12345"
            file_month = fname.split("_")[0] if "_" in fname else fname

            if file_month not in (current_month, prev_month):
                continue

            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = self._parse_record(line)
                        except Exception:
                            continue
                        if record is None:
                            continue

                        ts_unix, cost_micros, record_month = record

                        if record_month == current_month:
                            self._monthly_total += cost_micros

                        if ts_unix >= cutoff:
                            self._daily_window.append((ts_unix, cost_micros))
            except OSError as e:
                logger.warning("Failed to read cost log %s: %s", path, e)

        daily_total = sum(c for _, c in self._daily_window)
        logger.info(
            "Cost tracker bootstrapped: daily=$%.2f, monthly=$%.2f (%d records in window)",
            daily_total / _MICROS_PER_DOLLAR,
            self._monthly_total / _MICROS_PER_DOLLAR,
            len(self._daily_window),
        )

    @staticmethod
    def _parse_record(line: str) -> tuple[float, int, str] | None:
        """Parse a JSONL line into (unix_timestamp, cost_micros, month_key)."""
        import json

        data = json.loads(line)
        ts_str = data.get("ts", "")
        cost_micros = int(data.get("cost_micros", 0))
        if cost_micros <= 0:
            return None

        try:
            ts = datetime.fromisoformat(ts_str.rstrip("Z").removesuffix("+00:00") + "+00:00")
        except (ValueError, TypeError):
            return None

        month_key = ts.strftime("%Y-%m")
        return ts.timestamp(), cost_micros, month_key

    def record(self, cost_micros: int) -> None:
        """Record a completed request's cost."""
        if cost_micros <= 0:
            return

        now = time.time()
        self._roll_month_if_needed()

        self._monthly_total += cost_micros
        self._daily_window.append((now, cost_micros))

    def _roll_month_if_needed(self) -> None:
        """Reset the calendar-month accumulator when UTC month changes."""
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        if current_month != self._monthly_key:
            self._monthly_total = 0
            self._monthly_key = current_month

    def _prune_daily_window(self) -> None:
        """Remove entries older than 24 hours from the rolling window."""
        cutoff = time.time() - _24H_SECONDS
        while self._daily_window and self._daily_window[0][0] < cutoff:
            self._daily_window.popleft()

    def daily_spend_micros(self) -> int:
        """Current rolling 24h spend in microdollars."""
        self._prune_daily_window()
        return sum(c for _, c in self._daily_window)

    def monthly_spend_micros(self) -> int:
        """Current calendar month spend in microdollars."""
        self._roll_month_if_needed()
        return self._monthly_total

    def check_cap(self, projected_cost_micros: int = 0) -> CapResult:
        """Check if spend would exceed any configured caps.

        Args:
            projected_cost_micros: Estimated cost of the pending request.
                In strict mode, added to current spend for pre-flight check.
                In post mode, ignored (only accumulated spend matters).

        Returns:
            CapResult indicating whether any cap is exceeded.
        """
        if not self.has_caps:
            return CapResult(exceeded=False)

        extra = projected_cost_micros if self.cap_mode == "strict" else 0

        if self.daily_cap_micros is not None:
            daily = self.daily_spend_micros() + extra
            if daily >= self.daily_cap_micros:
                return CapResult(
                    exceeded=True,
                    cap_type="daily",
                    current_micros=daily,
                    limit_micros=self.daily_cap_micros,
                    projected=extra > 0,
                )

        if self.monthly_cap_micros is not None:
            monthly = self.monthly_spend_micros() + extra
            if monthly >= self.monthly_cap_micros:
                return CapResult(
                    exceeded=True,
                    cap_type="monthly",
                    current_micros=monthly,
                    limit_micros=self.monthly_cap_micros,
                    projected=extra > 0,
                )

        return CapResult(exceeded=False)

    def cap_summary(self) -> dict[str, dict[str, float]]:
        """Return current spend vs caps for CLI display."""
        result: dict[str, dict[str, float]] = {}
        if self.daily_cap_micros is not None:
            daily = self.daily_spend_micros()
            result["daily"] = {
                "current_usd": daily / _MICROS_PER_DOLLAR,
                "limit_usd": self.daily_cap_micros / _MICROS_PER_DOLLAR,
                "percent": round(daily / self.daily_cap_micros * 100, 1) if self.daily_cap_micros > 0 else 0,
            }
        if self.monthly_cap_micros is not None:
            monthly = self.monthly_spend_micros()
            result["monthly"] = {
                "current_usd": monthly / _MICROS_PER_DOLLAR,
                "limit_usd": self.monthly_cap_micros / _MICROS_PER_DOLLAR,
                "percent": round(monthly / self.monthly_cap_micros * 100, 1) if self.monthly_cap_micros > 0 else 0,
            }
        return result
