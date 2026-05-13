"""Tests for credential manager."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from forge.core.llm.credentials import CredentialManager
from forge.core.llm.errors import NoApiKeyError


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton between tests."""
    CredentialManager.reset_default()
    yield
    CredentialManager.reset_default()


@pytest.fixture
def mock_config():
    """Mock unified config to return base_urls from templates.

    Templates now define base_url (no env var fallback).
    We patch the internal helpers to return template-like values.
    """
    from forge.core.llm import credentials

    def _patched_remote_base_url() -> str:
        # Remote templates define base_url (litellm-openai, litellm-gemini)
        return "https://litellm.example.com"

    def _patched_local_base_url() -> str:
        # Local templates define base_url (litellm-gemini-local: port 4000, litellm-gemini-test: port 4001)
        return "http://localhost:4000"

    with (
        patch.object(credentials, "_get_litellm_remote_base_url", _patched_remote_base_url),
        patch.object(credentials, "_get_litellm_local_base_url", _patched_local_base_url),
    ):
        yield


class TestCredentialManagerSingleton:
    """Tests for singleton pattern (sync, no async marker)."""

    def test_default_returns_same_instance(self):
        cm1 = CredentialManager.default()
        cm2 = CredentialManager.default()
        assert cm1 is cm2

    def test_new_instance_is_different(self):
        cm1 = CredentialManager.default()
        cm2 = CredentialManager()  # New instance, not using default()
        assert cm1 is not cm2

    def test_reset_default_clears_instance(self):
        cm1 = CredentialManager.default()
        CredentialManager.reset_default()
        cm2 = CredentialManager.default()
        assert cm1 is not cm2


@pytest.mark.asyncio
class TestLiteLLMRemoteCredentials:
    """Tests for remote LiteLLM credentials."""

    @pytest.fixture
    def env_vars(self, mock_config):
        """Set up environment variables for remote LiteLLM.

        Uses mock_config (returns template base_url).
        Only API key is a secret in env vars.
        """
        env = {"LITELLM_API_KEY": "test-api-key"}
        with patch.dict(os.environ, env):
            yield

    async def test_get_remote_credentials(self, env_vars):
        cm = CredentialManager()
        creds = await cm.get_credentials("litellm_remote")

        # Base URL comes from template (mock_config)
        assert creds["base_url"] == "https://litellm.example.com"
        assert creds["api_key"] == "test-api-key"

    async def test_missing_api_key_raises(self, mock_config):
        """Missing API key raises NoApiKeyError (non-localhost)."""
        with patch.dict(os.environ, {}, clear=True):
            cm = CredentialManager()
            with pytest.raises(NoApiKeyError) as exc_info:
                await cm.get_credentials("litellm_remote")
            assert "LITELLM_API_KEY" in str(exc_info.value)

    async def test_localhost_allows_no_api_key(self, mock_config):
        """Localhost doesn't require API key."""
        # Mock local base_url for this test
        from forge.core.llm import credentials

        with patch.object(
            credentials,
            "_get_litellm_remote_base_url",
            return_value="http://localhost:4000",
        ):
            with patch.dict(os.environ, {"LITELLM_API_KEY": ""}, clear=True):
                cm = CredentialManager()
                creds = await cm.get_credentials("litellm_remote")
                assert creds["base_url"] == "http://localhost:4000"
                assert creds["api_key"] == ""

    async def test_default_base_url(self, mock_config):
        """Uses base_url from template (mock_config)."""
        with patch.dict(os.environ, {"LITELLM_API_KEY": "test-key"}, clear=True):
            cm = CredentialManager()
            creds = await cm.get_credentials("litellm_remote")
            assert creds["base_url"] == "https://litellm.example.com"


@pytest.mark.asyncio
class TestLiteLLMLocalCredentials:
    """Tests for local LiteLLM credentials."""

    @pytest.fixture
    def env_vars(self, mock_config):
        """Set up environment variables for local LiteLLM.

        Base URL comes from template (mock_config).
        Only API key is a secret in env vars.
        """
        with patch.dict(os.environ, {"LITELLM_LOCAL_API_KEY": "local-key"}):
            yield

    async def test_get_local_credentials(self, env_vars):
        cm = CredentialManager()
        creds = await cm.get_credentials("litellm_local")

        # Base URL comes from template (mock_config)
        assert creds["base_url"] == "http://localhost:4000"
        assert creds["api_key"] == "local-key"

    async def test_api_key_defaults_to_not_needed(self, mock_config):
        """API key defaults to 'not-needed' for local."""
        with patch.dict(os.environ, {}, clear=True):
            cm = CredentialManager()
            creds = await cm.get_credentials("litellm_local")
            assert creds["api_key"] == "not-needed"


@pytest.mark.asyncio
class TestCredentialCaching:
    """Tests for credential caching."""

    @pytest.fixture
    def env_vars(self, mock_config):
        """Set up environment variables.

        Base URL comes from template (mock_config).
        Only API key is a secret in env vars.
        """
        with patch.dict(os.environ, {"LITELLM_API_KEY": "test-api-key"}):
            yield

    async def test_credentials_are_cached(self, env_vars):
        """Same credentials returned from cache."""
        cm = CredentialManager()

        creds1 = await cm.get_credentials("litellm_remote")
        creds2 = await cm.get_credentials("litellm_remote")

        # Should be the same dict object (from cache)
        assert creds1 is creds2

    async def test_cache_status(self, env_vars):
        """Cache status reports correct information."""
        cm = CredentialManager()

        # Initially empty
        status = cm.get_cache_status()
        assert status["providers"] == {}

        # After fetching
        await cm.get_credentials("litellm_remote")
        status = cm.get_cache_status()
        assert "litellm_remote" in status["providers"]
        assert status["providers"]["litellm_remote"]["expired"] is False

    async def test_invalidate_clears_cache(self, env_vars):
        """Invalidate removes credentials from cache."""
        cm = CredentialManager()

        await cm.get_credentials("litellm_remote")
        assert "litellm_remote" in cm.get_cache_status()["providers"]

        await cm.invalidate("litellm_remote")
        assert "litellm_remote" not in cm.get_cache_status()["providers"]

    async def test_clear_cache(self, env_vars):
        """Clear cache removes all credentials."""
        cm = CredentialManager()
        # Manually add to cache for test
        cm._cache["test"] = ({}, 0, 3600)

        cm.clear_cache()
        assert cm._cache == {}


@pytest.mark.asyncio
class TestAnthropicCredentials:
    """Tests for Anthropic credentials (deferred implementation)."""

    async def test_missing_api_key_raises(self):
        """Missing API key raises NoApiKeyError."""
        with patch.dict(os.environ, {}, clear=True):
            cm = CredentialManager()
            with pytest.raises(NoApiKeyError) as exc_info:
                await cm.get_credentials("anthropic")
            msg = str(exc_info.value)
            detail = exc_info.value.detail or ""
            assert "ANTHROPIC_API_KEY" in msg
            assert "forge auth login -c anthropic-api" in msg
            assert "NOT needed for:" in msg
            assert "--subprocess-proxy" in msg
            assert "forge auth login -c anthropic-api" in detail
            assert "--subprocess-proxy" in detail

    async def test_with_api_key(self):
        """Returns credentials when API key is set."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            cm = CredentialManager()
            creds = await cm.get_credentials("anthropic")
            assert creds["api_key"] == "sk-ant-test"


@pytest.mark.asyncio
class TestOpenRouterCredentials:
    """Tests for OpenRouter credentials."""

    async def test_missing_api_key_raises(self, mock_config):
        """Missing API key raises NoApiKeyError."""
        with patch.dict(os.environ, {}, clear=True):
            cm = CredentialManager()
            with pytest.raises(NoApiKeyError) as exc_info:
                await cm.get_credentials("openrouter")
            msg = str(exc_info.value)
            detail = exc_info.value.detail or ""
            assert "OPENROUTER_API_KEY" in msg
            assert "forge auth login -c openrouter" in msg
            assert "openrouter.ai/keys" in msg
            assert "forge auth login -c openrouter" in detail
            assert "openrouter.ai/keys" in detail

    async def test_with_api_key_uses_default_base_url(self, mock_config):
        """Returns credentials with default base URL when only API key is set."""
        from forge.core.llm import credentials

        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=True),
            patch.object(credentials, "_get_openrouter_base_url", return_value="https://openrouter.ai/api/v1"),
        ):
            cm = CredentialManager()
            creds = await cm.get_credentials("openrouter")
            assert creds["api_key"] == "sk-or-test"
            assert creds["base_url"] == "https://openrouter.ai/api/v1"
            assert "X-OpenRouter-Title" in creds["extra_headers"]
            assert creds["extra_headers"]["X-OpenRouter-Title"] == "Claude Forge"

    async def test_env_base_url_overrides_default(self, mock_config):
        """OPENROUTER_BASE_URL env var overrides the default."""
        env = {
            "OPENROUTER_API_KEY": "sk-or-test",
            "OPENROUTER_BASE_URL": "https://custom-or.example.com/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            from forge.core.llm.credentials import _get_openrouter_base_url

            url = _get_openrouter_base_url()
            assert url == "https://custom-or.example.com/v1"

    async def test_config_base_url_takes_precedence(self, mock_config):
        """Template config base_url wins over env var."""
        from forge.core.llm import credentials

        with (
            patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "sk-or-test", "OPENROUTER_BASE_URL": "https://env.example.com"},
                clear=True,
            ),
            patch.object(credentials, "_get_openrouter_base_url", return_value="https://config.example.com/v1"),
        ):
            cm = CredentialManager()
            creds = await cm.get_credentials("openrouter")
            assert creds["base_url"] == "https://config.example.com/v1"


# --- Phase 3: File-based credential chain tests ---


def _write_creds(path: Path, profiles: dict[str, dict[str, str]]) -> None:
    """Write a credentials.yaml for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"version": 1, "profiles": profiles}, f)


class TestDefaultUsesChain:
    """Verify CredentialManager.default() wires the EnvSecretsProvider → FileSecretsProvider chain."""

    def test_default_secrets_is_chain(self):
        from forge.core.auth import ChainSecretsProvider

        cm = CredentialManager.default()
        assert isinstance(cm._secrets, ChainSecretsProvider)

    def test_default_chain_has_two_providers(self):
        from forge.core.auth import EnvSecretsProvider
        from forge.core.auth.secrets import FileSecretsProvider

        cm = CredentialManager.default()
        providers = cm._secrets._providers
        assert len(providers) == 2
        assert isinstance(providers[0], EnvSecretsProvider)
        assert isinstance(providers[1], FileSecretsProvider)


@pytest.mark.asyncio
class TestAnthropicFromFile:
    """Anthropic key resolved from credentials file when not in env."""

    async def test_anthropic_from_file(self, tmp_path: Path, mock_config):
        from forge.core.auth import ChainSecretsProvider, EnvSecretsProvider
        from forge.core.auth.secrets import FileSecretsProvider

        creds_file = tmp_path / "credentials.yaml"
        _write_creds(creds_file, {"default": {"ANTHROPIC_API_KEY": "sk-ant-from-file"}})

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds_file),
        )
        cm = CredentialManager(secrets=secrets)

        with patch.dict(os.environ, {}, clear=True):
            result = await cm.get_credentials("anthropic")
            assert result["api_key"] == "sk-ant-from-file"

    async def test_env_overrides_file_for_anthropic(self, tmp_path: Path, mock_config):
        from forge.core.auth import ChainSecretsProvider, EnvSecretsProvider
        from forge.core.auth.secrets import FileSecretsProvider

        creds_file = tmp_path / "credentials.yaml"
        _write_creds(creds_file, {"default": {"ANTHROPIC_API_KEY": "sk-ant-from-file"}})

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds_file),
        )
        cm = CredentialManager(secrets=secrets)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-from-env"}):
            result = await cm.get_credentials("anthropic")
            assert result["api_key"] == "sk-ant-from-env"


@pytest.mark.asyncio
class TestLiteLLMFromFile:
    """LiteLLM keys resolved from credentials file when not in env."""

    async def test_remote_key_from_file(self, tmp_path: Path, mock_config):
        from forge.core.auth import ChainSecretsProvider, EnvSecretsProvider
        from forge.core.auth.secrets import FileSecretsProvider

        creds_file = tmp_path / "credentials.yaml"
        _write_creds(creds_file, {"default": {"LITELLM_API_KEY": "sk-litellm-from-file"}})

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds_file),
        )
        cm = CredentialManager(secrets=secrets)

        with patch.dict(os.environ, {}, clear=True):
            result = await cm.get_credentials("litellm_remote")
            assert result["api_key"] == "sk-litellm-from-file"

    async def test_local_key_from_file(self, tmp_path: Path, mock_config):
        from forge.core.auth import ChainSecretsProvider, EnvSecretsProvider
        from forge.core.auth.secrets import FileSecretsProvider

        creds_file = tmp_path / "credentials.yaml"
        _write_creds(creds_file, {"default": {"LITELLM_LOCAL_API_KEY": "sk-local-from-file"}})

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds_file),
        )
        cm = CredentialManager(secrets=secrets)

        with patch.dict(os.environ, {}, clear=True):
            result = await cm.get_credentials("litellm_local")
            assert result["api_key"] == "sk-local-from-file"


class TestLiteLLMRemoteBaseUrlResolution:
    """Regression: _get_litellm_remote_base_url credential-file fallback."""

    def test_config_empty_env_missing_credential_file_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """base_url resolved from credential file when config and env are empty."""
        from forge.core.llm.credentials import _get_litellm_remote_base_url

        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        # Lazy import inside function reads forge.config.config — mock it at source
        mock_cfg = type("C", (), {"proxy": type("P", (), {"litellm": type("L", (), {"base_url": ""})()})()})()
        with (
            patch("forge.config.get_config", return_value=mock_cfg),
            patch("forge.config._config", mock_cfg),
            patch(
                "forge.core.auth.template_secrets._get_file_secrets",
                return_value={"LITELLM_BASE_URL": "https://from-cred-file.example.com"},
            ),
        ):
            result = _get_litellm_remote_base_url()
        assert result == "https://from-cred-file.example.com"

    def test_all_empty_raises_with_auth_guidance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Error message mentions forge auth login when nothing is configured."""
        from forge.core.llm.credentials import _get_litellm_remote_base_url

        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        mock_cfg = type("C", (), {"proxy": type("P", (), {"litellm": type("L", (), {"base_url": ""})()})()})()
        with (
            patch("forge.config.get_config", return_value=mock_cfg),
            patch("forge.config._config", mock_cfg),
            patch(
                "forge.core.auth.template_secrets._get_file_secrets",
                return_value={},
            ),
        ):
            with pytest.raises(ValueError, match="forge auth login"):
                _get_litellm_remote_base_url()
