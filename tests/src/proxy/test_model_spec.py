"""Tests for model spec normalization."""

import pytest

from forge.proxy.model_spec import (
    KNOWN_PROVIDERS,
    KNOWN_VENDORS,
    _infer_vendor,
    normalize_model_spec,
)


class TestInferVendor:
    """Tests for _infer_vendor helper."""

    def test_openai_gpt_models(self):
        """GPT models map to openai."""
        assert _infer_vendor("gpt-5.2") == "openai"
        assert _infer_vendor("gpt-4") == "openai"
        assert _infer_vendor("GPT-5.2") == "openai"

    def test_openai_o_series_models(self):
        """O-series models map to openai."""
        assert _infer_vendor("o1-preview") == "openai"
        assert _infer_vendor("o3-mini") == "openai"
        assert _infer_vendor("o4-preview") == "openai"

    def test_anthropic_claude_models(self):
        """Claude models map to anthropic."""
        assert _infer_vendor("claude-sonnet-4") == "anthropic"
        assert _infer_vendor("claude-3-opus") == "anthropic"
        assert _infer_vendor("CLAUDE-SONNET-4") == "anthropic"

    def test_gemini_models_to_vertex_ai(self):
        """Gemini models map to vertex_ai (remote routing)."""
        assert _infer_vendor("gemini-3.1-pro-preview") == "vertex_ai"
        assert _infer_vendor("gemini-2.0-flash") == "vertex_ai"
        assert _infer_vendor("GEMINI-3-PRO") == "vertex_ai"

    def test_unknown_model_raises(self):
        """Unknown model patterns raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            _infer_vendor("llama-3")
        assert "Cannot infer vendor" in str(exc_info.value)

        with pytest.raises(ValueError):
            _infer_vendor("mistral-large")


class TestNormalizeModelSpecHappyPaths:
    """Tests for normalize_model_spec happy paths."""

    def test_provider_vendor_model_format(self):
        """Full provider/vendor/model format."""
        provider, model_id = normalize_model_spec("litellm_remote/openai/gpt-5.2")
        assert provider == "litellm_remote"
        assert model_id == "openai/gpt-5.2"

    def test_vendor_model_format(self):
        """Standard vendor/model format uses default provider."""
        provider, model_id = normalize_model_spec("openai/gpt-5.2")
        assert provider == "litellm_remote"
        assert model_id == "openai/gpt-5.2"

    def test_model_only_format(self):
        """Model-only format infers vendor and uses default provider."""
        provider, model_id = normalize_model_spec("gpt-5.2")
        assert provider == "litellm_remote"
        assert model_id == "openai/gpt-5.2"

    def test_custom_default_provider(self):
        """Custom default provider is respected."""
        provider, model_id = normalize_model_spec("openai/gpt-5.2", default_provider="litellm_local")
        assert provider == "litellm_local"
        assert model_id == "openai/gpt-5.2"


class TestVendorInference:
    """Tests for vendor inference in model-only format."""

    def test_gpt_to_openai(self):
        """gpt-* infers openai vendor."""
        provider, model_id = normalize_model_spec("gpt-5.2")
        assert model_id == "openai/gpt-5.2"

    def test_claude_to_anthropic(self):
        """claude-* infers anthropic vendor."""
        provider, model_id = normalize_model_spec("claude-sonnet-4")
        assert model_id == "anthropic/claude-sonnet-4"

    def test_gemini_to_vertex_ai(self):
        """gemini-* infers vertex_ai (remote routing)."""
        provider, model_id = normalize_model_spec("gemini-2.0-flash")
        assert model_id == "vertex_ai/gemini-2.0-flash"

    def test_o_series_to_openai(self):
        """o1*, o3*, o4* infer openai vendor."""
        _, model_id_o1 = normalize_model_spec("o1-preview")
        _, model_id_o3 = normalize_model_spec("o3-mini")
        _, model_id_o4 = normalize_model_spec("o4-preview")

        assert model_id_o1 == "openai/o1-preview"
        assert model_id_o3 == "openai/o3-mini"
        assert model_id_o4 == "openai/o4-preview"


class TestInvalidInputs:
    """Tests for invalid input handling."""

    def test_empty_spec_raises(self):
        """Empty spec raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            normalize_model_spec("")
        assert "cannot be empty" in str(exc_info.value)

        with pytest.raises(ValueError):
            normalize_model_spec("   ")

    def test_provider_without_vendor_raises(self):
        """Provider-like first segment without vendor raises."""
        with pytest.raises(ValueError) as exc_info:
            normalize_model_spec("litellm_remote/gpt-5.2")
        assert "Ambiguous" in str(exc_info.value)

    def test_unknown_vendor_raises(self):
        """Unknown vendor prefix raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            normalize_model_spec("unknown_vendor/model")
        assert "Unknown vendor" in str(exc_info.value)

    def test_unknown_provider_raises(self):
        """Unknown provider prefix raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            normalize_model_spec("unknown_provider/openai/gpt-5.2")
        assert "Unknown provider" in str(exc_info.value)

    def test_too_many_segments_raises(self):
        """4+ segments raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            normalize_model_spec("a/b/c/d")
        assert "too many segments" in str(exc_info.value)

    def test_unknown_model_only_vendor_inference_raises(self):
        """Model-only with unknown pattern raises."""
        with pytest.raises(ValueError) as exc_info:
            normalize_model_spec("llama-3-70b")
        assert "Cannot infer vendor" in str(exc_info.value)


class TestKnownConstants:
    """Tests that known constants are properly defined."""

    def test_known_providers(self):
        """All expected providers are defined."""
        assert "litellm_remote" in KNOWN_PROVIDERS
        assert "litellm_local" in KNOWN_PROVIDERS

    def test_known_vendors(self):
        """All expected vendors are defined."""
        assert "openai" in KNOWN_VENDORS
        assert "anthropic" in KNOWN_VENDORS
        assert "vertex_ai" in KNOWN_VENDORS
        assert "gemini" in KNOWN_VENDORS
