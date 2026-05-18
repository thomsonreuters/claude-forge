"""Regression tests for M4: hyperparameter consistency between get_client() and runtime truth.

Prior to M4, get_client() and get_default_hyperparams_for_tier() diverged:
- get_client() ignored tier_overrides from config

This meant GET / reported different hyperparameters than what clients actually used.
The fix extracted _resolve_tier_hyperparams() as the single source of truth.

These tests guard the invariant: for the same (provider, tier, model_name),
get_client() must create a client with the same default_hyperparams as
get_default_hyperparams_for_tier() returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from forge.config.schema import TierOverride, TierOverrides
from forge.core.llm.types import ModelHyperparameters

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Minimal config stubs — mirrors the real ProviderConfig fields used by
# _resolve_tier_hyperparams()
# ---------------------------------------------------------------------------


@dataclass
class _ProviderCfg:
    top_p: float | None = None
    tier_overrides: TierOverrides = field(default_factory=TierOverrides)


@dataclass
class _ProxyCfg:
    litellm: _ProviderCfg = field(default_factory=_ProviderCfg)


@dataclass
class _Config:
    proxy: _ProxyCfg = field(default_factory=_ProxyCfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_consistency(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    tier: str,
    model_name: str,
    cfg: _Config,
) -> ModelHyperparameters:
    """Assert get_client() and get_default_hyperparams_for_tier() produce identical hyperparams.

    Mocks CoreLLMClientAdapter.__init__ to capture the default_hyperparams
    passed to it by get_client(), then compares against what
    get_default_hyperparams_for_tier() returns.

    Returns the resolved hyperparams for further assertions.
    """
    import forge.proxy.client_factory as cf_mod

    monkeypatch.setattr(cf_mod, "config", cfg)
    monkeypatch.setattr(
        cf_mod,
        "_enforce_max_output_tokens_cap",
        lambda _model, req, **_kwargs: req if req is not None else 4096,
    )

    # Reset singleton for clean state
    cf_mod.TierClientFactory._instance = None
    factory = cf_mod.TierClientFactory()

    # Capture the default_hyperparams passed to CoreLLMClientAdapter
    captured_hyperparams: list[ModelHyperparameters] = []

    class FakeAdapter:
        def __init__(self, **kwargs):
            if "default_hyperparams" in kwargs:
                captured_hyperparams.append(kwargs["default_hyperparams"])

    # Stub client class import
    factory._client_classes[cf_mod.ModelProvider.LITELLM] = FakeAdapter

    # For LiteLLM, mock core provider detection
    if provider_name == "litellm":
        monkeypatch.setattr(
            "forge.core.llm.detection.detect_provider",
            lambda _model: "litellm_remote",
        )

    # Call get_client() — uses pytest-asyncio's managed event loop
    await factory.get_client(model_name, tier=tier)

    assert len(captured_hyperparams) == 1, "CoreLLMClientAdapter should have been called once"
    client_hp = captured_hyperparams[0]

    # Call get_default_hyperparams_for_tier()
    truth_hp = factory.get_default_hyperparams_for_tier(
        provider=provider_name,
        tier=tier,
        model_name=model_name,
    )

    # The invariant: both must be equal
    assert client_hp == truth_hp, (
        f"Hyperparameter divergence for {provider_name}/{tier}/{model_name}:\n"
        f"  get_client():                     {client_hp}\n"
        f"  get_default_hyperparams_for_tier(): {truth_hp}"
    )

    return truth_hp


# ---------------------------------------------------------------------------
# Tests: LiteLLM consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_litellm_default_config_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM with default config: get_client and runtime truth agree."""
    await _assert_consistency(monkeypatch, "litellm", "sonnet", "openai/gpt-5.2", _Config())


@pytest.mark.asyncio
async def test_litellm_tier_override_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM with tier_overrides: get_client and runtime truth agree."""
    cfg = _Config()
    cfg.proxy.litellm.tier_overrides = TierOverrides(
        opus=TierOverride(reasoning_effort="high", temperature=0.3, thinking_budget_tokens=2048),
    )

    hp = await _assert_consistency(monkeypatch, "litellm", "opus", "openai/gpt-5.2", cfg)

    # Verify the tier override was actually applied
    assert hp.reasoning_effort == "high"
    assert hp.temperature == 0.3
    assert hp.thinking is not None
    assert hp.thinking.budget_tokens == 2048


@pytest.mark.asyncio
async def test_litellm_env_overrides_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM with env var overrides: get_client and runtime truth agree."""
    monkeypatch.setenv("LITELLM_HAIKU_REASONING_EFFORT", "low")
    monkeypatch.setenv("LITELLM_HAIKU_VERBOSITY", "low")

    hp = await _assert_consistency(monkeypatch, "litellm", "haiku", "openai/gpt-4o-mini", _Config())

    assert hp.reasoning_effort == "low"
    assert hp.verbosity == "low"
