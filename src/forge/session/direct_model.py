"""Claude Code direct model pin helpers."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass

from forge.core.models.catalog import (
    ModelCatalogError,
    get_model_spec,
    resolve_model_id,
)

ONE_M_SUFFIX = "[1m]"


@dataclass(frozen=True)
class DirectModelPin:
    """A Claude Code env-ready direct model pin."""

    canonical_model: str
    env_model: str
    tier: str

    @property
    def env_var(self) -> str:
        return f"ANTHROPIC_DEFAULT_{self.tier.upper()}_MODEL"

    def env(self) -> dict[str, str]:
        return {
            "ANTHROPIC_MODEL": self.tier,
            self.env_var: self.env_model,
        }


def resolve_direct_model_pin(value: str) -> DirectModelPin:
    """Resolve a direct-session model value to Claude Code model env vars.

    The catalog owns aliases and canonical model IDs. Claude Code owns the
    ``[1m]`` model-pin suffix, so this helper strips it for catalog lookup and
    restores it on the normalized env-ready model value.
    """
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("--model cannot be empty")

    requested_1m = raw_value.endswith(ONE_M_SUFFIX)
    lookup_value = raw_value.removesuffix(ONE_M_SUFFIX) if requested_1m else raw_value

    try:
        canonical = resolve_model_id(lookup_value)
    except ModelCatalogError as e:
        raise ValueError(f"Unknown direct Claude model: {value!r}") from e

    normalized_1m = requested_1m or canonical.endswith("-1m")
    base_canonical = canonical.removesuffix("-1m") if canonical.endswith("-1m") else canonical

    if not base_canonical.startswith("claude-"):
        raise ValueError(f"--model only supports Claude models for direct sessions, got {value!r}")

    tier = _claude_tier(base_canonical)
    if tier is None:
        raise ValueError(f"Unsupported Claude model tier for direct sessions: {value!r}")

    if normalized_1m:
        spec = get_model_spec(base_canonical)
        if tier not in {"opus", "sonnet"} and not spec.supports_1m_context:
            raise ValueError("[1m] direct model pins are only supported for Opus/Sonnet Claude models")

    env_model = f"{base_canonical}{ONE_M_SUFFIX}" if normalized_1m else base_canonical
    return DirectModelPin(canonical_model=base_canonical, env_model=env_model, tier=tier)


def direct_model_env(value: str | None) -> dict[str, str]:
    """Return Claude Code direct-model environment variables for ``value``."""
    if not value:
        return {}
    return resolve_direct_model_pin(value).env()


def apply_direct_model_env(env_vars: MutableMapping[str, str], value: str | None) -> str | None:
    """Apply direct-model env vars in-place, returning an error message on failure."""
    if not value:
        return None
    try:
        env_vars.update(direct_model_env(value))
    except ValueError as e:
        return str(e)
    return None


def token_estimate_multiplier_for_direct_model(value: str | None) -> float:
    """Return the catalog token-estimate multiplier for a direct model pin."""
    if not value:
        return 1.0
    pin = resolve_direct_model_pin(value)
    return get_model_spec(pin.canonical_model).token_estimate_multiplier


def _claude_tier(canonical_model: str) -> str | None:
    if canonical_model.startswith("claude-opus-"):
        return "opus"
    if canonical_model.startswith("claude-sonnet-"):
        return "sonnet"
    if canonical_model.startswith("claude-haiku-"):
        return "haiku"
    return None
