"""Tests for direct Claude model pin helpers."""

from __future__ import annotations

import pytest

from forge.session.direct_model import (
    apply_direct_model_env,
    direct_model_env,
    resolve_direct_model_pin,
)


def test_resolves_opus_47_alias_to_env_pin() -> None:
    pin = resolve_direct_model_pin("opus-4-7")

    assert pin.canonical_model == "claude-opus-4-7"
    assert pin.env_model == "claude-opus-4-7"
    assert pin.tier == "opus"
    assert pin.env() == {
        "ANTHROPIC_MODEL": "opus",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7",
    }


def test_preserves_claude_code_1m_suffix() -> None:
    pin = resolve_direct_model_pin("claude-sonnet-4-6[1m]")

    assert pin.canonical_model == "claude-sonnet-4-6"
    assert pin.env_model == "claude-sonnet-4-6[1m]"
    assert pin.env() == {
        "ANTHROPIC_MODEL": "sonnet",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]",
    }


def test_normalizes_catalog_1m_alias_to_claude_code_suffix() -> None:
    pin = resolve_direct_model_pin("opus-4-6-1m")

    assert pin.canonical_model == "claude-opus-4-6"
    assert pin.env_model == "claude-opus-4-6[1m]"


def test_rejects_unknown_direct_model() -> None:
    with pytest.raises(ValueError, match="Unknown direct Claude model"):
        resolve_direct_model_pin("claude-opus-4.7.1")


def test_rejects_non_claude_model() -> None:
    with pytest.raises(ValueError, match="only supports Claude"):
        direct_model_env("gpt-5.5")


def test_apply_direct_model_env_updates_mapping() -> None:
    env_vars: dict[str, str] = {}

    error = apply_direct_model_env(env_vars, "opus-4-7")

    assert error is None
    assert env_vars == {
        "ANTHROPIC_MODEL": "opus",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7",
    }


def test_apply_direct_model_env_returns_error() -> None:
    env_vars: dict[str, str] = {}

    error = apply_direct_model_env(env_vars, "gpt-5.5")

    assert error is not None
    assert "only supports Claude" in error
    assert env_vars == {}
