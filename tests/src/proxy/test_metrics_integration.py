"""Component integration tests for proxy metrics wiring.

These tests verify that metrics are correctly recorded when requests flow
through server.py's create_message endpoint. They use mocked LLM clients
(no real API calls) but exercise the full request → metrics → GET / path.

pytestmark: integration (CIT level — requires server module + metrics module).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.proxy.metrics import proxy_metrics

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyRequestState:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id


class _DummyRawRequest:
    def __init__(self, request_id: str = "req_test") -> None:
        self.state = _DummyRequestState(request_id)


class _DummyAnthropicResponse:
    def model_dump(self) -> dict:
        return {"content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}


def _make_request_data(*, stream: bool = False, tier: str = "sonnet") -> Any:
    """Minimal request_data stub for create_message."""
    return type(
        "Req",
        (),
        {
            "has_explicit_tier": True,
            "tier": tier,
            "stream": stream,
            "messages": [],
            "tools": None,
            "system": None,
            "temperature": None,
            "max_tokens": 1,
            "top_p": None,
            "stop_sequences": None,
            "original_model_name": "claude-sonnet",
            "model": "claude-sonnet",
            "model_dump": lambda self=None: {},
        },
    )()


def _stub_server(monkeypatch, server, *, usage: dict | None = None):
    """Apply standard stubs to the server module for metrics tests."""
    monkeypatch.setattr(server, "reload", lambda: None)
    monkeypatch.setattr(server, "log_request_response", AsyncMock())
    monkeypatch.setattr(server, "log_request_beautifully", lambda *a, **k: None)
    monkeypatch.setattr(server, "log_tool_event", lambda *a, **k: None)
    monkeypatch.setattr(server, "_check_client_tool_failures", AsyncMock())
    monkeypatch.setattr(server, "map_model_name", lambda v: v)
    monkeypatch.setattr(server, "convert_anthropic_to_openai", lambda *a, **k: {"messages": []})
    monkeypatch.setattr(server, "convert_openai_to_anthropic", lambda *a, **k: _DummyAnthropicResponse())
    monkeypatch.setattr(
        server.client_factory,
        "detect_provider_for_model",
        lambda *_: type("E", (), {"value": "openai"})(),
    )

    # Config stub
    class ProxyCfg:
        default_tier = "sonnet"
        preferred_provider = "openai"

        @staticmethod
        def get_model_for_tier(_tier: str) -> str:
            return "openai/gpt-5.5"

    monkeypatch.setattr(server.config, "proxy", ProxyCfg())

    # Default usage for non-streaming responses
    response_usage = usage or {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 0,
    }

    async def _fake_get_client(*args, **kwargs):
        client = AsyncMock()
        client.create_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": response_usage,
            }
        )
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _fake_get_client)


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Ensure metrics are clean between tests."""
    proxy_metrics.reset()
    yield
    proxy_metrics.reset()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_records_metrics(monkeypatch):
    from forge.proxy import server

    _stub_server(
        monkeypatch,
        server,
        usage={
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
            "cached_tokens": 50,
        },
    )

    resp = await server.create_message(_make_request_data(stream=False), _DummyRawRequest())
    assert resp.status_code == 200

    snap = proxy_metrics.snapshot()
    assert snap["total_requests"] == 1
    assert snap["total_streaming"] == 0
    assert snap["tokens"]["input"] == 200
    assert snap["tokens"]["output"] == 80
    assert snap["tokens"]["cached"] == 50
    assert snap["total_failures"] == 0


@pytest.mark.asyncio
async def test_non_streaming_records_per_model(monkeypatch):
    from forge.proxy import server

    _stub_server(monkeypatch, server)

    await server.create_message(_make_request_data(stream=False), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    assert "openai/gpt-5.5" in snap["by_model"]
    assert snap["by_model"]["openai/gpt-5.5"]["requests"] == 1


@pytest.mark.asyncio
async def test_non_streaming_records_per_tier(monkeypatch):
    from forge.proxy import server

    _stub_server(monkeypatch, server)

    await server.create_message(_make_request_data(stream=False, tier="opus"), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    assert "opus" in snap["by_tier"]
    assert snap["by_tier"]["opus"]["requests"] == 1


@pytest.mark.asyncio
async def test_failure_records_metrics(monkeypatch):
    """ToolCallError should increment total_failures and failures_by_type."""
    from forge.proxy import server
    from forge.proxy.base_client import ToolCallError

    _stub_server(monkeypatch, server)

    # Make get_client return a client that raises ToolCallError
    async def _failing_get_client(*args, **kwargs):
        client = AsyncMock()
        client.create_completion = AsyncMock(
            side_effect=ToolCallError("SCHEMA_MISMATCH", "Write", {"error": "bad call"})
        )
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _failing_get_client)

    with pytest.raises(Exception):  # HTTPException wrapping ToolCallError
        await server.create_message(_make_request_data(stream=False), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    assert snap["total_failures"] == 1
    assert snap["failures_by_type"].get("tool_call_error") == 1


@pytest.mark.asyncio
async def test_auth_refresh_records_metrics(monkeypatch):
    """Requests that succeed after credential refresh must still be counted."""
    from forge.core.llm.errors import AuthenticationError
    from forge.proxy import server

    _stub_server(
        monkeypatch,
        server,
        usage={
            "prompt_tokens": 300,
            "completion_tokens": 75,
            "total_tokens": 375,
            "cached_tokens": 100,
        },
    )

    # First call raises AuthenticationError, retry succeeds
    call_count = 0

    async def _auth_failing_get_client(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        client = AsyncMock()
        client.create_completion = AsyncMock(side_effect=AuthenticationError("openai", "token expired"))
        return client

    async def _retry_client(*args, **kwargs):
        client = AsyncMock()
        client.create_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 75, "total_tokens": 375, "cached_tokens": 100},
            }
        )
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _auth_failing_get_client)
    monkeypatch.setattr(server.client_factory, "invalidate_and_retry", _retry_client)

    resp = await server.create_message(_make_request_data(stream=False), _DummyRawRequest())
    assert resp.status_code == 200

    snap = proxy_metrics.snapshot()
    assert snap["total_requests"] == 1
    assert snap["total_failures"] == 0
    assert snap["tokens"]["input"] == 300
    assert snap["tokens"]["output"] == 75
    assert snap["tokens"]["cached"] == 100


@pytest.mark.asyncio
async def test_metrics_accumulate(monkeypatch):
    from forge.proxy import server

    _stub_server(
        monkeypatch,
        server,
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 0,
        },
    )

    for _ in range(3):
        await server.create_message(_make_request_data(stream=False), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    assert snap["total_requests"] == 3
    assert snap["tokens"]["input"] == 300
    assert snap["tokens"]["output"] == 150


@pytest.mark.asyncio
async def test_strict_cap_counts_pydantic_message_content(monkeypatch):
    """Strict cap preflight should count validated Message objects, not only dicts."""
    from forge.proxy import server
    from forge.proxy.cost_tracker import CostTracker
    from forge.proxy.data_models import MessagesRequest

    monkeypatch.setattr(server, "reload", lambda: None)
    monkeypatch.setattr(
        server,
        "cost_tracker",
        CostTracker(daily_cap_usd=0.0005, cap_mode="strict", on_cap_hit="reject"),
    )

    request_data = MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=1,
        messages=[{"role": "user", "content": "x" * 1000}],
    )

    resp = await server.create_message(request_data, _DummyRawRequest())

    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_warn_cap_adds_header_and_allows_request(monkeypatch):
    """Warn mode should continue the request and surface a warning header."""
    from forge.proxy import server
    from forge.proxy.cost_tracker import CostTracker

    _stub_server(monkeypatch, server)

    tracker = CostTracker(daily_cap_usd=0.001, on_cap_hit="warn")
    tracker.record(2_000)
    monkeypatch.setattr(server, "cost_tracker", tracker)

    resp = await server.create_message(_make_request_data(stream=False), _DummyRawRequest())

    assert resp.status_code == 200
    assert "daily spend cap reached" in resp.headers["X-Spend-Warning"]


@pytest.mark.asyncio
async def test_cached_tokens_in_metrics(monkeypatch):
    from forge.proxy import server

    _stub_server(
        monkeypatch,
        server,
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cached_tokens": 600,
        },
    )

    await server.create_message(_make_request_data(stream=False), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    assert snap["tokens"]["cached"] == 600
    assert snap["cache_hit_rate"] == 60.0


@pytest.mark.asyncio
async def test_snapshot_json_serializable(monkeypatch):
    from forge.proxy import server

    _stub_server(monkeypatch, server)
    await server.create_message(_make_request_data(stream=False), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    json.dumps(snap)  # Raises if not serializable


@pytest.mark.asyncio
async def test_avg_latency_per_tier(monkeypatch):
    from forge.proxy import server

    _stub_server(monkeypatch, server)
    await server.create_message(_make_request_data(stream=False, tier="sonnet"), _DummyRawRequest())

    snap = proxy_metrics.snapshot()
    assert snap["by_tier"]["sonnet"]["avg_latency_ms"] > 0


def test_reset_isolates_tests():
    """Verify reset() zeroes counters for test isolation."""
    proxy_metrics.record_request(
        tier="sonnet",
        model="openai/gpt-5.5",
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
        latency_ms=100,
        streaming=False,
        failed=False,
    )
    assert proxy_metrics.total_requests == 1
    proxy_metrics.reset()
    assert proxy_metrics.total_requests == 0
    assert proxy_metrics.snapshot()["total_requests"] == 0
