"""Tests for SecretsProvider implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from forge.core.auth.secrets import (
    ChainSecretsProvider,
    ConfigSecretsProvider,
    EnvSecretsProvider,
    FileSecretsProvider,
)
from forge.core.llm.errors import NoApiKeyError


class TestEnvSecretsProvider:
    """Tests for EnvSecretsProvider."""

    def test_get_returns_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvSecretsProvider.get() returns value from os.environ."""
        monkeypatch.setenv("TEST_API_KEY", "secret-123")

        provider = EnvSecretsProvider()
        assert provider.get("TEST_API_KEY") == "secret-123"

    def test_get_returns_default_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvSecretsProvider.get() returns default when key is missing."""
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)

        provider = EnvSecretsProvider()
        assert provider.get("NONEXISTENT_KEY", "default-value") == "default-value"

    def test_get_returns_default_when_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvSecretsProvider.get() treats empty string as not-set."""
        monkeypatch.setenv("EMPTY_KEY", "")

        provider = EnvSecretsProvider()
        assert provider.get("EMPTY_KEY", "default") == "default"

    def test_require_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvSecretsProvider.require() returns value when present."""
        monkeypatch.setenv("REQUIRED_KEY", "required-value")

        provider = EnvSecretsProvider()
        assert provider.require("REQUIRED_KEY") == "required-value"

    def test_require_raises_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvSecretsProvider.require() raises NoApiKeyError when key is missing."""
        monkeypatch.delenv("MISSING_KEY", raising=False)

        provider = EnvSecretsProvider()
        with pytest.raises(NoApiKeyError) as exc_info:
            provider.require("MISSING_KEY")

        assert exc_info.value.provider == "env"
        assert exc_info.value.env_var == "MISSING_KEY"

    def test_require_known_key_uses_actionable_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Known credential keys get the shared actionable formatter."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = EnvSecretsProvider(ignore_env=False)
        with pytest.raises(NoApiKeyError) as exc_info:
            provider.require("ANTHROPIC_API_KEY")

        msg = str(exc_info.value)
        assert "forge auth login -c anthropic-api" in msg
        assert "NOT needed for:" in msg

    def test_require_raises_when_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvSecretsProvider.require() raises when value is empty string."""
        monkeypatch.setenv("EMPTY_KEY", "")

        provider = EnvSecretsProvider()
        with pytest.raises(NoApiKeyError):
            provider.require("EMPTY_KEY")


class TestEnvSecretsProviderIgnoreEnv:
    """Tests for EnvSecretsProvider with ignore_env flag."""

    def test_get_returns_default_when_explicit_ignore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KEY", "from-env")
        provider = EnvSecretsProvider(ignore_env=True)
        assert provider.get("TEST_KEY", "fallback") == "fallback"

    def test_get_returns_none_when_explicit_ignore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KEY", "from-env")
        provider = EnvSecretsProvider(ignore_env=True)
        assert provider.get("TEST_KEY") is None

    def test_require_raises_when_explicit_ignore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KEY", "from-env")
        provider = EnvSecretsProvider(ignore_env=True)
        with pytest.raises(NoApiKeyError):
            provider.require("TEST_KEY")

    def test_require_known_key_when_ignored_mentions_env_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")

        provider = EnvSecretsProvider(ignore_env=True)
        with pytest.raises(NoApiKeyError) as exc_info:
            provider.require("ANTHROPIC_API_KEY")

        msg = str(exc_info.value)
        assert "auth_ignore_env" in msg
        assert "ANTHROPIC_API_KEY is set in env" in msg

    def test_chain_falls_through_when_ignore_active(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Chain with ignore_env falls through to FileSecretsProvider."""
        monkeypatch.setenv("TEST_KEY", "from-env")

        cred_file = tmp_path / "credentials.yaml"
        cred_file.write_text(yaml.dump({"version": 1, "profiles": {"default": {"TEST_KEY": "from-file"}}}))

        chain = ChainSecretsProvider(
            EnvSecretsProvider(ignore_env=True),
            FileSecretsProvider(path=cred_file),
        )
        assert chain.get("TEST_KEY") == "from-file"

    def test_lazy_reads_runtime_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default EnvSecretsProvider (ignore_env=None) reads flag lazily."""
        monkeypatch.setenv("TEST_KEY", "from-env")
        provider = EnvSecretsProvider()

        assert provider.get("TEST_KEY") == "from-env"

        monkeypatch.setattr(
            "forge.runtime_config.get_runtime_config",
            lambda: type("C", (), {"auth_ignore_env": True})(),
        )
        assert provider.get("TEST_KEY") is None

    def test_explicit_false_ignores_runtime_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ignore_env=False overrides runtime config."""
        monkeypatch.setenv("TEST_KEY", "from-env")
        monkeypatch.setattr(
            "forge.runtime_config.get_runtime_config",
            lambda: type("C", (), {"auth_ignore_env": True})(),
        )
        provider = EnvSecretsProvider(ignore_env=False)
        assert provider.get("TEST_KEY") == "from-env"


class TestConfigSecretsProvider:
    """Tests for ConfigSecretsProvider."""

    @dataclass
    class MockProviderConfig:
        """Minimal mock for ProviderConfig."""

        auth_url: str = ""

    @dataclass
    class MockProxyConfig:
        """Minimal mock for ProxyConfig."""

        openai: "TestConfigSecretsProvider.MockProviderConfig" = field(
            default_factory=lambda: TestConfigSecretsProvider.MockProviderConfig()
        )
        gemini: "TestConfigSecretsProvider.MockProviderConfig" = field(
            default_factory=lambda: TestConfigSecretsProvider.MockProviderConfig()
        )

    @dataclass
    class MockForgeConfig:
        """Minimal mock for ForgeConfig."""

        proxy: "TestConfigSecretsProvider.MockProxyConfig" = field(
            default_factory=lambda: TestConfigSecretsProvider.MockProxyConfig()
        )

    def test_get_maps_openai_auth_url(self) -> None:
        """ConfigSecretsProvider maps OPENAI_AUTH_URL to config.proxy.openai.auth_url."""
        config = self.MockForgeConfig()
        config.proxy.openai.auth_url = "https://auth.example.com"

        provider = ConfigSecretsProvider(config)  # type: ignore[arg-type]
        assert provider.get("OPENAI_AUTH_URL") == "https://auth.example.com"

    def test_get_maps_gemini_auth_url(self) -> None:
        """ConfigSecretsProvider maps GEMINI_AUTH_URL to config.proxy.gemini.auth_url."""
        config = self.MockForgeConfig()
        config.proxy.gemini.auth_url = "https://gemini-auth.example.com"

        provider = ConfigSecretsProvider(config)  # type: ignore[arg-type]
        assert provider.get("GEMINI_AUTH_URL") == "https://gemini-auth.example.com"

    def test_get_returns_default_for_unknown_key(self) -> None:
        """ConfigSecretsProvider returns default for unmapped keys."""
        config = self.MockForgeConfig()

        provider = ConfigSecretsProvider(config)  # type: ignore[arg-type]
        assert provider.get("UNKNOWN_KEY", "default") == "default"

    def test_get_returns_default_when_config_value_empty(self) -> None:
        """ConfigSecretsProvider treats empty string as not-set."""
        config = self.MockForgeConfig()
        config.proxy.openai.auth_url = ""

        provider = ConfigSecretsProvider(config)  # type: ignore[arg-type]
        assert provider.get("OPENAI_AUTH_URL", "fallback") == "fallback"


class TestChainSecretsProvider:
    """Tests for ChainSecretsProvider."""

    def test_chain_returns_first_truthy_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ChainSecretsProvider returns first truthy value found."""
        monkeypatch.setenv("TEST_KEY", "env-value")

        # Env has value, config has a different mapped value
        config = TestConfigSecretsProvider.MockForgeConfig()
        config.proxy.openai.auth_url = "https://config-auth.example.com"

        provider = ChainSecretsProvider(
            EnvSecretsProvider(),
            ConfigSecretsProvider(config),  # type: ignore[arg-type]
        )

        # For TEST_KEY (not in config mapping), should return env value
        assert provider.get("TEST_KEY") == "env-value"

    def test_chain_env_overrides_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env values take precedence over config-injected values."""
        monkeypatch.setenv("OPENAI_AUTH_URL", "https://env-auth.example.com")

        config = TestConfigSecretsProvider.MockForgeConfig()
        config.proxy.openai.auth_url = "https://config-auth.example.com"

        provider = ChainSecretsProvider(
            EnvSecretsProvider(),
            ConfigSecretsProvider(config),  # type: ignore[arg-type]
        )

        # Env wins
        assert provider.get("OPENAI_AUTH_URL") == "https://env-auth.example.com"

    def test_chain_falls_through_to_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Chain falls through to config when env is not set."""
        monkeypatch.delenv("OPENAI_AUTH_URL", raising=False)

        config = TestConfigSecretsProvider.MockForgeConfig()
        config.proxy.openai.auth_url = "https://config-auth.example.com"

        provider = ChainSecretsProvider(
            EnvSecretsProvider(),
            ConfigSecretsProvider(config),  # type: ignore[arg-type]
        )

        # Falls through to config
        assert provider.get("OPENAI_AUTH_URL") == "https://config-auth.example.com"

    def test_chain_treats_empty_string_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty string "" is treated as not-set, falls through to next provider."""
        monkeypatch.setenv("OPENAI_AUTH_URL", "")

        config = TestConfigSecretsProvider.MockForgeConfig()
        config.proxy.openai.auth_url = "https://config-auth.example.com"

        provider = ChainSecretsProvider(
            EnvSecretsProvider(),
            ConfigSecretsProvider(config),  # type: ignore[arg-type]
        )

        # Env has "", so falls through to config
        assert provider.get("OPENAI_AUTH_URL") == "https://config-auth.example.com"

    def test_chain_returns_default_when_all_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Chain returns default when all providers return empty/None."""
        monkeypatch.delenv("OPENAI_AUTH_URL", raising=False)

        config = TestConfigSecretsProvider.MockForgeConfig()
        config.proxy.openai.auth_url = ""

        provider = ChainSecretsProvider(
            EnvSecretsProvider(),
            ConfigSecretsProvider(config),  # type: ignore[arg-type]
        )

        assert provider.get("OPENAI_AUTH_URL", "final-default") == "final-default"

    def test_chain_require_raises_when_all_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Chain.require() raises NoApiKeyError when no provider has a value."""
        monkeypatch.delenv("OPENAI_AUTH_URL", raising=False)

        config = TestConfigSecretsProvider.MockForgeConfig()
        config.proxy.openai.auth_url = ""

        provider = ChainSecretsProvider(
            EnvSecretsProvider(),
            ConfigSecretsProvider(config),  # type: ignore[arg-type]
        )

        with pytest.raises(NoApiKeyError) as exc_info:
            provider.require("OPENAI_AUTH_URL")

        assert exc_info.value.provider == "chain"
        assert exc_info.value.env_var == "OPENAI_AUTH_URL"

    def test_chain_require_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Chain.require() returns value when found."""
        monkeypatch.setenv("OPENAI_AUTH_URL", "https://valid-auth.example.com")

        provider = ChainSecretsProvider(EnvSecretsProvider())

        assert provider.require("OPENAI_AUTH_URL") == "https://valid-auth.example.com"

    def test_chain_requires_at_least_one_provider(self) -> None:
        """ChainSecretsProvider requires at least one provider."""
        with pytest.raises(ValueError, match="at least one provider"):
            ChainSecretsProvider()


# --- Helper to write a credentials file for FileSecretsProvider tests ---


def _write_creds(path: Path, profiles: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"version": 1, "profiles": profiles}, f)


class TestFileSecretsProvider:

    def test_get_returns_value_from_file(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"API_KEY": "sk-file-123"}})

        provider = FileSecretsProvider(profile="default", path=creds)
        assert provider.get("API_KEY") == "sk-file-123"

    def test_get_returns_default_when_key_missing(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"OTHER_KEY": "val"}})

        provider = FileSecretsProvider(profile="default", path=creds)
        assert provider.get("MISSING_KEY", "fallback") == "fallback"

    def test_get_returns_default_when_profile_missing(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": "val"}})

        provider = FileSecretsProvider(profile="nonexistent", path=creds)
        assert provider.get("KEY", "fallback") == "fallback"

    def test_get_returns_default_when_file_missing(self, tmp_path: Path) -> None:
        provider = FileSecretsProvider(profile="default", path=tmp_path / "nope.yaml")
        assert provider.get("KEY", "fallback") == "fallback"

    def test_get_returns_default_when_value_empty(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": ""}})

        provider = FileSecretsProvider(profile="default", path=creds)
        assert provider.get("KEY", "fallback") == "fallback"

    def test_require_returns_value(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"work": {"ANTHROPIC_API_KEY": "sk-ant-test"}})

        provider = FileSecretsProvider(profile="work", path=creds)
        assert provider.require("ANTHROPIC_API_KEY") == "sk-ant-test"

    def test_require_raises_when_key_missing(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"OTHER": "val"}})

        provider = FileSecretsProvider(profile="default", path=creds)
        with pytest.raises(NoApiKeyError) as exc_info:
            provider.require("MISSING_KEY")

        assert exc_info.value.provider == "file:default"
        assert exc_info.value.env_var == "MISSING_KEY"

    def test_require_raises_when_profile_missing(self, tmp_path: Path) -> None:
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": "val"}})

        provider = FileSecretsProvider(profile="work", path=creds)
        with pytest.raises(NoApiKeyError) as exc_info:
            provider.require("KEY")

        assert exc_info.value.provider == "file:work"

    def test_require_raises_when_file_missing(self, tmp_path: Path) -> None:
        provider = FileSecretsProvider(profile="default", path=tmp_path / "nope.yaml")
        with pytest.raises(NoApiKeyError):
            provider.require("KEY")

    def test_profile_defaults_to_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FORGE_PROFILE", raising=False)

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": "default-val"}, "work": {"KEY": "work-val"}})

        provider = FileSecretsProvider(path=creds)
        assert provider.get("KEY") == "default-val"

    def test_profile_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORGE_PROFILE", "work")

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": "default-val"}, "work": {"KEY": "work-val"}})

        provider = FileSecretsProvider(path=creds)
        assert provider.get("KEY") == "work-val"

    def test_explicit_profile_overrides_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORGE_PROFILE", "work")

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": "default-val"}, "work": {"KEY": "work-val"}})

        provider = FileSecretsProvider(profile="default", path=creds)
        assert provider.get("KEY") == "default-val"

    def test_reads_fresh_data_each_call(self, tmp_path: Path) -> None:
        """Verify no stale caching — reads disk each time."""
        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"KEY": "old-value"}})

        provider = FileSecretsProvider(profile="default", path=creds)
        assert provider.get("KEY") == "old-value"

        _write_creds(creds, {"default": {"KEY": "new-value"}})
        assert provider.get("KEY") == "new-value"

    def test_satisfies_secrets_provider_protocol(self, tmp_path: Path) -> None:
        from forge.core.auth.secrets import SecretsProvider

        provider = FileSecretsProvider(profile="default", path=tmp_path / "creds.yaml")
        assert isinstance(provider, SecretsProvider)


class TestChainWithFileProvider:
    """Chain integration: env > file precedence."""

    def test_env_overrides_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "env-value")

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"API_KEY": "file-value"}})

        chain = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds),
        )
        assert chain.get("API_KEY") == "env-value"

    def test_file_fallback_when_env_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_KEY", raising=False)

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"API_KEY": "file-value"}})

        chain = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds),
        )
        assert chain.get("API_KEY") == "file-value"

    def test_file_fallback_when_env_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "")

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"API_KEY": "file-value"}})

        chain = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds),
        )
        assert chain.get("API_KEY") == "file-value"

    def test_chain_require_finds_in_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {"ANTHROPIC_API_KEY": "sk-ant-file"}})

        chain = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds),
        )
        assert chain.require("ANTHROPIC_API_KEY") == "sk-ant-file"

    def test_chain_raises_when_neither_has_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_KEY", raising=False)

        creds = tmp_path / "credentials.yaml"
        _write_creds(creds, {"default": {}})

        chain = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(profile="default", path=creds),
        )
        with pytest.raises(NoApiKeyError) as exc_info:
            chain.require("MISSING_KEY")
        assert exc_info.value.provider == "chain"
