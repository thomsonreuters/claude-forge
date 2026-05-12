"""
Unified LLM Proxy Server - Anthropic-compatible API for multiple providers.

This FastAPI server provides an Anthropic Messages API-compatible interface for
LLM providers via LiteLLM.

The server uses a unified client architecture where provider-specific logic is
encapsulated in client implementations that inherit from AbstractLLMClient.
This design ensures consistent behavior across providers while keeping the
server code clean and maintainable.

Key endpoints:
- POST /v1/messages - Main chat completion endpoint (streaming/non-streaming)
- POST /v1/messages/count_tokens - Token counting endpoint
- GET / - Health check and service information

For detailed API documentation, architecture overview, and configuration options,
see README.md in the project root.
"""

import asyncio
import logging
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import click
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from forge.config import TierOverride, config, init_config, reload
from forge.core.llm.errors import AuthenticationError
from forge.core.logging import (
    configure_console_logging,
    configure_debug_logging,
    get_effective_log_level,
)
from forge.proxy.base_client import ProxyStreamError, ToolCallError
from forge.proxy.client_factory import TierClientFactory
from forge.proxy.converters import (
    convert_anthropic_to_openai,
    convert_openai_to_anthropic,
    convert_openai_to_anthropic_sse,
)
from forge.proxy.cost_logger import log_request_cost
from forge.proxy.cost_tracker import CostTracker
from forge.proxy.data_models import (
    MessagesRequest,
    TokenCountRequest,
    TokenCountResponse,
    map_model_name,
)
from forge.proxy.error_hints import enrich_error_content
from forge.proxy.metrics import proxy_metrics
from forge.proxy.utils import (
    log_request_beautifully,
    log_request_response,
    log_tool_event,
    log_tool_failure,
)

logger = logging.getLogger(__name__)

logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

client_factory = TierClientFactory()

PREFERRED_PROVIDER = None

# When a proxy is started under a proxy id, its config should be stable for the
# lifetime of the process (no hot reload).
PROXY_ID: str | None = os.environ.get("FORGE_PROXY_ID")

cost_tracker: CostTracker | None = None


def _initialize_cost_tracker_from_config() -> CostTracker:
    """Initialize request cost tracking in the module serving FastAPI traffic.

    ``python -m forge.proxy.server`` executes this file as ``__main__``, while
    uvicorn imports ``forge.proxy.server:app`` for request handling. Module
    globals therefore need to be initialized in the imported app module too.
    """
    global cost_tracker
    if cost_tracker is not None:
        return cost_tracker

    from forge.config.schema import CostConfig

    cost_cfg = getattr(config.proxy, "costs", None) or CostConfig()
    if cost_cfg.caps.per_day is not None or cost_cfg.caps.per_month is not None:
        from forge.core.paths import get_forge_home

        cost_tracker = CostTracker(
            daily_cap_usd=cost_cfg.caps.per_day,
            monthly_cap_usd=cost_cfg.caps.per_month,
            cap_mode=cost_cfg.cap_mode,
            on_cap_hit=cost_cfg.on_cap_hit,
        )
        cost_tracker.bootstrap_from_logs(get_forge_home() / "costs" / "requests")
    else:
        cost_tracker = CostTracker()
    return cost_tracker


def _ensure_runtime_state() -> None:
    """Ensure the imported app module has proxy config and runtime trackers."""
    if PROXY_ID is None:
        reload()
    elif not config.proxy.active_template:
        reload(proxy_id=PROXY_ID)

    _initialize_cost_tracker_from_config()


def _calc_and_log_cost(
    *,
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    latency_ms: float,
    failed: bool,
    request_id: str,
) -> int:
    """Calculate cost in microdollars and write to the persistent cost log.

    Best-effort: pricing/logging failures return 0 cost and warn.
    Never raises — cost tracking must not break the proxy request path.
    """
    try:
        from forge.core.models.pricing import calculate_cost, get_pricing

        cost_micros = calculate_cost(model, input_tokens, output_tokens, cached_tokens)
        pricing = get_pricing(model)

        log_request_cost(
            proxy_id=PROXY_ID or "unknown",
            model=model,
            tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_micros=cost_micros,
            latency_ms=latency_ms,
            failed=failed,
            request_id=request_id,
            pricing_source=pricing.source,
        )

        if cost_tracker is not None:
            cost_tracker.record(cost_micros)

        return cost_micros
    except Exception as e:
        logger.warning("Cost calculation failed (non-fatal): %s", e)
        return 0


def _cap_result_message(cap_result) -> str:
    """Format a spend cap result for HTTP headers and errors."""
    cap_type = cap_result.cap_type or "configured"
    return (
        f"{'Projected ' if cap_result.projected else ''}"
        f"{cap_type} spend cap reached: "
        f"${cap_result.current_micros / 1_000_000:.2f} / "
        f"${cap_result.limit_micros / 1_000_000:.2f}. "
        f"Adjust with: forge proxy set <id> costs.caps.per_{cap_type}=<amount>"
    )


def _with_spend_warning(headers: dict[str, str], warning: str | None) -> dict[str, str]:
    """Attach the optional spend warning header to a response header dict."""
    if warning:
        headers["X-Spend-Warning"] = warning
    return headers


def _textish_chars(value: object) -> int:
    """Approximate text-bearing request payload size for strict cap preflight."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        total = 0
        for key in ("content", "text", "thinking", "input", "name", "description"):
            if key in value:
                total += _textish_chars(value[key])
        return total
    if isinstance(value, (list, tuple)):
        return sum(_textish_chars(item) for item in value)

    total = 0
    for attr in ("content", "text", "thinking", "input", "name", "description"):
        if hasattr(value, attr):
            total += _textish_chars(getattr(value, attr))
    return total


def _estimate_input_tokens(request_data: MessagesRequest) -> int:
    """Approximate request input tokens for strict cap preflight."""
    chars = _textish_chars(getattr(request_data, "system", None))
    chars += _textish_chars(getattr(request_data, "messages", None))
    chars += _textish_chars(getattr(request_data, "tools", None))
    return chars // 4


def _get_tier_override(tier: str) -> TierOverride | None:
    """Get tier override from the active provider config.

    Returns the TierOverride for the specified tier, or None if not configured.
    Tier overrides allow per-tier hyperparameter customization (e.g., different
    reasoning_effort for opus vs sonnet when both map to the same model).
    """
    try:
        provider_cfg = config.proxy.get_provider()
        return provider_cfg.tier_overrides.get(tier)
    except Exception:
        return None


def _resolve_model_with_alternatives(tier: str, original_model_name: str | None, fallback_model: str) -> str:
    """Resolve backend model, checking per-tier alternatives before the tier default.

    Used by both message routing and token counting so model resolution is
    consistent across both paths.  Strips ``[1m]`` context-window suffix before
    lookup since it is a Claude Code hint, not a routing decision.
    """
    try:
        provider_cfg = config.proxy.get_provider()
        alt_models = provider_cfg.model_alternatives.get(tier, {})
        if original_model_name and alt_models:
            lookup = original_model_name.removesuffix("[1m]")
            if lookup in alt_models:
                return alt_models[lookup]
    except Exception:
        # Best-effort: degrade to fallback_model if provider config is unavailable
        logger.debug("model_alternatives lookup failed, using tier default", exc_info=True)
    return fallback_model


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan management."""
    logger.info("Server started...")
    yield
    logger.info("Server is shutting down... Cleaning up resources")


app = FastAPI(title="Unified LLM Proxy", lifespan=lifespan)


# --- Thinking → reasoning_effort translation ---
# Claude Code sends Anthropic-specific `thinking` config; litellm uses
# `reasoning_effort` which it translates per provider (Gemini 3: thinking_level,
# Gemini 2.5: thinkingBudget). These helpers map between the two.

# Ordered from lowest to highest so we can compare with max().
_EFFORT_RANK: dict[str | None, int] = {
    None: -1,
    "none": 0,
    "disable": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
}

# Budget thresholds for ceil-to-tier mapping (never downgrade).
# Checked top-down; first match wins.  LiteLLM internal budgets for
# reference: low ~ 1,024, medium ~ 8,192, high ~ 24,576.
_BUDGET_THRESHOLDS: list[tuple[int, str]] = [
    (25_000, "xhigh"),  # >=25k tokens -> xhigh (above litellm high)
    (10_000, "high"),  # >=10k tokens -> high
    (2_000, "medium"),  # >=2k tokens  -> medium
    (500, "low"),  # >=500 tokens -> low
    (1, "minimal"),  # >=1 token    -> minimal
]

# Type-based fallback when budget_tokens is absent.
_TYPE_TO_EFFORT: dict[str, str] = {
    "enabled": "high",
    "adaptive": "medium",
    "disabled": "none",
}


def _derive_reasoning_effort(thinking: dict[str, object] | object | None) -> str | None:
    """Derive reasoning_effort from Claude Code's thinking config.

    Priority: budget_tokens (numeric, precise) > type (semantic label).
    Unknown types default to "medium" (safe — never results in no reasoning).
    """
    if not isinstance(thinking, dict):
        return None

    # 1) Use budget_tokens if present — data-driven, not label-driven.
    budget = thinking.get("budget_tokens")
    if isinstance(budget, (int, float)) and budget > 0:
        for threshold, effort in _BUDGET_THRESHOLDS:
            if budget >= threshold:
                return effort
        return "minimal"  # budget_tokens in (0, 1) — fractional edge case

    # 2) Fall back to type-based mapping.
    thinking_type = thinking.get("type")
    if isinstance(thinking_type, str):
        mapped: str | None = _TYPE_TO_EFFORT.get(thinking_type)
        if mapped is not None:
            return mapped
        # Unknown type — default to medium (safe), log warning.
        logger.warning(
            "Unknown thinking type '%s', defaulting to reasoning_effort='medium'",
            thinking_type,
        )
        return "medium"

    return None


def _max_effort(a: str | None, b: str | None) -> str | None:
    """Return the higher of two reasoning_effort levels, treating None as unset."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _EFFORT_RANK.get(a, 3) >= _EFFORT_RANK.get(b, 3) else b


@app.post("/v1/messages", response_model=None)
async def create_message(request_data: MessagesRequest, raw_request: Request):
    """
    Process chat completion requests using unified client architecture.

    This endpoint handles both streaming and non-streaming responses,
    automatically routing to the appropriate provider based on model name.
    """
    request_id = raw_request.state.request_id
    start_time = time.time()

    _ensure_runtime_state()

    spend_warning: str | None = None

    # Spend cap check (before any model resolution or forwarding)
    if cost_tracker is not None and cost_tracker.has_caps:
        projected = 0
        if cost_tracker.cap_mode == "strict":
            from forge.core.models.pricing import calculate_cost as _est_cost

            _est_max_output = request_data.max_tokens or 4096
            _est_input = _estimate_input_tokens(request_data)
            try:
                projected = _est_cost(
                    request_data.model or "claude-sonnet-4-6",
                    _est_input,
                    _est_max_output,
                    0,
                )
            except Exception:
                projected = 0

        cap_result = cost_tracker.check_cap(projected_cost_micros=projected)
        if cap_result.exceeded:
            spend_warning = _cap_result_message(cap_result)
            if cost_tracker.on_cap_hit == "reject":
                return JSONResponse(
                    status_code=429,
                    content={
                        "type": "error",
                        "error": {
                            "type": "spend_cap_exceeded",
                            "message": spend_warning,
                        },
                    },
                    headers={"X-Request-ID": request_id},
                )
            logger.warning("[%s] %s", request_id, spend_warning)

    # Resolve effective tier (routing invariants):
    # Precedence: request explicit tier > config.proxy.default_tier
    # If neither is available, fail fast (misconfiguration).
    if request_data.has_explicit_tier and request_data.tier:
        # Request explicitly specified a tier (haiku/sonnet/opus in model name)
        resolved_tier: str = request_data.tier
        resolved_tier_source = "request"
    elif config.proxy.default_tier:
        resolved_tier = config.proxy.default_tier
        resolved_tier_source = "proxy.default_tier"
    else:
        raise HTTPException(
            status_code=500,
            detail={
                "type": "configuration_error",
                "message": "config.proxy.default_tier is required for ambiguous requests under proxy-only routing",
            },
        )

    logger.debug(f"[{request_id}] Resolved tier: {resolved_tier} (source={resolved_tier_source})")

    request_data.tier = resolved_tier

    # Determine if this is an explicit backend model or needs tier-based resolution
    # Only re-resolve model based on tier if:
    #   1. Model was mapped from Anthropic-style (contains haiku/sonnet/opus), OR
    #   2. Model is truly ambiguous (no provider prefix and not a known backend model)
    # Do NOT override explicit backend models like "openai/gpt-5.5" or "vertex_ai/gemini-3.1-pro"
    original_model_name = request_data.original_model_name
    mapped_model = map_model_name(request_data.model)  # Map AFTER reload() for fresh config

    # Check if original model is an explicit backend model (has provider prefix)
    # These should be passed through, not tier-resolved
    if config.proxy.preferred_provider == "openrouter":
        # OpenRouter: any provider/model format is explicit (google/, meta-llama/, etc.)
        is_explicit_backend = original_model_name is not None and "/" in original_model_name
    else:
        is_explicit_backend = (
            original_model_name is not None
            and "/" in original_model_name
            and any(
                original_model_name.startswith(prefix)
                for prefix in [
                    "openai/",
                    "anthropic/",
                    "vertex_ai/",
                    "bedrock/",
                    "gemini/",
                    "together_ai/",
                    "replicate/",
                ]
            )
        )

    # Only use tier-resolved model for Anthropic-style or ambiguous requests
    # For explicit backend models, use what map_model_name() returned (usually pass-through)
    if is_explicit_backend:
        # Explicit backend model - preserve it (map_model_name already handled it)
        actual_model_id = mapped_model
        logger.debug(
            f"[{request_id}] Explicit backend model '{original_model_name}' - preserving as '{actual_model_id}'"
        )
    else:
        # Anthropic-style or ambiguous — check alternatives, then fall back to tier default
        tier_default = config.proxy.get_model_for_tier(resolved_tier)
        actual_model_id = _resolve_model_with_alternatives(resolved_tier, original_model_name, tier_default)
        logger.debug(f"[{request_id}] Tier-resolved model: tier={resolved_tier} -> '{actual_model_id}'")

    try:
        num_messages = len(request_data.messages) if request_data.messages else 0
        num_tools = len(request_data.tools) if request_data.tools else 0
        tool_names = [tool.name for tool in request_data.tools] if request_data.tools else []
        has_system = bool(request_data.system)

        await _check_client_tool_failures(request_data, request_id, actual_model_id)

        # Detect provider BEFORE conversion to enable provider-specific schema handling
        detected_provider = client_factory.detect_provider_for_model(actual_model_id)
        provider_name = detected_provider.value  # Convert enum to string

        logger.debug(
            f"[{request_id}] Processing '/v1/messages': "
            f"original='{original_model_name}', target='{actual_model_id}', provider='{provider_name}', "
            f"messages={num_messages}, tools={num_tools}, stream={request_data.stream}"
        )

        openai_request_dict = convert_anthropic_to_openai(request_data, provider=provider_name)

        openai_request_dict["model"] = actual_model_id

        # Forward User-Agent from incoming request (Claude Code identity).
        # Upstream LLM gateways may filter traffic by User-Agent; without this,
        # the proxy's OpenAI SDK default header could cause requests to be blocked.
        # Only inject for LiteLLM providers (other clients don't need it).
        if provider_name in ("litellm_remote", "litellm_local", "openrouter"):
            incoming_user_agent = raw_request.headers.get("user-agent")
            if incoming_user_agent:
                openai_request_dict["_user_agent"] = incoming_user_agent
                logger.debug(f"[{request_id}] Forwarding User-Agent: {incoming_user_agent[:120]!r}")

        # Priority: request explicit > tier_override > model default (in catalog)
        tier_override = _get_tier_override(resolved_tier)
        if tier_override:
            logger.debug(f"[{request_id}] Tier override for '{resolved_tier}': {tier_override}")

        if request_data.temperature is not None:
            openai_request_dict["temperature"] = request_data.temperature
        elif tier_override and tier_override.temperature is not None:
            openai_request_dict["temperature"] = tier_override.temperature

        if request_data.max_tokens is not None:
            openai_request_dict["max_tokens"] = request_data.max_tokens
        if request_data.top_p is not None:
            openai_request_dict["top_p"] = request_data.top_p

        # Optional reasoning/thinking overrides.
        # Priority: request explicit > thinking-derived > tier_override > model default
        # tier_override acts as a FLOOR (never go below the user's tier config).
        # Use getattr() for test stubs that don't include new fields.
        reasoning_effort = getattr(request_data, "reasoning_effort", None)
        if reasoning_effort is not None:
            openai_request_dict["reasoning_effort"] = reasoning_effort
        else:
            # Claude Code sends `thinking` (Anthropic-specific) instead of
            # `reasoning_effort`. Translate to reasoning_effort so litellm can
            # map it to each provider's native parameter.
            thinking = getattr(request_data, "thinking", None)
            derived = _derive_reasoning_effort(thinking)

            # Apply tier_override as a floor: max(derived, tier_override).
            tier_effort = tier_override.reasoning_effort if tier_override else None
            openai_request_dict["reasoning_effort"] = _max_effort(derived, tier_effort)

        # Note: the raw `thinking` dict is NOT forwarded — it's Anthropic-specific.
        # Litellm controls thinking via reasoning_effort (mapped above).

        verbosity = getattr(request_data, "verbosity", None)
        if verbosity is not None:
            openai_request_dict["verbosity"] = verbosity
        elif tier_override and tier_override.verbosity is not None:
            openai_request_dict["verbosity"] = tier_override.verbosity

        if request_data.stop_sequences:
            openai_request_dict["stop"] = request_data.stop_sequences

        # Get unified client for this model (pass tier for tier-specific hyperparameters)
        try:
            client = await client_factory.get_client(actual_model_id, tier=request_data.tier)
            logger.debug(f"[{request_id}] Got client for {actual_model_id} (tier={request_data.tier})")
        except AuthenticationError as e:
            logger.error(f"[{request_id}] Authentication failed: {e}")
            raise HTTPException(
                status_code=401,
                detail={
                    "type": "authentication_error",
                    "message": f"Authentication failed: {e}",
                },
            )

        if request_data.stream:
            # Streaming response
            async def stream_generator():
                try:
                    async for chunk in client.create_streaming_completion(openai_request_dict, request_id):
                        yield chunk
                except ToolCallError as e:
                    yield {
                        "error": {
                            "type": e.error_type,
                            "message": str(e),
                        }
                    }
                except ProxyStreamError as e:
                    logger.error(f"[{request_id}] ProxyStreamError ({e.error_type}): {e}")
                    yield {
                        "error": {
                            "type": e.error_type,
                            "message": str(e),
                            "status_code": e.status_code,
                        }
                    }

            headers = {
                "X-Request-ID": request_id,
                "X-Resolved-Tier": resolved_tier,
                "X-Resolved-Model": actual_model_id,
                "X-Cumulative-Cost": f"{proxy_metrics.total_cost_micros / 1_000_000:.6f}",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
            headers = _with_spend_warning(headers, spend_warning)

            # Log streaming request (no response body available)
            duration_ms = (time.time() - start_time) * 1000
            asyncio.create_task(
                log_request_response(
                    request_id=request_id,
                    original_model=original_model_name or "",
                    mapped_model=actual_model_id,
                    request_body=request_data.model_dump(),
                    response_body=None,  # Streaming has no response body
                    status_code=200,
                    duration_ms=duration_ms,
                    num_messages=num_messages,
                    num_tools=num_tools,
                    tool_names=tool_names,
                    has_system=has_system,
                    temperature=request_data.temperature,
                    max_tokens=request_data.max_tokens,
                    streaming=True,
                )
            )

            log_request_beautifully(
                method="POST",
                path="/v1/messages (streaming)",
                original_model=original_model_name or "",
                mapped_model=actual_model_id,
                num_messages=num_messages,
                num_tools=num_tools,
                status_code=200,
            )

            def _on_stream_complete(usage: dict[str, int], failed: bool, error_type: str | None) -> None:
                elapsed = (time.time() - start_time) * 1000
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cache_tok = usage.get("cached_tokens", 0)
                cost = _calc_and_log_cost(
                    model=actual_model_id,
                    tier=resolved_tier,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cached_tokens=cache_tok,
                    latency_ms=elapsed,
                    failed=failed,
                    request_id=request_id,
                )
                proxy_metrics.record_request(
                    tier=resolved_tier,
                    model=actual_model_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cached_tokens=cache_tok,
                    latency_ms=elapsed,
                    streaming=True,
                    failed=failed,
                    error_type=error_type,
                    cost_micros=cost,
                )

            return StreamingResponse(
                convert_openai_to_anthropic_sse(
                    stream_generator(),
                    request_data,
                    request_id,
                    on_complete=_on_stream_complete,
                ),
                media_type="text/event-stream",
                headers=headers,
            )
        else:
            try:
                openai_response = await client.create_completion(openai_request_dict, request_id)
                anthropic_response = convert_openai_to_anthropic(openai_response, original_model_name)

                if not anthropic_response:
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "type": "api_error",
                            "message": "Failed to convert response",
                        },
                    )

                response_dict = anthropic_response.model_dump()
                response_dict["_request_id"] = request_id

                duration_ms = (time.time() - start_time) * 1000

                _usage = openai_response.get("usage", {})
                _in = _usage.get("prompt_tokens", 0)
                _out = _usage.get("completion_tokens", 0)
                _cached = _usage.get("cached_tokens", 0)
                _cost = _calc_and_log_cost(
                    model=actual_model_id,
                    tier=resolved_tier,
                    input_tokens=_in,
                    output_tokens=_out,
                    cached_tokens=_cached,
                    latency_ms=duration_ms,
                    failed=False,
                    request_id=request_id,
                )
                proxy_metrics.record_request(
                    tier=resolved_tier,
                    model=actual_model_id,
                    input_tokens=_in,
                    output_tokens=_out,
                    cached_tokens=_cached,
                    latency_ms=duration_ms,
                    streaming=False,
                    failed=False,
                    error_type=None,
                    cost_micros=_cost,
                )

                asyncio.create_task(
                    log_request_response(
                        request_id=request_id,
                        original_model=original_model_name or "",
                        mapped_model=actual_model_id,
                        request_body=request_data.model_dump(),
                        response_body=response_dict,
                        status_code=200,
                        duration_ms=duration_ms,
                        num_messages=num_messages,
                        num_tools=num_tools,
                        tool_names=tool_names,
                        has_system=has_system,
                        temperature=request_data.temperature,
                        max_tokens=request_data.max_tokens,
                        streaming=False,
                    )
                )

                log_request_beautifully(
                    method="POST",
                    path="/v1/messages",
                    original_model=original_model_name or "",
                    mapped_model=actual_model_id,
                    num_messages=num_messages,
                    num_tools=num_tools,
                    status_code=200,
                )
                return JSONResponse(
                    content=response_dict,
                    headers=_with_spend_warning(
                        {
                            "X-Request-ID": request_id,
                            "X-Resolved-Tier": resolved_tier,
                            "X-Resolved-Model": actual_model_id,
                            "X-Request-Cost": f"{_cost / 1_000_000:.6f}",
                            "X-Cumulative-Cost": f"{proxy_metrics.total_cost_micros / 1_000_000:.6f}",
                        },
                        spend_warning,
                    ),
                )

            except ToolCallError as e:
                duration_ms = (time.time() - start_time) * 1000
                error_msg = str(e)

                _tc_cost = _calc_and_log_cost(
                    model=actual_model_id,
                    tier=resolved_tier,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    latency_ms=duration_ms,
                    failed=True,
                    request_id=request_id,
                )
                proxy_metrics.record_request(
                    tier=resolved_tier,
                    model=actual_model_id,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    latency_ms=duration_ms,
                    streaming=False,
                    failed=True,
                    error_type="tool_call_error",
                    cost_micros=_tc_cost,
                )

                asyncio.create_task(
                    log_request_response(
                        request_id=request_id,
                        original_model=original_model_name or "",
                        mapped_model=actual_model_id,
                        request_body=request_data.model_dump(),
                        response_body=None,
                        status_code=400,
                        duration_ms=duration_ms,
                        error=error_msg,
                        num_messages=num_messages,
                        num_tools=num_tools,
                        tool_names=tool_names,
                        has_system=has_system,
                        temperature=request_data.temperature,
                        max_tokens=request_data.max_tokens,
                        streaming=False,
                    )
                )

                log_request_beautifully(
                    method="POST",
                    path="/v1/messages",
                    original_model=original_model_name or "",
                    mapped_model=actual_model_id,
                    num_messages=num_messages,
                    num_tools=num_tools,
                    status_code=400,
                )

                logger.error(f"[{request_id}] Tool call error: {e}")
                raise HTTPException(
                    status_code=400,
                    detail={"type": "invalid_request_error", "message": error_msg},
                )
            except AuthenticationError:
                # Try refreshing credentials once
                logger.warning(f"[{request_id}] Auth failed, refreshing credentials")
                client = await client_factory.invalidate_and_retry(actual_model_id)
                openai_response = await client.create_completion(openai_request_dict, request_id)
                anthropic_response = convert_openai_to_anthropic(openai_response, original_model_name)

                if not anthropic_response:
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "type": "api_error",
                            "message": "Failed to convert response after retry",
                        },
                    )

                retry_duration_ms = (time.time() - start_time) * 1000
                _retry_usage = openai_response.get("usage", {})
                _ri = _retry_usage.get("prompt_tokens", 0)
                _ro = _retry_usage.get("completion_tokens", 0)
                _rc = _retry_usage.get("cached_tokens", 0)
                _rcost = _calc_and_log_cost(
                    model=actual_model_id,
                    tier=resolved_tier,
                    input_tokens=_ri,
                    output_tokens=_ro,
                    cached_tokens=_rc,
                    latency_ms=retry_duration_ms,
                    failed=False,
                    request_id=request_id,
                )
                proxy_metrics.record_request(
                    tier=resolved_tier,
                    model=actual_model_id,
                    input_tokens=_ri,
                    output_tokens=_ro,
                    cached_tokens=_rc,
                    latency_ms=retry_duration_ms,
                    streaming=False,
                    failed=False,
                    error_type=None,
                    cost_micros=_rcost,
                )

                response_dict = anthropic_response.model_dump()
                response_dict["_request_id"] = request_id
                return JSONResponse(
                    content=response_dict,
                    headers=_with_spend_warning(
                        {
                            "X-Request-ID": request_id,
                            "X-Resolved-Tier": resolved_tier,
                            "X-Resolved-Model": actual_model_id,
                            "X-Request-Cost": f"{_rcost / 1_000_000:.6f}",
                            "X-Cumulative-Cost": f"{proxy_metrics.total_cost_micros / 1_000_000:.6f}",
                        },
                        spend_warning,
                    ),
                )

    except HTTPException:
        raise
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        error_msg = f"Internal error: {str(e)}"

        _err_cost = _calc_and_log_cost(
            model=actual_model_id,
            tier=resolved_tier,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            latency_ms=duration_ms,
            failed=True,
            request_id=request_id,
        )
        proxy_metrics.record_request(
            tier=resolved_tier,
            model=actual_model_id,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            latency_ms=duration_ms,
            streaming=request_data.stream or False,
            failed=True,
            error_type="api_error",
            cost_micros=_err_cost,
        )

        asyncio.create_task(
            log_request_response(
                request_id=request_id,
                original_model=original_model_name or "",
                mapped_model=actual_model_id,
                request_body=request_data.model_dump(),
                response_body=None,
                status_code=500,
                duration_ms=duration_ms,
                error=error_msg,
                num_messages=num_messages,
                num_tools=num_tools,
                tool_names=tool_names,
                has_system=has_system,
                temperature=request_data.temperature,
                max_tokens=request_data.max_tokens,
                streaming=request_data.stream or False,
            )
        )

        log_request_beautifully(
            method="POST",
            path="/v1/messages",
            original_model=original_model_name or "",
            mapped_model=actual_model_id,
            num_messages=num_messages,
            num_tools=num_tools,
            status_code=500,
        )

        logger.error(f"[{request_id}] Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"type": "api_error", "message": error_msg})


@app.post("/v1/messages/count_tokens", response_model=TokenCountResponse)
async def count_tokens(request_data: TokenCountRequest, raw_request: Request):
    """Count tokens using the appropriate client's token counter."""
    request_id = raw_request.state.request_id

    _ensure_runtime_state()

    try:
        original_model_name = request_data.original_model_name

        # Resolve tier FIRST (same precedence as message routing)
        if request_data.has_explicit_tier and request_data.tier:
            resolved_tier: str = request_data.tier
            resolved_tier_source = "request"
        elif config.proxy.default_tier:
            resolved_tier = config.proxy.default_tier
            resolved_tier_source = "proxy.default_tier"
        else:
            raise HTTPException(
                status_code=500,
                detail={
                    "type": "configuration_error",
                    "message": "config.proxy.default_tier is required for ambiguous requests under proxy-only routing",
                },
            )

        request_data.tier = resolved_tier

        # Match the /v1/messages model resolution: explicit backend models are
        # preserved; Anthropic-style or ambiguous models go through tier + alternatives.
        mapped_model = map_model_name(request_data.model)

        if config.proxy.preferred_provider == "openrouter":
            is_explicit_backend = original_model_name is not None and "/" in original_model_name
        else:
            is_explicit_backend = (
                original_model_name is not None
                and "/" in original_model_name
                and any(
                    original_model_name.startswith(p)
                    for p in [
                        "openai/",
                        "anthropic/",
                        "vertex_ai/",
                        "bedrock/",
                        "gemini/",
                        "together_ai/",
                        "replicate/",
                    ]
                )
            )

        if is_explicit_backend:
            actual_model_id = mapped_model
        else:
            tier_default = config.proxy.get_model_for_tier(resolved_tier)
            actual_model_id = _resolve_model_with_alternatives(resolved_tier, original_model_name, tier_default)

        logger.info(f"[{request_id}] Token counting: original='{original_model_name}', target='{actual_model_id}'")
        logger.debug(f"[{request_id}] Token count resolved tier: {resolved_tier} (source={resolved_tier_source})")

        detected_provider = client_factory.detect_provider_for_model(actual_model_id)
        provider_name = detected_provider.value

        simulated_request = MessagesRequest(
            model=actual_model_id,
            messages=request_data.messages,
            system=request_data.system,
            max_tokens=1,
        )
        openai_dict = convert_anthropic_to_openai(simulated_request, provider=provider_name)
        messages = openai_dict.get("messages", [])

        client = await client_factory.get_client(actual_model_id, tier=resolved_tier)
        token_count = await client.count_tokens(messages)

        response = TokenCountResponse(input_tokens=token_count)
        return JSONResponse(content=response.model_dump(), headers={"X-Request-ID": request_id})

    except Exception as e:
        logger.error(f"[{request_id}] Token counting failed: {e}")
        raise HTTPException(
            status_code=500,
            detail={"type": "api_error", "message": f"Token counting failed: {str(e)}"},
        )


DEFAULT_CONTEXT_WINDOW = 200000


def get_context_window(model_name: str) -> int:
    """Get context window size for a model from the central catalog.

    Falls back to a safe default for models not in the catalog (e.g.,
    OpenRouter models outside Forge's known set).

    Args:
        model_name: Model ID (canonical or alias like 'openai/gpt-5.5')

    Returns:
        Context window size in tokens.
    """
    from forge.core.models import get_context_window_tokens, model_exists

    if not model_exists(model_name):
        logger.debug(f"Model {model_name!r} not in catalog, using default context window")
        return DEFAULT_CONTEXT_WINDOW

    return get_context_window_tokens(model_name)


@app.get("/", include_in_schema=False)
async def root(request: Request):
    """Service health and runtime truth for status line scripts.

    Returns proxy runtime status including:
    - is_proxy: True (indicates this is a proxy, not direct Anthropic API)
    - template: Active configuration template name
    - provider: Underlying provider (litellm, openai, gemini)
    - tiers: Mapping of Claude tiers to actual models with context windows
    - proxy: First-class proxy identity (proxy_id, template, port, base_url)
    - runtime: Actual resolved tier → model mappings, context windows, llm defaults

    Note: Session state is no longer returned by proxy. Consumers should read
    session state locally via FORGE_SESSION env var or CWD manifest.

    This endpoint reflects what the proxy is **actually doing**, not just
    echoed configuration. It serves as the source of runtime truth.
    """
    import os

    from forge.proxy.proxy_identity import get_proxy_identity

    active_template = os.environ.get("ACTIVE_TEMPLATE", "unknown")
    preferred_provider = os.environ.get("PREFERRED_PROVIDER", "unknown")

    # Extract request host/port for proxy identity (accurate even with --auto-port)
    request_host = request.url.hostname or "localhost"
    request_port = request.url.port

    # Fallback to env var if request port unavailable
    env_port_str = os.environ.get("ACTIVE_PORT")
    env_port = int(env_port_str) if env_port_str else None

    # Discover proxy identity (2-tier: registry > derived)
    proxy_identity = get_proxy_identity(
        active_template=active_template,
        request_host=request_host,
        request_port=request_port,
        env_port=env_port,
        process_proxy_id=os.environ.get("FORGE_PROXY_ID"),
    )

    # Tier mappings exposed via GET / for status line and session context
    tiers = {}
    provider_config = config.proxy.get_provider(preferred_provider)
    tier_models = {
        "haiku": provider_config.tiers.haiku,
        "sonnet": provider_config.tiers.sonnet,
        "opus": provider_config.tiers.opus,
    }

    for tier, model in tier_models.items():
        tiers[tier] = {
            "model": model,
            "context_window": get_context_window(model),
        }

    # Compute runtime LLM defaults (post-merge) from the credential manager.
    # This reflects the actual baseline hyperparameters used by proxy clients,
    # including env/tier overrides and caps.
    llm_defaults_by_tier: dict[str, dict[str, object]] = {}
    for tier in ("haiku", "sonnet", "opus"):
        try:
            model_name = tier_models.get(tier)
            if not model_name:
                raise ValueError(f"No model configured for tier {tier!r}")
            hp = client_factory.get_default_hyperparams_for_tier(
                provider=preferred_provider, tier=tier, model_name=model_name
            )
            llm_defaults_by_tier[tier] = hp.model_dump(exclude_unset=True)
        except Exception as e:
            llm_defaults_by_tier[tier] = {"error": f"failed to compute defaults: {e}"}

    if config.proxy.default_tier:
        default_tier = config.proxy.default_tier
        default_tier_source = "proxy.default_tier"
    else:
        default_tier = None
        default_tier_source = "missing"

    runtime_active_model = tier_models.get(default_tier or "sonnet") or tier_models.get("sonnet")

    routing_section = {
        "default_tier": default_tier,
        "default_tier_source": default_tier_source,
        "note": "Routing defaults are proxy-owned. Session state is not authoritative for routing defaults.",
    }

    if default_tier is None:
        routing_section["note"] = (
            "Proxy is missing config.proxy.default_tier; ambiguous requests will fail until configured."
        )

    runtime_section = {
        "template": active_template,
        "provider": preferred_provider,
        "tier_mappings": tier_models,
        "context_windows": {tier: get_context_window(model) for tier, model in tier_models.items()},
        "active_tier": default_tier,
        "active_context_window": get_context_window(runtime_active_model) if runtime_active_model else None,
        # Proxy-owned hyperparameter defaults actually used by proxy clients (post-merge)
        "llm_defaults_by_tier": llm_defaults_by_tier,
    }

    # Build proxy identity section (B2.1.5)
    proxy_section = {
        "proxy_id": proxy_identity.proxy_id,
        "template": proxy_identity.template,
        "port": proxy_identity.port,
        "base_url": proxy_identity.base_url,
        "source": proxy_identity.source,
        "status": proxy_identity.status,
    }

    response = {
        "is_proxy": True,
        "template": active_template,
        "provider": preferred_provider,
        "tiers": tiers,
        "status": "running",
        "routing": routing_section,
        # Proxy identity (B2.1.5): first-class proxy identity
        "proxy": proxy_section,
        # Runtime truth: tier mappings, context windows, hyperparameter defaults
        "runtime": runtime_section,
        # Per-proxy metrics (request counts, token usage, latency)
        "metrics": proxy_metrics.snapshot(),
    }

    return response


@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    """Request logging middleware."""
    start_time = time.time()

    path = request.url.path
    prefix = "req_"
    if "/count_tokens" in path:
        prefix = "tok_"
    elif "/" == path:
        prefix = "inf_"

    request_id = request.headers.get("X-Request-ID") or f"{prefix}{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id

    # Endpoints that have their own detailed logging
    verbose_endpoints = ("/messages", "/event_logging")
    has_own_logging = any(ep in path for ep in verbose_endpoints)

    logger.debug(f"{path} [{request_id}] {request.method}")

    try:
        response = await call_next(request)
        elapsed = time.time() - start_time

        if has_own_logging:
            logger.debug(f"{path} [{request_id}] Middleware: {elapsed:.3f}s")
        else:
            status = response.status_code
            logger.info(f"{path} [{request_id}] Completed in {elapsed:.3f}s ({status})")

        if "X-Request-ID" not in response.headers:
            response.headers["X-Request-ID"] = request_id

        return response
    except Exception as e:
        logger.error(f"[{request_id}] Middleware error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "api_error",
                    "message": f"Internal error [{request_id}]",
                }
            },
            headers={"X-Request-ID": request_id},
        )


async def _check_client_tool_failures(request_data: MessagesRequest, request_id: str, mapped_model: str):
    """Check for client-side tool execution failures in the request.

    Only scans the most recent user message. Older tool_result blocks were
    already inspected on prior requests; re-scanning them produces duplicate
    log entries and skews telemetry.
    """
    latest_user_msg = next(
        (m for m in reversed(request_data.messages) if m.role == "user" and isinstance(m.content, list)),
        None,
    )
    if latest_user_msg is None:
        return

    for msg in (latest_user_msg,):
        if msg.role == "user" and isinstance(msg.content, list):
            for block in msg.content:
                if hasattr(block, "type") and block.type == "tool_result":
                    tool_use_id = getattr(block, "tool_use_id", None)
                    is_error = False
                    error_content = None

                    # 1. Most reliable: Check explicit is_error field
                    if hasattr(block, "is_error") and block.is_error:
                        is_error = True
                        if hasattr(block, "content"):
                            error_content = block.content

                    if hasattr(block, "content") and not is_error:
                        # 2. Check for dict with error keys (structured errors)
                        if isinstance(block.content, dict) and any(k in block.content for k in ["error", "exception"]):
                            is_error = True
                            error_content = block.content
                        # 3. For string content, only check for explicit error patterns at the start
                        # Don't scan the entire content as it causes false positives with documentation
                        elif isinstance(block.content, str):
                            content_start = block.content[:200] if len(block.content) > 200 else block.content
                            # Be specific to avoid false positives
                            error_patterns = [
                                "Error:",
                                "ERROR:",
                                "Exception:",
                                "EXCEPTION:",
                                "Failed:",
                                "FAILED:",
                                "Tool execution failed",
                                "Command failed",
                                "File not found",
                                "Permission denied",
                                "Invalid tool",  # More specific than just "Invalid"
                                "Invalid arguments",
                                "Invalid input",
                                "Traceback (most recent call last)",
                            ]
                            if any(content_start.startswith(pattern) for pattern in error_patterns):
                                is_error = True
                                error_content = block.content
                            else:
                                error_content = None
                        else:
                            error_content = block.content

                    if is_error and tool_use_id:
                        tool_name, tool_input = _find_tool_use_info(request_data.messages, msg, tool_use_id)

                        # Check if this is a stale cleared tool result (not actionable)
                        is_cleared_content = (
                            isinstance(error_content, str) and "Old tool result content cleared" in error_content
                        )

                        # Only log as warning if we have actual error content (not cleared)
                        if error_content and not is_cleared_content:
                            logger.warning(
                                f"[{request_id}] Client tool failure: "
                                f"tool='{tool_name or 'unknown'}', id='{tool_use_id}', "
                                f"error={str(error_content)[:100]}"
                            )
                        elif is_cleared_content:
                            logger.debug(
                                f"[{request_id}] Stale tool failure (content cleared): "
                                f"tool='{tool_name or 'unknown'}', id='{tool_use_id}'"
                            )
                        else:
                            # Debug log for investigation when is_error but no content
                            logger.debug(
                                f"[{request_id}] Tool marked as error but no error content: "
                                f"tool='{tool_name or 'unknown'}', id='{tool_use_id}', "
                                f"is_error={getattr(block, 'is_error', None)}"
                            )

                        enriched_content = error_content
                        if error_content and not is_cleared_content and isinstance(error_content, str):
                            provider_cfg = config.proxy.get_provider()
                            if provider_cfg.error_hints:
                                enriched_content = enrich_error_content(tool_name, error_content)
                                if enriched_content != error_content:
                                    block.content = enriched_content
                                    logger.debug(f"[{request_id}] Enriched error hint for tool '{tool_name}'")

                        # Only log as failure if we have actual error content (not cleared)
                        if error_content and not is_cleared_content:
                            asyncio.create_task(
                                log_tool_failure(
                                    request_id=request_id,
                                    mapped_model=mapped_model,
                                    tool_name=tool_name,
                                    tool_use_id=tool_use_id,
                                    tool_input=tool_input,
                                    error_content=error_content,
                                )
                            )
                            asyncio.create_task(
                                log_tool_event(
                                    request_id=request_id,
                                    tool_name=tool_name,
                                    status="failure",
                                    stage="client_execution_report",
                                    details={
                                        "tool_use_id": tool_use_id,
                                        "error_content": enriched_content,
                                        "tool_name_found": bool(tool_name),
                                    },
                                )
                            )


def _find_tool_use_info(messages, current_msg, tool_use_id) -> tuple[str | None, dict[str, Any] | None]:
    """Find tool name and input parameters from message history."""
    current_idx = messages.index(current_msg)

    for i in range(current_idx - 1, -1, -1):
        prev_msg = messages[i]
        if prev_msg.role == "assistant" and isinstance(prev_msg.content, list):
            for block in prev_msg.content:
                if (
                    hasattr(block, "type")
                    and block.type == "tool_use"
                    and hasattr(block, "id")
                    and block.id == tool_use_id
                ):
                    return (
                        getattr(block, "name", None),
                        getattr(block, "input", None),
                    )
    return None, None


def find_available_port(start_port: int, max_attempts: int = 10) -> int:
    """Find an available port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("", port))
                sock.close()
                return port
            except OSError:
                continue
    raise RuntimeError(f"Could not find available port in range {start_port}-{start_port + max_attempts}")


@click.command()
@click.option(
    "--template",
    type=str,
    required=True,
    help="Configuration template to use (e.g., openrouter-gemini, openrouter-openai, openrouter-anthropic)",
)
@click.option("--port", type=int, default=8082, help="Port to run the server on (default: 8082)")
@click.option("--host", default="0.0.0.0", help="Host to bind the server to (default: 0.0.0.0)")
@click.option("--reload", is_flag=True, help="Enable auto-reload on code changes")
@click.option(
    "--auto-port",
    is_flag=True,
    help="Automatically find an available port if the specified port is in use",
)
@click.option(
    "--proxy-id",
    type=str,
    required=False,
    help="Explicit proxy id (enables proxy-scoped overrides + strict startup validation).",
)
def main(
    template: str,
    port: int,
    host: str,
    reload: bool,
    auto_port: bool,
    proxy_id: str | None,
):
    """Start the Unified LLM Proxy server with template-based configuration.

    Template configurations are defined in YAML files under config/defaults/templates/.
    Each template specifies:
    - Provider (gemini, openai, litellm)
    - Model tier mappings (haiku, sonnet, opus)
    - Provider-specific settings (reasoning effort, cache TTL, etc.)
    """
    import os

    from forge.config.loader import template_exists

    if not template_exists(template):
        click.echo(f"Unknown template '{template}'")
        click.echo("Run 'forge proxy template list' to see available templates.")
        sys.exit(1)

    level = get_effective_log_level()
    if level != "off":
        configure_debug_logging(component="proxy", subdirectory="proxy")
        configure_console_logging()

    effective_proxy_id = proxy_id

    try:
        cfg = init_config(template=template, proxy_id=effective_proxy_id)
        provider = cfg.proxy.preferred_provider
        default_port = cfg.proxy.default_port

        if not provider:
            click.echo(f"✘ Template '{template}' missing 'preferred_provider' field")
            sys.exit(1)

    except Exception as e:
        click.echo(f"✘ Failed to load template '{template}': {e}")
        sys.exit(1)

    if default_port and default_port != port:
        click.echo(
            f"⚠︎  Warning: Template '{template}' typically uses port {default_port}, but starting on port {port}"
        )
        click.echo(f" Recommended: python -m forge.proxy.server --template {template} --port {default_port}")

    actual_port = port
    if auto_port:
        if effective_proxy_id is not None:
            click.echo("✘ --auto-port cannot be used when starting under a proxy id")
            sys.exit(1)

        actual_port = find_available_port(port)
        if actual_port != port:
            click.echo(f"⚠︎  Port {port} is in use, using port {actual_port} instead")
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                sock.close()
            except OSError:
                click.echo(f"✘ Port {port} is already in use!")
                click.echo(" Use --auto-port to automatically find an available port")
                sys.exit(1)

    # Strict proxy startup validation (B2.1.3)
    if effective_proxy_id is not None:
        try:
            from forge.proxy.proxy_startup import (
                ProxyStartupContext,
                ProxyStartupValidationError,
                validate_proxy_startup,
            )

            validate_proxy_startup(
                ctx=ProxyStartupContext(proxy_id=effective_proxy_id, template=template, port=actual_port)
            )

        except ProxyStartupValidationError as e:
            click.echo(f"✘ {e}")
            sys.exit(1)
        except Exception as e:
            click.echo(f"✘ Failed to validate proxy startup: {e}")
            sys.exit(1)

    # Track which template is active (for runtime introspection)
    # Set ACTIVE_PORT to actual_port (not port) to handle --auto-port correctly
    os.environ["ACTIVE_TEMPLATE"] = template
    os.environ["ACTIVE_PORT"] = str(actual_port)
    os.environ["PREFERRED_PROVIDER"] = provider

    # Freeze proxy id for request handlers. Set in env so the uvicorn worker
    # (which reimports the module when app is passed as a string) picks it up.
    global PROXY_ID
    PROXY_ID = effective_proxy_id
    if effective_proxy_id is not None:
        os.environ["FORGE_PROXY_ID"] = effective_proxy_id

    # Initialize in this module for direct/app-object runs; the imported
    # uvicorn app module initializes itself lazily via _ensure_runtime_state().
    _initialize_cost_tracker_from_config()

    provider_cfg = cfg.proxy.get_provider(provider)
    tier_models = {
        "haiku": provider_cfg.tiers.haiku,
        "sonnet": provider_cfg.tiers.sonnet,
        "opus": provider_cfg.tiers.opus,
    }

    click.echo("")
    click.echo("╔══════════════════════════════════════╗")
    click.echo("║     Unified LLM Proxy Server         ║")
    click.echo("╚══════════════════════════════════════╝")
    click.echo("")
    click.echo(f"🌐 Server:    http://{host}:{actual_port}")
    click.echo(f" Template:  {template}")
    click.echo(f"📡 Provider:  {provider}")
    click.echo(f" Log Level: {level}")
    click.echo(f"🔄 Reload:    {'enabled' if reload else 'disabled'}")
    click.echo("")
    click.echo(" Model Tier Mappings:")
    for tier, model in tier_models.items():
        if model:
            click.echo(f"   {tier.capitalize():6} → {model}")
    click.echo("")

    click.echo("  Provider Settings:")
    click.echo(f"   cache_ttl: {provider_cfg.cache_ttl}")
    if provider_cfg.base_url:
        click.echo(f"   base_url: {provider_cfg.base_url}")
    click.echo("")

    if effective_proxy_id is not None:
        click.echo(f" Proxy: ~/.forge/proxies/{effective_proxy_id}/proxy.yaml")
    else:
        click.echo(f" Template: defaults/templates/{template}.yaml")
    click.echo("")
    click.echo("Press CTRL+C to stop the server")
    click.echo("")

    uvicorn_level = {
        "off": "warning",
        "debug": "debug",
        "info": "info",
        "warning": "warning",
    }.get(level, "warning")

    uvicorn.run(
        "forge.proxy.server:app",
        host=host,
        port=actual_port,
        log_level=uvicorn_level,
        reload=reload,
    )


if __name__ == "__main__":
    main()
