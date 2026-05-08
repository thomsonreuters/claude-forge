"""Tests for model pricing lookup and cost calculation."""

from __future__ import annotations

import pytest

from forge.core.models.pricing import (
    calculate_cost,
    get_pricing,
    micros_to_usd,
    reset_pricing_cache,
)


@pytest.fixture(autouse=True)
def _fresh_pricing_cache():
    """Reset pricing singleton between tests."""
    reset_pricing_cache()
    yield
    reset_pricing_cache()


class TestGetPricing:
    def test_known_model_returns_catalog_source(self):
        p = get_pricing("claude-sonnet-4-6")
        assert p.source == "catalog"
        assert p.input_per_mtok == 3.0
        assert p.output_per_mtok == 15.0
        assert p.cached_input_per_mtok == 0.30

    def test_known_model_opus(self):
        p = get_pricing("claude-opus-4-6")
        assert p.input_per_mtok == 5.0
        assert p.output_per_mtok == 25.0

    def test_known_model_haiku(self):
        p = get_pricing("claude-haiku-4-5-20251001")
        assert p.input_per_mtok == 1.0
        assert p.output_per_mtok == 5.0

    def test_known_openai_model(self):
        p = get_pricing("gpt-5.5")
        assert p.source == "catalog"
        assert p.input_per_mtok == 5.0

    def test_known_gemini_model(self):
        p = get_pricing("gemini-2.5-pro")
        assert p.source == "catalog"
        assert p.input_per_mtok == 1.25

    def test_alias_resolves_to_canonical(self):
        """OpenRouter dot-form alias should resolve via model catalog."""
        p = get_pricing("anthropic/claude-sonnet-4.6")
        assert p.source == "catalog"
        assert p.input_per_mtok == 3.0

    def test_unknown_model_falls_back_to_default(self):
        p = get_pricing("meta-llama/llama-3.1-70b")
        assert p.source == "default"
        assert p.input_per_mtok > 0

    def test_pricing_is_frozen(self):
        p = get_pricing("claude-sonnet-4-6")
        with pytest.raises(AttributeError):
            p.input_per_mtok = 999.0  # type: ignore[misc]


class TestCalculateCost:
    def test_basic_cost(self):
        cost = calculate_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=500, cached_tokens=0)
        assert cost > 0
        assert isinstance(cost, int)

    def test_zero_tokens_zero_cost(self):
        cost = calculate_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=0, cached_tokens=0)
        assert cost == 0

    def test_cached_tokens_not_double_counted(self):
        """Cached tokens should be charged at cached_input rate, not full input rate.

        If all 1000 input tokens are cached:
          cost = (1000-1000)*input + 1000*cached + 0*output
               = 0 + 1000*cached_rate + 0
        This should be LESS than the non-cached cost.
        """
        full_cost = calculate_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=0, cached_tokens=0)
        cached_cost = calculate_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=0, cached_tokens=1000)
        assert cached_cost < full_cost

    def test_cached_tokens_capped_at_input(self):
        """Cached tokens > input tokens should not produce negative fresh input."""
        cost_normal = calculate_cost("claude-sonnet-4-6", input_tokens=100, output_tokens=0, cached_tokens=100)
        cost_overcached = calculate_cost("claude-sonnet-4-6", input_tokens=100, output_tokens=0, cached_tokens=200)
        assert cost_overcached == cost_normal

    def test_cost_math_explicit(self):
        """Verify exact cost calculation for sonnet with known rates.

        Sonnet: input=$3/MTok, cached=$0.30/MTok, output=$15/MTok
        1M input (500K cached) + 200K output:
          fresh = (1M - 500K) * 3.0 / 1M = 1.50
          cached = 500K * 0.30 / 1M = 0.15
          output = 200K * 15.0 / 1M = 3.00
          total = 4.65 USD = 4_650_000 microdollars
        """
        cost = calculate_cost(
            "claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=200_000,
            cached_tokens=500_000,
        )
        assert cost == 4_650_000

    def test_unknown_model_uses_default_pricing(self):
        cost = calculate_cost("unknown/model-xyz", input_tokens=1000, output_tokens=500, cached_tokens=0)
        assert cost > 0

    def test_output_only_cost(self):
        """Output-only cost (input=0, cached=0)."""
        cost = calculate_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=1_000_000, cached_tokens=0)
        assert cost == 15_000_000  # $15/MTok * 1M tokens


class TestMicrosToUsd:
    def test_conversion(self):
        assert micros_to_usd(1_000_000) == 1.0
        assert micros_to_usd(500_000) == 0.5
        assert micros_to_usd(0) == 0.0

    def test_small_values(self):
        assert micros_to_usd(1) == 0.000001
