"""Regression tests for spend cap bugs.

These pin the fixes from the spend cap stabilization work:
- proxy.yaml costs must flow into runtime ProxyConfig,
- YAML/CLI numeric strings must be coerced,
- monthly rollover must clear stale totals before cap checks,
- strict mode must include projected request cost,
- warn mode must allow requests while surfacing a warning header.
"""

from __future__ import annotations

import textwrap
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.regression


def test_bug_caps_config_load_flows_yaml_costs_to_runtime_proxy_config(tmp_path, monkeypatch):
    """proxy.yaml costs must reach the loaded runtime ProxyConfig."""
    from forge.config import load_config

    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    proxy_dir = tmp_path / "proxies" / "cost-proxy"
    proxy_dir.mkdir(parents=True)
    (proxy_dir / "proxy.yaml").write_text(
        textwrap.dedent("""
            proxy_format: 1
            template: litellm-openai
            template_digest: sha256:test
            provider: litellm
            proxy_endpoint: http://localhost:8085
            port: 8085
            upstream_base_url: https://litellm.test.example.com
            tiers:
              haiku: openai/gpt-5-mini
              sonnet: openai/gpt-5.5
              opus: openai/gpt-5.5
            costs:
              caps:
                per_day: 20.00
                per_month: 100.00
              cap_mode: strict
              on_cap_hit: warn
            """),
        encoding="utf-8",
    )

    config = load_config(proxy_id="cost-proxy")

    assert config.proxy.costs.caps.per_day == 20.0
    assert config.proxy.costs.caps.per_month == 100.0
    assert config.proxy.costs.cap_mode == "strict"
    assert config.proxy.costs.on_cap_hit == "warn"


def test_bug_caps_numeric_strings_coerce_in_proxy_instance_config():
    """String USD caps from CLI/YAML must become positive floats."""
    from forge.config.loader import load_proxy_instance_config_from_dict

    config = load_proxy_instance_config_from_dict(
        {
            "proxy_format": 1,
            "template": "litellm-openai",
            "template_digest": "sha256:test",
            "provider": "litellm",
            "proxy_endpoint": "http://localhost:8085",
            "port": 8085,
            "upstream_base_url": "https://litellm.test.example.com",
            "tiers": {
                "haiku": "openai/gpt-5-mini",
                "sonnet": "openai/gpt-5.5",
                "opus": "openai/gpt-5.5",
            },
            "costs": {"caps": {"per_day": "20.00", "per_month": "100.00"}},
        }
    )

    assert config.costs.caps.per_day == 20.0
    assert config.costs.caps.per_month == 100.0


def test_bug_caps_monthly_rollover_clears_stale_total_before_rejecting():
    """A long-running proxy must not reject a new month based on stale spend."""
    from forge.proxy.cost_tracker import CostTracker

    tracker = CostTracker(monthly_cap_usd=1.00)
    tracker._monthly_key = "1999-01"
    tracker._monthly_total = 2_000_000

    result = tracker.check_cap()

    assert not result.exceeded
    assert tracker.monthly_spend_micros() == 0


def test_bug_caps_strict_mode_includes_projected_request_cost():
    """Strict mode must reject when current spend plus estimate reaches the cap."""
    from forge.proxy.cost_tracker import CostTracker

    tracker = CostTracker(daily_cap_usd=1.00, cap_mode="strict")
    tracker.record(800_000)

    result = tracker.check_cap(projected_cost_micros=300_000)

    assert result.exceeded
    assert result.projected
    assert result.cap_type == "daily"


class _DummyRequestState:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id


class _DummyRawRequest:
    def __init__(self, request_id: str = "req_warn") -> None:
        self.state = _DummyRequestState(request_id)


class _DummyAnthropicResponse:
    def model_dump(self) -> dict[str, Any]:
        return {"content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}


def _make_request_data() -> Any:
    return type(
        "Req",
        (),
        {
            "has_explicit_tier": True,
            "tier": "sonnet",
            "stream": False,
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


def _stub_server(monkeypatch, server) -> None:
    monkeypatch.setattr(server, "reload", lambda: None)
    monkeypatch.setattr(server, "log_request_response", AsyncMock())
    monkeypatch.setattr(server, "log_request_beautifully", lambda *a, **k: None)
    monkeypatch.setattr(server, "log_tool_event", lambda *a, **k: None)
    monkeypatch.setattr(server, "_check_client_tool_failures", AsyncMock())
    monkeypatch.setattr(server, "map_model_name", lambda v: v)
    monkeypatch.setattr(server, "convert_anthropic_to_openai", lambda *a, **k: {"messages": []})
    monkeypatch.setattr(server, "convert_openai_to_anthropic", lambda *a, **k: _DummyAnthropicResponse())
    monkeypatch.setattr(
        server.client_factory, "detect_provider_for_model", lambda *_: type("E", (), {"value": "openai"})()
    )

    class ProxyCfg:
        default_tier = "sonnet"
        preferred_provider = "openai"

        @staticmethod
        def get_model_for_tier(_tier: str) -> str:
            return "openai/gpt-5.5"

    monkeypatch.setattr(server.config, "proxy", ProxyCfg())

    async def _fake_get_client(*args, **kwargs):
        client = AsyncMock()
        client.create_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "cached_tokens": 0,
                },
            }
        )
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _fake_get_client)


@pytest.mark.asyncio
async def test_bug_caps_warn_mode_surfaces_header_without_rejecting(monkeypatch):
    """Warn mode must allow the request and return X-Spend-Warning."""
    from forge.proxy import server
    from forge.proxy.cost_tracker import CostTracker

    _stub_server(monkeypatch, server)
    tracker = CostTracker(daily_cap_usd=0.001, on_cap_hit="warn")
    tracker.record(2_000)
    monkeypatch.setattr(server, "cost_tracker", tracker)

    response = await server.create_message(_make_request_data(), _DummyRawRequest())

    assert response.status_code == 200
    assert "daily spend cap reached" in response.headers["X-Spend-Warning"]
