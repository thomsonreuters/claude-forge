"""Regression tests for tier override truthiness bugs (#55).

Upstream 30ffe6d fixed Python truthiness bugs where ``if val:`` silently
skipped legitimate falsy values like ``top_p=0.0`` and ``thinking_budget_tokens=0``.
Forge's ``client_factory.py`` (formerly ``credential_manager.py``) had 3 affected lines
in the LiteLLM tier-override path using bare truthiness checks instead of ``is not None``.

These tests verify the fixed code correctly distinguishes ``None`` (not set,
fall through to provider default) from non-None values (apply the override).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from forge.config.schema import TierOverride
from forge.core.llm.types import ModelHyperparameters

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Stub config that satisfies TierClientFactory.get_default_hyperparams_for_tier
# ---------------------------------------------------------------------------


@dataclass
class _LiteLLM:
    top_p: float | None = None
    tier_overrides: dict[str, TierOverride] = field(default_factory=dict)


@dataclass
class _ProxyCfg:
    litellm: _LiteLLM = field(default_factory=_LiteLLM)


@dataclass
class _Config:
    proxy: _ProxyCfg = field(default_factory=_ProxyCfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_hyperparams(
    monkeypatch: pytest.MonkeyPatch,
    tier_override: TierOverride,
    tier: str = "opus",
) -> ModelHyperparameters:
    """Call get_default_hyperparams_for_tier with a stubbed config."""
    import forge.proxy.client_factory as cf_mod

    cfg = _Config()
    cfg.proxy.litellm.tier_overrides[tier] = tier_override

    monkeypatch.setattr(cf_mod, "config", cfg)
    monkeypatch.setattr(
        cf_mod,
        "_enforce_max_output_tokens_cap",
        lambda _model, req, **_kwargs: req if req is not None else 4096,
    )

    # Reset singleton so we get a fresh instance with test config
    cf_mod.TierClientFactory._instance = None
    factory = cf_mod.TierClientFactory()

    return factory.get_default_hyperparams_for_tier(
        provider="litellm",
        tier=tier,
        model_name="openai/gpt-5.2",
    )


# ---------------------------------------------------------------------------
# Tests: thinking_budget_tokens
# ---------------------------------------------------------------------------


def test_thinking_budget_tokens_positive_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive thinking_budget_tokens override creates a ThinkingConfig."""
    hp = _get_hyperparams(monkeypatch, TierOverride(thinking_budget_tokens=1024))

    assert hp.thinking is not None
    assert hp.thinking.type == "enabled"
    assert hp.thinking.budget_tokens == 1024


def test_thinking_budget_tokens_none_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking_budget_tokens=None means 'not configured' — fall through to provider."""
    hp = _get_hyperparams(monkeypatch, TierOverride(thinking_budget_tokens=None))

    # Provider config also has thinking_type=None, so thinking should be None
    assert hp.thinking is None


def test_thinking_budget_tokens_zero_disables_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking_budget_tokens=0 means 'disable thinking for this tier'.

    The is-not-None fix ensures 0 is not silently ignored (old truthiness bug).
    The proxy layer interprets 0 as "disable" rather than passing it to
    ThinkingConfig (which rejects non-positive budget_tokens).
    """
    hp = _get_hyperparams(monkeypatch, TierOverride(thinking_budget_tokens=0))

    assert hp.thinking is None


def test_thinking_budget_tokens_negative_disables_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative thinking_budget_tokens also disables thinking."""
    hp = _get_hyperparams(monkeypatch, TierOverride(thinking_budget_tokens=-1))

    assert hp.thinking is None


# ---------------------------------------------------------------------------
# Tests: reasoning_effort
# ---------------------------------------------------------------------------


def test_reasoning_effort_override_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit reasoning_effort override is used instead of provider default."""
    hp = _get_hyperparams(monkeypatch, TierOverride(reasoning_effort="high"))

    assert hp.reasoning_effort == "high"


def test_reasoning_effort_none_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reasoning_effort=None means 'not configured' — no fallback, stays None."""
    hp = _get_hyperparams(monkeypatch, TierOverride(reasoning_effort=None))

    assert hp.reasoning_effort is None


# ---------------------------------------------------------------------------
# Tests: verbosity
# ---------------------------------------------------------------------------


def test_verbosity_override_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit verbosity override is used instead of provider default."""
    hp = _get_hyperparams(monkeypatch, TierOverride(verbosity="low"))

    assert hp.verbosity == "low"


def test_verbosity_none_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verbosity=None means 'not configured' — no fallback, stays None."""
    hp = _get_hyperparams(monkeypatch, TierOverride(verbosity=None))

    assert hp.verbosity is None


# ---------------------------------------------------------------------------
# Tests: temperature (already correct — regression guard)
# ---------------------------------------------------------------------------


def test_temperature_zero_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """temperature=0.0 must not be skipped. (Already correct pre-fix — guards regression.)"""
    hp = _get_hyperparams(monkeypatch, TierOverride(temperature=0.0))

    assert hp.temperature == 0.0


def test_temperature_none_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """temperature=None means 'not configured' — no fallback, stays None."""
    hp = _get_hyperparams(monkeypatch, TierOverride(temperature=None))

    assert hp.temperature is None
