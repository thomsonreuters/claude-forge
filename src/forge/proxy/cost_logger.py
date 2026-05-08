"""PID-sharded JSONL cost log writer.

Each proxy process writes to its own shard file to avoid interprocess
locking. The CLI aggregates across shards at query time.

Location: ~/.forge/costs/requests/YYYY-MM_<pid>.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.core.paths import get_forge_home

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _pid_suffix() -> str:
    return str(os.getpid())


def _costs_dir() -> Path:
    return get_forge_home() / "costs" / "requests"


def _current_log_path() -> Path:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return _costs_dir() / f"{month}_{_pid_suffix()}.jsonl"


def log_request_cost(
    *,
    proxy_id: str,
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_micros: int,
    latency_ms: float,
    failed: bool,
    request_id: str,
    pricing_source: str = "catalog",
) -> None:
    """Append a cost record to the PID-sharded JSONL log.

    Best-effort: write failures are logged but never block the request.
    """
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy_id": proxy_id,
        "model": model,
        "tier": tier,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "cost_micros": cost_micros,
        "estimated": True,
        "pricing_source": pricing_source,
        "latency_ms": round(latency_ms, 1),
        "failed": failed,
        "request_id": request_id,
    }

    try:
        log_path = _current_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with _lock:
            with open(log_path, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning("Failed to write cost log: %s", e)


def read_cost_logs(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read and aggregate cost records from all PID shards.

    Args:
        period_start: Only include records at or after this time (UTC).
        period_end: Only include records before this time (UTC).

    Returns:
        List of cost record dicts, sorted by timestamp.
    """
    costs_dir = _costs_dir()
    if not costs_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(costs_dir.glob("*.jsonl")):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if period_start or period_end:
                        ts_str = record.get("ts", "")
                        try:
                            ts = datetime.fromisoformat(
                                ts_str.rstrip("Z").removesuffix("+00:00") + "+00:00"
                            )
                        except (ValueError, TypeError):
                            continue
                        if period_start and ts < period_start:
                            continue
                        if period_end and ts >= period_end:
                            continue

                    records.append(record)
        except OSError as e:
            logger.warning("Failed to read cost log %s: %s", path, e)

    records.sort(key=lambda r: r.get("ts", ""))
    return records
