"""Unit tests for proxy routing invariants.

Routing invariants (B2.1):
- Default routing tier must be proxy-owned (config.proxy.default_tier).
- Session state must not influence routing defaults.
- If proxy.default_tier is missing and request has no explicit tier, requests fail fast (configuration error).

We validate the tier resolution logic for:
- create_message (POST /v1/messages)
- count_tokens (POST /v1/messages/count_tokens)

We keep these tests lightweight by stubbing provider conversion + LLM client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class DummyRequestState:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id


class DummyRequest:
    def __init__(self, request_id: str) -> None:
        self.state = DummyRequestState(request_id)


class DummyAnthropicResponse:
    def model_dump(self) -> dict:
        return {"content": []}


class DummyMessagesRequest:
    def __init__(self, **_kwargs) -> None:
        # The proxy's convert_anthropic_to_openai stub doesn't need fields.
        pass


@pytest.mark.asyncio
async def test_create_message_request_explicit_tier_wins(monkeypatch):
    from forge.proxy import server

    # Avoid real reload/config reloading
    monkeypatch.setattr(server, "reload", lambda: None)

    async def _fake_get_client(*args, **kwargs):
        client = AsyncMock()
        client.create_completion = AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]})
        client.create_streaming_completion = AsyncMock()
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _fake_get_client)

    # Stub config objects (must provide fields used by other code paths)
    class ProxyCfg:
        default_tier = "haiku"
        preferred_provider = "openai"
        gemini = type(
            "G",
            (),
            {"tiers": type("T", (), {"haiku": "h", "sonnet": "s", "opus": "o"})()},
        )()

        @staticmethod
        def get_model_for_tier(_tier: str) -> str:
            return "openai/gpt-4o-mini"

    class SessionCfg:
        default_tier = "opus"

    monkeypatch.setattr(server.config, "proxy", ProxyCfg())
    monkeypatch.setattr(server.config, "session", SessionCfg())

    # Stub map_model_name — these tests verify tier resolution, not model mapping
    monkeypatch.setattr(server, "map_model_name", lambda v: v)

    # Minimal request_data stand-in
    request_data = type(
        "Req",
        (),
        {
            "has_explicit_tier": True,
            "tier": "opus",
            "stream": False,
            "messages": [],
            "tools": None,
            "system": None,
            "temperature": None,
            "max_tokens": 1,
            "top_p": None,
            "stop_sequences": None,
            "original_model_name": "claude-opus",
            "model": "claude-opus",
            "model_dump": lambda self=None: {},
        },
    )()

    raw_request = DummyRequest("req_test")

    # convert_* are heavy; stub them
    monkeypatch.setattr(server, "convert_anthropic_to_openai", lambda *a, **k: {"messages": []})
    monkeypatch.setattr(server, "convert_openai_to_anthropic", lambda *a, **k: DummyAnthropicResponse())
    monkeypatch.setattr(server, "_check_client_tool_failures", AsyncMock())
    monkeypatch.setattr(
        server.client_factory,
        "detect_provider_for_model",
        lambda *_: type("E", (), {"value": "openai"})(),
    )

    # Avoid real loggers/utilities during test
    monkeypatch.setattr(server, "log_request_response", AsyncMock())
    monkeypatch.setattr(server, "log_request_beautifully", lambda *a, **k: None)
    monkeypatch.setattr(server, "log_tool_event", lambda *a, **k: None)

    resp = await server.create_message(request_data, raw_request)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_count_tokens_uses_proxy_default_tier(monkeypatch):
    from forge.proxy import server

    monkeypatch.setattr(server, "reload", lambda: None)

    async def _fake_get_client(*args, **kwargs):
        client = AsyncMock()
        client.count_tokens = AsyncMock(return_value=123)
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _fake_get_client)

    class ProxyCfg:
        default_tier = "haiku"
        preferred_provider = "openai"
        gemini = type(
            "G",
            (),
            {"tiers": type("T", (), {"haiku": "h", "sonnet": "s", "opus": "o"})()},
        )()

    class SessionCfg:
        default_tier = "opus"

    monkeypatch.setattr(server.config, "proxy", ProxyCfg())
    monkeypatch.setattr(server.config, "session", SessionCfg())

    # Stub map_model_name — these tests verify tier resolution, not model mapping
    monkeypatch.setattr(server, "map_model_name", lambda v: v)

    # Ensure data_models.map_model_name doesn't blow up during simulated MessagesRequest creation
    monkeypatch.setattr(server, "MessagesRequest", DummyMessagesRequest)

    # Minimal TokenCountRequest stand-in
    request_data = type(
        "TokReq",
        (),
        {
            "original_model_name": "claude-sonnet",
            "model": "claude-sonnet",
            "messages": [],
            "system": None,
            "tier": None,
            "has_explicit_tier": False,
        },
    )()

    raw_request = DummyRequest("tok_test")

    monkeypatch.setattr(server, "convert_anthropic_to_openai", lambda *a, **k: {"messages": []})
    monkeypatch.setattr(
        server.client_factory,
        "detect_provider_for_model",
        lambda *_: type("E", (), {"value": "openai"})(),
    )

    resp = await server.count_tokens(request_data, raw_request)
    assert resp.status_code == 200

    assert request_data.tier == "haiku"


@pytest.mark.asyncio
async def test_count_tokens_fails_when_proxy_default_tier_missing(monkeypatch):
    from forge.proxy import server

    monkeypatch.setattr(server, "reload", lambda: None)

    async def _fake_get_client(*args, **kwargs):
        client = AsyncMock()
        client.count_tokens = AsyncMock(return_value=123)
        return client

    monkeypatch.setattr(server.client_factory, "get_client", _fake_get_client)

    class ProxyCfg:
        default_tier = ""  # force missing
        preferred_provider = "openai"
        gemini = type(
            "G",
            (),
            {"tiers": type("T", (), {"haiku": "h", "sonnet": "s", "opus": "o"})()},
        )()

    class SessionCfg:
        default_tier = "opus"  # No longer used as fallback

    monkeypatch.setattr(server.config, "proxy", ProxyCfg())
    monkeypatch.setattr(server.config, "session", SessionCfg())

    # Stub map_model_name — these tests verify tier resolution, not model mapping
    monkeypatch.setattr(server, "map_model_name", lambda v: v)

    monkeypatch.setattr(server, "MessagesRequest", DummyMessagesRequest)

    request_data = type(
        "TokReq",
        (),
        {
            "original_model_name": "claude-sonnet",
            "model": "claude-sonnet",
            "messages": [],
            "system": None,
            "tier": None,
            "has_explicit_tier": False,
        },
    )()

    raw_request = DummyRequest("tok_test")

    monkeypatch.setattr(server, "convert_anthropic_to_openai", lambda *a, **k: {"messages": []})
    monkeypatch.setattr(
        server.client_factory,
        "detect_provider_for_model",
        lambda *_: type("E", (), {"value": "openai"})(),
    )

    with pytest.raises(Exception):
        await server.count_tokens(request_data, raw_request)


def test_model_mapping_uses_fresh_config(monkeypatch):
    """Model mapping happens at call time, not at Pydantic construction time.

    With field validators removed, MessagesRequest.model holds the raw client string.
    map_model_name() is called explicitly in the handler, so config changes between
    construction and mapping are reflected.
    """
    from forge.config import config as config_proxy
    from forge.proxy.data_models import MessagesRequest, map_model_name

    # Helper to build a ProxyCfg stub that maps haiku to a given model name
    def _make_proxy_cfg(haiku_model: str):
        class Provider:
            tiers = type(
                "T",
                (),
                {"haiku": haiku_model, "sonnet": haiku_model, "opus": haiku_model},
            )()

        class ProxyCfg:
            preferred_provider = "openai"
            gemini = type(
                "G",
                (),
                {"tiers": type("T", (), {"haiku": "h", "sonnet": "s", "opus": "o"})()},
            )()

            @staticmethod
            def get_provider(_name):
                return Provider()

        return ProxyCfg()

    # Stub config A: maps haiku → model-a (uses same pattern as existing tests)
    monkeypatch.setattr(config_proxy, "proxy", _make_proxy_cfg("model-a"))

    # Build request — model field should be the raw string (no validator mapping)
    req = MessagesRequest(model="claude-3-5-haiku-20241022", max_tokens=1, messages=[])
    assert req.model == "claude-3-5-haiku-20241022"  # Raw, not mapped
    assert req.original_model_name == "claude-3-5-haiku-20241022"

    # Map using config A
    result_a = map_model_name(req.model)
    assert result_a == "model-a"

    # Stub config B: maps haiku → model-b
    monkeypatch.setattr(config_proxy, "proxy", _make_proxy_cfg("model-b"))

    # Same request, different config → different mapping
    result_b = map_model_name(req.model)
    assert result_b == "model-b"

    # The key invariant: same input, different config → different output
    assert result_a != result_b


# ---------------------------------------------------------------------------
# OpenRouter model name mapping
# ---------------------------------------------------------------------------


class TestOpenRouterModelMapping:
    """Tests for map_model_name() with preferred_provider=openrouter."""

    def test_slash_id_passed_through(self, monkeypatch):
        """OpenRouter model IDs (provider/model) pass through unchanged."""
        from forge.config import config as config_proxy
        from forge.proxy.data_models import map_model_name

        class ORProvider:
            tiers = type(
                "T",
                (),
                {
                    "haiku": "anthropic/claude-haiku-4.5",
                    "sonnet": "anthropic/claude-sonnet-4.6",
                    "opus": "anthropic/claude-opus-4.6",
                },
            )()

        class ProxyCfg:
            preferred_provider = "openrouter"

            @staticmethod
            def get_provider(_name=None):
                return ORProvider()

        monkeypatch.setattr(config_proxy, "proxy", ProxyCfg())

        assert map_model_name("google/gemini-2.5-pro") == "google/gemini-2.5-pro"
        assert map_model_name("anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"
        assert map_model_name("meta-llama/llama-3.1-70b") == "meta-llama/llama-3.1-70b"

    def test_anthropic_flavor_maps_to_openrouter_tier(self, monkeypatch):
        """Anthropic-style model names map to OpenRouter tier models."""
        from forge.config import config as config_proxy
        from forge.proxy.data_models import map_model_name

        class ORProvider:
            tiers = type(
                "T",
                (),
                {
                    "haiku": "anthropic/claude-haiku-4.5",
                    "sonnet": "anthropic/claude-sonnet-4.6",
                    "opus": "anthropic/claude-opus-4.6",
                },
            )()

        class ProxyCfg:
            preferred_provider = "openrouter"

            @staticmethod
            def get_provider(_name=None):
                return ORProvider()

        monkeypatch.setattr(config_proxy, "proxy", ProxyCfg())

        assert map_model_name("claude-3-5-sonnet") == "anthropic/claude-sonnet-4.6"
        assert map_model_name("claude-3-5-haiku-20241022") == "anthropic/claude-haiku-4.5"
