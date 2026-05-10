"""Tests for model ID and alias resolution."""

import pytest

from forge.core.models import (
    ModelCatalogError,
    ModelSpec,
    get_context_window_tokens,
    get_max_output_tokens,
    get_model_spec,
    load_model_catalog,
    model_exists,
    resolve_model_id,
)


class TestResolveModelId:
    """Tests for resolve_model_id function."""

    def test_resolves_canonical_id(self):
        """Canonical model ID resolves to itself."""
        result = resolve_model_id("gpt-5.2")
        assert result == "gpt-5.2"

    def test_resolves_alias_to_canonical(self):
        """Alias resolves to its canonical model ID."""
        result = resolve_model_id("openai/gpt-5.2")
        assert result == "gpt-5.2"

    def test_raises_on_unknown_model(self):
        """Unknown model ID raises ModelCatalogError."""
        with pytest.raises(ModelCatalogError, match="Unknown model or alias"):
            resolve_model_id("totally-fake-model")

    def test_raises_on_unknown_alias(self):
        """Unknown alias raises ModelCatalogError."""
        with pytest.raises(ModelCatalogError, match="Unknown model or alias"):
            resolve_model_id("openai/totally-fake-model")


class TestGetModelSpec:
    """Tests for get_model_spec function."""

    def test_returns_spec_for_canonical_id(self):
        """Returns ModelSpec for canonical model ID."""
        spec = get_model_spec("gpt-5.2")

        assert isinstance(spec, ModelSpec)
        assert spec.friendly_name == "GPT-5.2"
        assert spec.context_window_tokens == 400000

    def test_returns_spec_for_alias(self):
        """Returns same ModelSpec when accessed via alias."""
        spec_canonical = get_model_spec("gpt-5.2")
        spec_alias = get_model_spec("openai/gpt-5.2")

        assert spec_canonical is spec_alias

    def test_raises_on_unknown(self):
        """Unknown model raises ModelCatalogError."""
        with pytest.raises(ModelCatalogError):
            get_model_spec("nonexistent-model")


class TestGemini31ProPreviewIsCanonical:
    """Tests ensuring gemini-3.1-pro-preview is a canonical model."""

    def test_gemini_31_pro_preview_is_canonical(self):
        """gemini-3.1-pro-preview exists as a canonical model, not an alias."""
        catalog = load_model_catalog()

        assert "gemini-3.1-pro-preview" in catalog.models
        assert "gemini-3.1-pro-preview" not in catalog.aliases

    def test_gemini_31_pro_preview_has_correct_properties(self):
        """gemini-3.1-pro-preview has expected intrinsic properties."""
        spec = get_model_spec("gemini-3.1-pro-preview")

        assert spec.context_window_tokens == 1048576  # 1M
        assert spec.max_output_tokens == 65536
        assert spec.supports_thinking is True
        assert spec.supports_images is True

    def test_gemini_31_pro_preview_customtools_is_canonical(self):
        """gemini-3.1-pro-preview-customtools exists as a canonical model."""
        catalog = load_model_catalog()

        assert "gemini-3.1-pro-preview-customtools" in catalog.models
        assert "gemini-3.1-pro-preview-customtools" not in catalog.aliases

    def test_customtools_aliases_resolve(self):
        """Provider-prefixed customtools aliases resolve correctly."""
        assert resolve_model_id("vertex_ai/gemini-3.1-pro-preview-customtools") == "gemini-3.1-pro-preview-customtools"
        assert resolve_model_id("gemini/gemini-3.1-pro-preview-customtools") == "gemini-3.1-pro-preview-customtools"

    def test_vertex_ai_alias_resolves_to_gemini_31(self):
        """vertex_ai/gemini-3.1-pro-preview alias resolves correctly."""
        canonical = resolve_model_id("vertex_ai/gemini-3.1-pro-preview")
        assert canonical == "gemini-3.1-pro-preview"

    def test_gemini_alias_resolves_to_gemini_31(self):
        """gemini/gemini-3.1-pro-preview alias resolves correctly."""
        canonical = resolve_model_id("gemini/gemini-3.1-pro-preview")
        assert canonical == "gemini-3.1-pro-preview"


class TestConvenienceFunctions:
    """Tests for convenience lookup functions."""

    def test_get_context_window_tokens_canonical(self):
        """get_context_window_tokens works with canonical IDs."""
        assert get_context_window_tokens("gpt-5.2") == 400000
        assert get_context_window_tokens("gemini-2.5-pro") == 1048576
        assert get_context_window_tokens("claude-opus-4-5-20251101") == 200000

    def test_get_context_window_tokens_alias(self):
        """get_context_window_tokens works with aliases."""
        assert get_context_window_tokens("openai/gpt-5.2") == 400000
        assert get_context_window_tokens("vertex_ai/gemini-2.5-pro") == 1048576

    def test_get_max_output_tokens_canonical(self):
        """get_max_output_tokens works with canonical IDs."""
        assert get_max_output_tokens("gpt-5.2") == 128000
        assert get_max_output_tokens("gemini-3.1-pro-preview") == 65536

    def test_get_max_output_tokens_alias(self):
        """get_max_output_tokens works with aliases."""
        assert get_max_output_tokens("openai/gpt-5.2") == 128000

    def test_convenience_functions_raise_on_unknown(self):
        """Convenience functions raise on unknown models."""
        with pytest.raises(ModelCatalogError):
            get_context_window_tokens("fake-model")

        with pytest.raises(ModelCatalogError):
            get_max_output_tokens("fake-model")


class TestModelExists:
    """Tests for model_exists function."""

    def test_returns_true_for_canonical(self):
        """Returns True for canonical model IDs."""
        assert model_exists("gpt-5.2") is True
        assert model_exists("gemini-3.1-pro-preview") is True

    def test_returns_true_for_alias(self):
        """Returns True for aliases."""
        assert model_exists("openai/gpt-5.2") is True
        assert model_exists("vertex_ai/gemini-3.1-pro-preview") is True

    def test_returns_false_for_unknown(self):
        """Returns False for unknown models (doesn't raise)."""
        assert model_exists("totally-fake-model") is False
        assert model_exists("openai/fake-model") is False


class TestCatalogContainment:
    """Tests for __contains__ method on ModelCatalog."""

    def test_in_operator_for_canonical(self):
        """'in' operator works for canonical models."""
        catalog = load_model_catalog()
        assert "gpt-5.2" in catalog
        assert "gemini-3.1-pro-preview" in catalog

    def test_in_operator_for_alias(self):
        """'in' operator works for aliases."""
        catalog = load_model_catalog()
        assert "openai/gpt-5.2" in catalog
        assert "vertex_ai/gemini-3.1-pro-preview" in catalog

    def test_in_operator_for_unknown(self):
        """'in' operator returns False for unknown."""
        catalog = load_model_catalog()
        assert "fake-model" not in catalog


class TestSystemPromptAddendum:
    """Tests for get_system_prompt_addendum resolution."""

    def test_returns_content_for_openai_model(self):
        from forge.core.models import get_system_prompt_addendum

        content = get_system_prompt_addendum("gpt-5.5")
        assert content is not None
        assert "Read" in content
        assert "pages" in content

    def test_returns_content_for_gemini_model(self):
        from forge.core.models import get_system_prompt_addendum

        content = get_system_prompt_addendum("gemini-3.1-pro-preview")
        assert content is not None
        assert "Read" in content

    def test_returns_none_for_claude_model(self):
        from forge.core.models import get_system_prompt_addendum

        assert get_system_prompt_addendum("claude-opus-4-6") is None

    def test_returns_none_for_unknown_model(self):
        from forge.core.models import get_system_prompt_addendum

        assert get_system_prompt_addendum("unknown-custom-model") is None

    def test_strips_provider_prefix(self):
        from forge.core.models import get_system_prompt_addendum

        content = get_system_prompt_addendum("openai/gpt-5.5")
        assert content is not None

    def test_openai_and_gemini_files_loadable(self):
        from importlib import resources

        for name in ("openai.md", "gemini.md"):
            ref = resources.files("forge.core.data").joinpath("system_prompt_addendums", name)
            content = ref.read_text(encoding="utf-8")
            assert len(content) > 100
