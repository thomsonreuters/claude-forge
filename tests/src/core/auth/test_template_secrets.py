"""Tests for forge.core.auth.template_secrets module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from forge.core.auth.template_secrets import (
    TEMPLATE_SECRETS,
    get_secrets_for_template,
    resolve_env_or_credential,
)


class TestTemplateSecrets:
    """Verify the template-to-secrets mapping."""

    def test_remote_templates_require_base_url(self) -> None:
        for name in ("litellm-openai", "litellm-gemini", "litellm-anthropic"):
            assert "LITELLM_BASE_URL" in TEMPLATE_SECRETS[name]
            assert "LITELLM_API_KEY" in TEMPLATE_SECRETS[name]

    def test_local_templates_require_provider_key(self) -> None:
        assert "GEMINI_API_KEY" in TEMPLATE_SECRETS["litellm-gemini-local"]
        assert "OPENAI_API_KEY" in TEMPLATE_SECRETS["litellm-openai-local"]

    def test_openrouter_templates_require_api_key(self) -> None:
        for name in (
            "openrouter-anthropic",
            "openrouter-openai",
            "openrouter-gemini",
            "openrouter-openai-codex",
            "openrouter-gemini-flash",
            "openrouter-deepseek",
            "openrouter-kimi",
            "openrouter-glm",
            "openrouter-minimax",
            "openrouter-qwen",
        ):
            assert "OPENROUTER_API_KEY" in TEMPLATE_SECRETS[name]


class TestResolveEnvOrCredential:
    """Verify env > credential-file fallback."""

    def test_env_wins_over_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_KEY", "from-env")
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"MY_KEY": "from-file"},
        ):
            assert resolve_env_or_credential("MY_KEY") == "from-env"

    def test_file_fallback_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_KEY", raising=False)
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"MY_KEY": "from-file"},
        ):
            assert resolve_env_or_credential("MY_KEY") == "from-file"

    def test_returns_none_when_both_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_KEY", raising=False)
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={},
        ):
            assert resolve_env_or_credential("MY_KEY") is None

    def test_file_load_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_KEY", raising=False)
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={},
        ):
            assert resolve_env_or_credential("MY_KEY") is None

    def test_empty_env_value_falls_through_to_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_KEY", "")
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"MY_KEY": "from-file"},
        ):
            assert resolve_env_or_credential("MY_KEY") == "from-file"


class TestGetSecretsForTemplate:
    """Verify template-scoped secret resolution."""

    def test_unknown_template_returns_empty(self) -> None:
        assert get_secrets_for_template("unknown-template") == {}

    def test_resolves_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gkey")
        result = get_secrets_for_template("litellm-gemini-local")
        assert result == {"GEMINI_API_KEY": "gkey"}

    def test_resolves_from_credential_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"GEMINI_API_KEY": "file-gkey"},
        ):
            result = get_secrets_for_template("litellm-gemini-local")
            assert result == {"GEMINI_API_KEY": "file-gkey"}


class TestAuthIgnoreEnv:
    """Verify auth_ignore_env bypasses environment variables."""

    def _set_ignore_env(self, monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._auth_ignore_env",
            lambda: value,
        )

    def test_resolve_skips_env_when_ignore_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_ignore_env(monkeypatch, True)
        monkeypatch.setenv("MY_KEY", "from-env")
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"MY_KEY": "from-file"},
        ):
            assert resolve_env_or_credential("MY_KEY") == "from-file"

    def test_resolve_reads_env_when_ignore_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_ignore_env(monkeypatch, False)
        monkeypatch.setenv("MY_KEY", "from-env")
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"MY_KEY": "from-file"},
        ):
            assert resolve_env_or_credential("MY_KEY") == "from-env"

    def test_resolve_returns_none_when_ignore_and_no_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_ignore_env(monkeypatch, True)
        monkeypatch.setenv("MY_KEY", "from-env")
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={},
        ):
            assert resolve_env_or_credential("MY_KEY") is None

    def test_get_secrets_skips_env_when_ignore_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_ignore_env(monkeypatch, True)
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        with patch(
            "forge.core.auth.template_secrets._get_file_secrets",
            return_value={"GEMINI_API_KEY": "file-key"},
        ):
            result = get_secrets_for_template("litellm-gemini-local")
            assert result == {"GEMINI_API_KEY": "file-key"}

    def test_get_secrets_reads_env_when_ignore_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_ignore_env(monkeypatch, False)
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        result = get_secrets_for_template("litellm-gemini-local")
        assert result == {"GEMINI_API_KEY": "env-key"}
