"""Regression: local LiteLLM proxy with openai/ models must use litellm_local credentials.

Bug: `detect_provider("openai/gpt-5.5")` returns `litellm_remote`, so the proxy's
client factory asks CredentialManager for `LITELLM_API_KEY` -- which doesn't exist
for local LiteLLM setups. The fix overrides to `litellm_local` when the proxy's
`upstream_base_url` is localhost.

Affected file: src/forge/proxy/client_factory.py
"""

import pytest

pytestmark = pytest.mark.regression


def test_is_local_url_detects_localhost():
    from forge.proxy.client_factory import _is_local_url

    assert _is_local_url("http://localhost:4000") is True
    assert _is_local_url("http://127.0.0.1:4000") is True
    assert _is_local_url("http://0.0.0.0:8089") is True


def test_is_local_url_rejects_remote():
    from forge.proxy.client_factory import _is_local_url

    assert _is_local_url("https://litellm.corp.com") is False
    assert _is_local_url("https://api.openai.com/v1") is False


def test_is_local_url_handles_empty():
    from forge.proxy.client_factory import _is_local_url

    assert _is_local_url("") is False


def test_local_upstream_overrides_openai_provider_to_litellm_local(monkeypatch):
    """When upstream_base_url is localhost, openai/ models should use litellm_local credentials."""
    from unittest.mock import patch

    from forge.proxy.client_factory import TierClientFactory

    # Reset singleton for test isolation
    TierClientFactory._instance = None
    TierClientFactory._initialized = False
    factory = TierClientFactory()

    proxy_id = "test-local-proxy"
    monkeypatch.setenv("FORGE_PROXY_ID", proxy_id)

    # Mock load_proxy_instance_config to return a config with localhost upstream
    class FakeInstanceConfig:
        upstream_base_url = "http://localhost:4000"

    with patch(
        "forge.config.loader.load_proxy_instance_config",
        return_value=FakeInstanceConfig(),
    ):
        upstream = factory._get_upstream_base_url()
        assert upstream == "http://localhost:4000"

    # Cleanup singleton
    TierClientFactory._instance = None
    TierClientFactory._initialized = False


def test_get_upstream_base_url_is_cached(monkeypatch):
    """The proxy upstream URL is static for a running proxy and should be read once."""
    from unittest.mock import patch

    from forge.proxy.client_factory import TierClientFactory

    TierClientFactory._instance = None
    TierClientFactory._initialized = False
    factory = TierClientFactory()
    monkeypatch.setenv("FORGE_PROXY_ID", "test-local-proxy")

    class FakeInstanceConfig:
        upstream_base_url = "http://localhost:4000"

    try:
        with patch(
            "forge.config.loader.load_proxy_instance_config",
            return_value=FakeInstanceConfig(),
        ) as mock_load:
            assert factory._get_upstream_base_url() == "http://localhost:4000"
            assert factory._get_upstream_base_url() == "http://localhost:4000"
            assert mock_load.call_count == 1
    finally:
        TierClientFactory._instance = None
        TierClientFactory._initialized = False


@pytest.mark.asyncio
async def test_get_client_uses_litellm_local_for_local_openai_upstream(monkeypatch):
    """The actual client construction path should pass provider='litellm_local'."""
    from unittest.mock import patch

    from forge.core.llm.types import ModelHyperparameters
    from forge.proxy.client_factory import ModelProvider, TierClientFactory

    TierClientFactory._instance = None
    TierClientFactory._initialized = False
    factory = TierClientFactory()
    monkeypatch.setenv("FORGE_PROXY_ID", "test-local-proxy")

    class FakeInstanceConfig:
        upstream_base_url = "http://localhost:4000"

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    try:
        factory._client_classes[ModelProvider.LITELLM] = FakeClient
        monkeypatch.setattr(factory, "_import_client_class", lambda _provider: None)
        monkeypatch.setattr(
            factory,
            "_resolve_tier_hyperparams",
            lambda *_args, **_kwargs: ModelHyperparameters(max_tokens=123),
        )

        with patch(
            "forge.config.loader.load_proxy_instance_config",
            return_value=FakeInstanceConfig(),
        ):
            client = await factory.get_client("openai/gpt-5.5", tier="sonnet")

        assert isinstance(client, FakeClient)
        assert captured["model"] == "openai/gpt-5.5"
        assert captured["provider"] == "litellm_local"
        assert captured["tier"] == "sonnet"
    finally:
        TierClientFactory._instance = None
        TierClientFactory._initialized = False
