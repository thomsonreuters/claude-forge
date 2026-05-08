"""Verb-level cost attribution via proxy metric snapshot deltas.

Wraps subprocess invocations (panel, supervisor, handoff, etc.) to
measure cost by snapshotting proxy metrics before and after execution.
Results are logged to PID-sharded verb JSONL files.

All verb costs are marked ``estimated`` because concurrent proxy traffic
(e.g., the main interactive session) may share the same proxy during
the measurement window.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.core.paths import get_forge_home

logger = logging.getLogger(__name__)

_verb_lock = threading.Lock()


@dataclass
class ProxyCostDelta:
    """Cost delta for a single proxy between two snapshots."""

    base_url: str
    cost_micros: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    request_count: int = 0


@dataclass
class VerbCostResult:
    """Aggregated cost attribution for one verb invocation."""

    verb: str
    total_cost_micros: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    request_count: int = 0
    duration_ms: float = 0.0
    estimated: bool = True
    per_proxy: list[ProxyCostDelta] = field(default_factory=list)


def _fetch_snapshot(base_url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """Fetch proxy metrics via GET /. Returns None on failure."""
    try:
        normalized = base_url if "://" in base_url else f"http://{base_url}"
        url = normalized.rstrip("/") + "/"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if data.get("is_proxy") and "metrics" in data:
            return data["metrics"]
    except Exception as e:
        logger.debug("Failed to fetch proxy snapshot from %s: %s", base_url, e)
    return None


def _compute_delta(before: dict[str, Any], after: dict[str, Any], base_url: str) -> ProxyCostDelta:
    """Compute the difference between two proxy metric snapshots."""
    b_tokens = before.get("tokens", {})
    a_tokens = after.get("tokens", {})
    b_costs = before.get("costs", {})
    a_costs = after.get("costs", {})

    return ProxyCostDelta(
        base_url=base_url,
        cost_micros=a_costs.get("total_micros", 0) - b_costs.get("total_micros", 0),
        input_tokens=a_tokens.get("input", 0) - b_tokens.get("input", 0),
        output_tokens=a_tokens.get("output", 0) - b_tokens.get("output", 0),
        cached_tokens=a_tokens.get("cached", 0) - b_tokens.get("cached", 0),
        request_count=after.get("total_requests", 0) - before.get("total_requests", 0),
    )


def _verb_log_dir() -> Path:
    return get_forge_home() / "costs" / "verbs"


def _log_verb_cost(result: VerbCostResult) -> None:
    """Append a verb cost record to the PID-sharded JSONL log."""
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verb": result.verb,
        "total_cost_micros": result.total_cost_micros,
        "estimated": result.estimated,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cached_tokens": result.cached_tokens,
        "request_count": result.request_count,
        "duration_ms": round(result.duration_ms, 1),
        "per_proxy": [asdict(p) for p in result.per_proxy],
    }

    try:
        log_dir = _verb_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        path = log_dir / f"{month}_{os.getpid()}.jsonl"

        with _verb_lock:
            with open(path, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning("Failed to write verb cost log: %s", e)


def resolve_subprocess_proxy_url() -> str | None:
    """Resolve the current FORGE_SUBPROCESS_PROXY to a base URL, if configured."""
    from forge.core.reactive.env import FORGE_SUBPROCESS_PROXY_VAR
    from forge.core.reactive.proxy import lookup_proxy_base_url

    proxy = os.environ.get(FORGE_SUBPROCESS_PROXY_VAR)
    if not proxy:
        return None

    try:
        return lookup_proxy_base_url(proxy)
    except Exception:
        return None


def resolve_proxy_urls(specs: list[Any]) -> list[str]:
    """Extract unique proxy base URLs from a list of ModelSpecs.

    For specs with no explicit proxy, falls back to FORGE_SUBPROCESS_PROXY
    when configured.
    Deduplicates by resolved URL.
    """
    from forge.core.reactive.env import FORGE_SUBPROCESS_PROXY_VAR
    from forge.core.reactive.proxy import lookup_proxy_base_url

    subprocess_proxy = os.environ.get(FORGE_SUBPROCESS_PROXY_VAR)
    seen: set[str] = set()
    urls: list[str] = []
    for spec in specs:
        proxy = getattr(spec, "proxy", None) or subprocess_proxy
        if not proxy:
            continue
        try:
            url = lookup_proxy_base_url(proxy)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        except Exception:
            pass
    return urls


@contextmanager
def track_verb_cost(verb: str, proxy_base_urls: list[str]):
    """Snapshot proxy metrics across all proxies before/after a verb invocation.

    Args:
        verb: Origin label ("panel", "supervisor", "handoff", etc.)
        proxy_base_urls: ALL proxy base URLs this verb will use.
            Direct workers (no proxy) are excluded — only proxied
            requests have cost data at the proxy level.

    Yields control to the caller. On exit, computes snapshot deltas,
    logs the verb cost record, and discards. The caller does not
    receive the result (it's fire-and-forget for the log).
    """
    unique_urls = list(dict.fromkeys(u for u in proxy_base_urls if u))

    if not unique_urls:
        yield
        return

    snapshots_before: dict[str, dict[str, Any]] = {}
    for url in unique_urls:
        snap = _fetch_snapshot(url)
        if snap is not None:
            snapshots_before[url] = snap

    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = (time.monotonic() - start) * 1000

        try:
            deltas: list[ProxyCostDelta] = []
            for url in unique_urls:
                if url not in snapshots_before:
                    continue
                after = _fetch_snapshot(url)
                if after is None:
                    continue
                deltas.append(_compute_delta(snapshots_before[url], after, url))

            total_cost = sum(d.cost_micros for d in deltas)
            total_input = sum(d.input_tokens for d in deltas)
            total_output = sum(d.output_tokens for d in deltas)
            total_cached = sum(d.cached_tokens for d in deltas)
            total_requests = sum(d.request_count for d in deltas)

            result = VerbCostResult(
                verb=verb,
                total_cost_micros=total_cost,
                input_tokens=total_input,
                output_tokens=total_output,
                cached_tokens=total_cached,
                request_count=total_requests,
                duration_ms=elapsed,
                estimated=True,
                per_proxy=deltas,
            )
            _log_verb_cost(result)
        except Exception as e:
            logger.warning("Failed to track verb cost for %s: %s", verb, e)


def read_verb_logs(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read and aggregate verb cost records from all PID shards."""
    log_dir = _verb_log_dir()
    if not log_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.jsonl")):
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
                            ts = datetime.fromisoformat(ts_str.rstrip("Z").removesuffix("+00:00") + "+00:00")
                        except (ValueError, TypeError):
                            continue
                        if period_start and ts < period_start:
                            continue
                        if period_end and ts >= period_end:
                            continue

                    records.append(record)
        except OSError as e:
            logger.warning("Failed to read verb log %s: %s", path, e)

    records.sort(key=lambda r: r.get("ts", ""))
    return records
