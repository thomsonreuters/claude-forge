"""Tests for credential registry and capability metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.core.auth.capabilities import (
    CREDENTIALS,
    RETIRED_NAMES,
    credential_for_env_var,
    credentials_for_template,
    format_missing_credential_error,
)
from forge.core.auth.template_secrets import TEMPLATE_SECRETS

TEMPLATE_DIR = Path("src/forge/config/defaults/templates")


def _shipped_template_names() -> list[str]:
    """Scan actual template YAML files on disk."""
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml"))


# ── Template coverage ─────────────────────────────────────────────


class TestTemplateCoverage:
    """Every shipped template must map to credentials."""

    def test_every_shipped_template_has_secrets(self):
        for name in _shipped_template_names():
            assert name in TEMPLATE_SECRETS, f"Template '{name}' has no entry in TEMPLATE_SECRETS"

    def test_every_template_maps_to_credential(self):
        for template in TEMPLATE_SECRETS:
            creds = credentials_for_template(template)
            assert creds, f"Template '{template}' has no matching credential"

    def test_openrouter_templates_use_openrouter_credential(self):
        for name in _shipped_template_names():
            if name.startswith("openrouter-"):
                creds = credentials_for_template(name)
                assert any(
                    c.name == "openrouter" for c in creds
                ), f"Template '{name}' should need 'openrouter' credential"

    def test_litellm_local_templates_use_upstream_credential(self):
        expected = {
            "litellm-anthropic-local": "anthropic-api",
            "litellm-gemini-local": "gemini-api",
            "litellm-openai-local": "openai-api",
        }
        for template, cred_name in expected.items():
            if template not in TEMPLATE_SECRETS:
                pytest.skip(f"Template '{template}' not shipped")
            creds = credentials_for_template(template)
            assert any(
                c.name == cred_name for c in creds
            ), f"Template '{template}' should need '{cred_name}' credential"

    def test_litellm_remote_templates_use_litellm_remote_credential(self):
        remote_templates = [
            t for t in TEMPLATE_SECRETS if t.startswith("litellm-") and "local" not in t and "test" not in t
        ]
        for template in remote_templates:
            creds = credentials_for_template(template)
            assert any(
                c.name == "litellm-remote" for c in creds
            ), f"Remote template '{template}' should need 'litellm-remote' credential"

    def test_unknown_template_returns_empty(self):
        assert credentials_for_template("nonexistent-template") == []

    def test_no_duplicate_credentials_per_template(self):
        for template in TEMPLATE_SECRETS:
            creds = credentials_for_template(template)
            names = [c.name for c in creds]
            assert len(names) == len(set(names)), f"Template '{template}' has duplicate credential entries"


# ── Reverse lookup ────────────────────────────────────────────────


class TestCredentialForEnvVar:

    def test_known_env_vars(self):
        assert credential_for_env_var("OPENROUTER_API_KEY") is CREDENTIALS["openrouter"]
        assert credential_for_env_var("ANTHROPIC_API_KEY") is CREDENTIALS["anthropic-api"]
        assert credential_for_env_var("OPENAI_API_KEY") is CREDENTIALS["openai-api"]
        assert credential_for_env_var("GEMINI_API_KEY") is CREDENTIALS["gemini-api"]
        assert credential_for_env_var("LITELLM_API_KEY") is CREDENTIALS["litellm-remote"]
        assert credential_for_env_var("LITELLM_BASE_URL") is CREDENTIALS["litellm-remote"]

    def test_optional_env_var(self):
        assert credential_for_env_var("OPENROUTER_BASE_URL") is CREDENTIALS["openrouter"]

    def test_unknown_env_var(self):
        assert credential_for_env_var("UNKNOWN_KEY") is None


# ── Retired names ─────────────────────────────────────────────────


class TestRetiredNames:

    def test_anthropic_retired(self):
        msg = RETIRED_NAMES["anthropic"]
        assert "anthropic-api" in msg
        assert "forge auth login -c anthropic-api" in msg
        assert "Claude Code login" in msg

    def test_litellm_local_retired(self):
        msg = RETIRED_NAMES["litellm-local"]
        assert "not a credential" in msg
        assert "gemini-api" in msg
        assert "openai-api" in msg
        assert "anthropic-api" in msg


# ── EnvVar metadata ───────────────────────────────────────────────


class TestEnvVarMetadata:

    def test_openrouter_base_url_has_default_value(self):
        cred = CREDENTIALS["openrouter"]
        base_url_var = next(ev for ev in cred.env_vars if ev.name == "OPENROUTER_BASE_URL")
        assert base_url_var.default_value == "https://openrouter.ai/api/v1"
        assert not base_url_var.required
        assert not base_url_var.secret
        assert base_url_var.connection_value

    def test_api_keys_are_secret(self):
        for cred in CREDENTIALS.values():
            for ev in cred.env_vars:
                if "API_KEY" in ev.name:
                    assert ev.secret, f"{ev.name} should be secret"

    def test_litellm_base_url_is_required(self):
        cred = CREDENTIALS["litellm-remote"]
        base_url_var = next(ev for ev in cred.env_vars if ev.name == "LITELLM_BASE_URL")
        assert base_url_var.required
        assert not base_url_var.secret
        assert base_url_var.connection_value


# ── Credential registry integrity ─────────────────────────────────


class TestCredentialRegistry:

    def test_five_credentials(self):
        assert len(CREDENTIALS) == 5

    def test_credential_names_match_keys(self):
        for key, cred in CREDENTIALS.items():
            assert cred.name == key

    def test_every_credential_has_at_least_one_env_var(self):
        for cred in CREDENTIALS.values():
            assert len(cred.env_vars) >= 1, f"Credential '{cred.name}' has no env vars"

    def test_only_anthropic_api_has_not_needed_for(self):
        for cred in CREDENTIALS.values():
            if cred.name == "anthropic-api":
                assert cred.not_needed_for is not None
            else:
                assert (
                    cred.not_needed_for is None
                ), f"Only anthropic-api should have not_needed_for, but '{cred.name}' has it"


# ── Error formatting ──────────────────────────────────────────────


class TestFormatMissingCredentialError:

    def test_single_missing_var(self):
        cred = CREDENTIALS["openrouter"]
        msg = format_missing_credential_error(cred, missing_vars=["OPENROUTER_API_KEY"])
        assert "OPENROUTER_API_KEY" in msg
        assert "key:" in msg  # singular
        assert "openrouter.ai/keys" in msg
        assert "forge auth login -c openrouter" in msg

    def test_multiple_missing_vars(self):
        cred = CREDENTIALS["litellm-remote"]
        msg = format_missing_credential_error(cred, missing_vars=["LITELLM_API_KEY", "LITELLM_BASE_URL"])
        assert "keys:" in msg  # plural
        assert "LITELLM_API_KEY" in msg
        assert "LITELLM_BASE_URL" in msg
        assert "forge auth login -c litellm-remote" in msg

    def test_with_template(self):
        cred = CREDENTIALS["openrouter"]
        msg = format_missing_credential_error(
            cred, missing_vars=["OPENROUTER_API_KEY"], template="openrouter-anthropic"
        )
        assert "openrouter-anthropic" in msg

    def test_with_context(self):
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(cred, missing_vars=["ANTHROPIC_API_KEY"], context="Supervisor")
        assert "Supervisor requires ANTHROPIC_API_KEY" in msg

    def test_with_context_and_template(self):
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(
            cred,
            missing_vars=["ANTHROPIC_API_KEY"],
            context="Supervisor",
            template="litellm-anthropic-local",
        )
        assert "Supervisor" in msg
        assert "litellm-anthropic-local" in msg

    def test_anthropic_api_shows_not_needed_for(self):
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(cred, missing_vars=["ANTHROPIC_API_KEY"], context="Supervisor")
        assert "NOT needed for:" in msg
        assert "Claude Code's own auth" in msg
        assert "openrouter-anthropic" in msg

    def test_non_anthropic_omits_not_needed_for(self):
        cred = CREDENTIALS["openrouter"]
        msg = format_missing_credential_error(cred, missing_vars=["OPENROUTER_API_KEY"])
        assert "NOT needed for" not in msg

    def test_with_profile(self):
        cred = CREDENTIALS["openrouter"]
        msg = format_missing_credential_error(cred, missing_vars=["OPENROUTER_API_KEY"], profile="work")
        assert "--profile work" in msg

    def test_with_extra_hint(self):
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(
            cred,
            missing_vars=["ANTHROPIC_API_KEY"],
            extra_hint="Or use --subprocess-proxy to route through an existing proxy.",
        )
        assert "--subprocess-proxy" in msg

    def test_with_env_ignored(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(
            cred,
            missing_vars=["ANTHROPIC_API_KEY"],
            env_ignored=True,
        )
        assert "auth_ignore_env" in msg
        assert "ANTHROPIC_API_KEY is set in env" in msg

    def test_env_ignored_only_shows_present_vars(self, monkeypatch):
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        cred = CREDENTIALS["litellm-remote"]
        msg = format_missing_credential_error(
            cred,
            missing_vars=["LITELLM_API_KEY", "LITELLM_BASE_URL"],
            env_ignored=True,
        )
        # Neither var is in env, so no env_ignored note
        assert "auth_ignore_env" not in msg

    def test_signup_url_included(self):
        cred = CREDENTIALS["gemini-api"]
        msg = format_missing_credential_error(cred, missing_vars=["GEMINI_API_KEY"])
        assert "aistudio.google.com" in msg

    def test_no_signup_url_when_none(self):
        cred = CREDENTIALS["litellm-remote"]
        msg = format_missing_credential_error(cred, missing_vars=["LITELLM_API_KEY"])
        assert "Get one at" not in msg

    def test_unlocks_features_shown(self):
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(cred, missing_vars=["ANTHROPIC_API_KEY"])
        assert "supervisor" in msg
        assert "handoff agent" in msg

    def test_note_shown(self):
        cred = CREDENTIALS["anthropic-api"]
        msg = format_missing_credential_error(cred, missing_vars=["ANTHROPIC_API_KEY"])
        assert "Pay-per-token" in msg
