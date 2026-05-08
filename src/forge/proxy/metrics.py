"""In-memory per-proxy runtime metrics.

Each proxy process maintains a single ProxyMetrics instance that accumulates
request counts, token usage (including cached and failed), and latency.
Metrics reset on proxy restart — this is expected and correct since each
proxy is a separate subprocess.

Exposed via GET / (runtime truth endpoint) and ``forge proxy metrics`` CLI.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from forge.core.state import now_iso


@dataclass
class TierTokens:
    """Per-tier (or per-model) token breakdown with latency tracking."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_latency_ms: float = 0.0
    request_count: int = 0  # for avg latency (separate from requests_by_tier for reset clarity)
    estimated_cost_micros: int = 0  # microdollars (1 USD = 1_000_000)

    def to_dict(self) -> dict[str, object]:
        avg = round(self.total_latency_ms / self.request_count, 1) if self.request_count > 0 else 0.0
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "avg_latency_ms": avg,
            "estimated_cost_usd": round(self.estimated_cost_micros / 1_000_000, 6),
            "estimated_cost_micros": self.estimated_cost_micros,
        }


@dataclass
class ProxyMetrics:
    """Thread-safe in-memory metrics for a single proxy process.

    All counter updates go through ``record_request()`` under a single lock.
    The lock hold time is microseconds (dict increments only), so contention
    with uvicorn's async event loop or thread pool workers is negligible.
    """

    # Timestamps
    started_at: str = field(default_factory=now_iso)
    _started_mono: float = field(default_factory=time.monotonic)

    # Counters
    total_requests: int = 0
    total_streaming: int = 0
    total_failures: int = 0

    # Token accounting (success + failure)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0

    # Failed request tokens (wasted spend)
    failed_input_tokens: int = 0
    failed_output_tokens: int = 0

    # Cost estimates (microdollars, 1 USD = 1_000_000)
    total_cost_micros: int = 0
    failed_cost_micros: int = 0

    # Per-tier breakdown
    requests_by_tier: dict[str, int] = field(default_factory=dict)
    tokens_by_tier: dict[str, TierTokens] = field(default_factory=dict)

    # Per-model breakdown (actual_model_id, for cost comparison)
    requests_by_model: dict[str, int] = field(default_factory=dict)
    tokens_by_model: dict[str, TierTokens] = field(default_factory=dict)

    # Failure classification (error_type, not HTTP status — streaming is always 200)
    failures_by_type: dict[str, int] = field(default_factory=dict)

    # Activity
    last_request_at: str | None = None

    # Lock
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request(
        self,
        *,
        tier: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        latency_ms: float,
        streaming: bool,
        failed: bool,
        error_type: str | None = None,
        cost_micros: int = 0,
    ) -> None:
        """Record a completed request. All fields updated atomically under lock."""
        with self._lock:
            self.total_requests += 1
            if streaming:
                self.total_streaming += 1

            # Tokens (always, success + failure)
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_cached_tokens += cached_tokens

            # Cost
            self.total_cost_micros += cost_micros

            # Per-tier
            self.requests_by_tier[tier] = self.requests_by_tier.get(tier, 0) + 1
            tier_tokens = self.tokens_by_tier.get(tier)
            if tier_tokens is None:
                tier_tokens = TierTokens()
                self.tokens_by_tier[tier] = tier_tokens
            tier_tokens.input_tokens += input_tokens
            tier_tokens.output_tokens += output_tokens
            tier_tokens.cached_tokens += cached_tokens
            tier_tokens.total_latency_ms += latency_ms
            tier_tokens.request_count += 1
            tier_tokens.estimated_cost_micros += cost_micros

            # Per-model
            self.requests_by_model[model] = self.requests_by_model.get(model, 0) + 1
            model_tokens = self.tokens_by_model.get(model)
            if model_tokens is None:
                model_tokens = TierTokens()
                self.tokens_by_model[model] = model_tokens
            model_tokens.input_tokens += input_tokens
            model_tokens.output_tokens += output_tokens
            model_tokens.cached_tokens += cached_tokens
            model_tokens.total_latency_ms += latency_ms
            model_tokens.request_count += 1
            model_tokens.estimated_cost_micros += cost_micros

            # Failures
            if failed:
                self.total_failures += 1
                self.failed_input_tokens += input_tokens
                self.failed_output_tokens += output_tokens
                self.failed_cost_micros += cost_micros
                if error_type:
                    self.failures_by_type[error_type] = self.failures_by_type.get(error_type, 0) + 1

            # Activity
            self.last_request_at = now_iso()

    def snapshot(self) -> dict:
        """Return a JSON-serializable dict of all metrics plus derived values."""
        with self._lock:
            total = self.total_requests
            uptime = time.monotonic() - self._started_mono

            return {
                "started_at": self.started_at,
                "uptime_seconds": round(uptime, 1),
                "total_requests": total,
                "total_streaming": self.total_streaming,
                "total_failures": self.total_failures,
                "tokens": {
                    "input": self.total_input_tokens,
                    "output": self.total_output_tokens,
                    "cached": self.total_cached_tokens,
                    "failed_input": self.failed_input_tokens,
                    "failed_output": self.failed_output_tokens,
                },
                "cache_hit_rate": (
                    round(self.total_cached_tokens / self.total_input_tokens * 100, 1)
                    if self.total_input_tokens > 0
                    else 0.0
                ),
                "by_tier": {
                    tier: {"requests": self.requests_by_tier.get(tier, 0), **tokens.to_dict()}
                    for tier, tokens in self.tokens_by_tier.items()
                },
                "by_model": {
                    model: {"requests": self.requests_by_model.get(model, 0), **tokens.to_dict()}
                    for model, tokens in self.tokens_by_model.items()
                },
                "failures_by_type": dict(self.failures_by_type),
                "costs": {
                    "total_usd": round(self.total_cost_micros / 1_000_000, 6),
                    "failed_usd": round(self.failed_cost_micros / 1_000_000, 6),
                    "total_micros": self.total_cost_micros,
                    "failed_micros": self.failed_cost_micros,
                },
                "last_request_at": self.last_request_at,
            }

    def reset(self) -> None:
        """Zero all counters. Preserves started_at for uptime. For test isolation."""
        with self._lock:
            self.total_requests = 0
            self.total_streaming = 0
            self.total_failures = 0
            self.total_input_tokens = 0
            self.total_output_tokens = 0
            self.total_cached_tokens = 0
            self.failed_input_tokens = 0
            self.failed_output_tokens = 0
            self.total_cost_micros = 0
            self.failed_cost_micros = 0
            self.requests_by_tier.clear()
            self.tokens_by_tier.clear()
            self.requests_by_model.clear()
            self.tokens_by_model.clear()
            self.failures_by_type.clear()
            self.last_request_at = None


# Module-level singleton — one per proxy process.
# Matches existing patterns: client_factory and PROXY_ID in server.py are also module globals.
proxy_metrics = ProxyMetrics()
