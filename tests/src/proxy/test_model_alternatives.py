"""Tests for model_alternatives proxy routing."""

import pytest

from forge.proxy import server


@pytest.fixture(autouse=True)
def _ensure_runtime(monkeypatch):
    """Stub runtime state so server helpers can run."""
    monkeypatch.setattr(server, "reload", lambda: None)

    class ProviderCfg:
        tiers = type("T", (), {"haiku": "h-model", "sonnet": "s-model", "opus": "o-model"})()
        model_alternatives = {
            "opus": {
                "claude-opus-4-7": "anthropic/claude-opus-4.7",
            },
        }

    class ProxyCfg:
        default_tier = "sonnet"
        preferred_provider = "openrouter"
        _provider = ProviderCfg()

        def get_model_for_tier(self, tier: str) -> str:
            return getattr(self._provider.tiers, tier, "s-model")

        def get_provider(self, name=None):
            return self._provider

    monkeypatch.setattr(server.config, "proxy", ProxyCfg())


class TestResolveModelWithAlternatives:
    """Tests for _resolve_model_with_alternatives shared helper."""

    def test_routes_to_alternative_when_matched(self):
        result = server._resolve_model_with_alternatives("opus", "claude-opus-4-7", "o-model")
        assert result == "anthropic/claude-opus-4.7"

    def test_routes_to_fallback_when_no_match(self):
        result = server._resolve_model_with_alternatives("opus", "claude-opus-4-6", "o-model")
        assert result == "o-model"

    def test_routes_to_fallback_when_no_original_model(self):
        result = server._resolve_model_with_alternatives("opus", None, "o-model")
        assert result == "o-model"

    def test_routes_to_fallback_for_tier_without_alternatives(self):
        result = server._resolve_model_with_alternatives("sonnet", "claude-sonnet-4-6", "s-model")
        assert result == "s-model"

    def test_strips_1m_suffix_before_lookup(self):
        result = server._resolve_model_with_alternatives("opus", "claude-opus-4-7[1m]", "o-model")
        assert result == "anthropic/claude-opus-4.7"

    def test_provider_error_degrades_to_fallback(self, monkeypatch):
        def _broken_provider(name=None):
            raise RuntimeError("config unavailable")

        proxy_cfg = server.config.proxy
        monkeypatch.setattr(proxy_cfg, "get_provider", _broken_provider)
        result = server._resolve_model_with_alternatives("opus", "claude-opus-4-7", "o-model")
        assert result == "o-model"
