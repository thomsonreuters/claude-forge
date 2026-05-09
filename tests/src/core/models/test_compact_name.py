"""Tests for get_compact_name() — catalog-driven model display names."""

from forge.core.models import get_compact_name


class TestGetCompactName:
    """Tests for get_compact_name function."""

    # --- Provider prefix stripping ---

    def test_strips_provider_prefix(self) -> None:
        """Provider prefix is stripped (vertex_ai/, openai/, gemini/)."""
        assert get_compact_name("vertex_ai/gemini-3.1-pro-preview") == "gemini-3.1-pro"
        assert get_compact_name("openai/gpt-5.2") == "gpt-5.2"
        assert get_compact_name("gemini/gemini-3.1-pro-preview") == "gemini-3.1-pro"

    def test_no_prefix_passthrough(self) -> None:
        """Models without provider prefix work."""
        assert get_compact_name("gpt-5.2") == "gpt-5.2"
        assert get_compact_name("o3") == "o3"

    # --- Catalog short_name overrides ---

    def test_catalog_short_name_gemini_flash(self) -> None:
        """gemini-2.5-flash uses catalog short_name 'gemini-flash'."""
        assert get_compact_name("gemini-2.5-flash") == "gemini-flash"

    def test_catalog_short_name_codex_mini(self) -> None:
        """gpt-5.1-codex-mini uses catalog short_name 'codex-mini'."""
        assert get_compact_name("gpt-5.1-codex-mini") == "codex-mini"

    def test_catalog_short_name_customtools(self) -> None:
        """gemini-3.1-pro-preview-customtools uses catalog short_name."""
        assert get_compact_name("gemini-3.1-pro-preview-customtools") == "gemini-3.1-pro-ct"
        assert get_compact_name("gemini/gemini-3.1-pro-preview-customtools") == "gemini-3.1-pro-ct"

    def test_catalog_short_name_with_prefix(self) -> None:
        """Short name works even with provider prefix."""
        assert get_compact_name("openai/gpt-5.1-codex-mini") == "codex-mini"

    # --- Generic -preview stripping ---

    def test_strips_preview_suffix(self) -> None:
        """The -preview suffix is stripped for display."""
        assert get_compact_name("gemini-3.1-pro-preview") == "gemini-3.1-pro"
        assert get_compact_name("gemini-3-flash-preview") == "gemini-3-flash"

    def test_no_preview_passthrough(self) -> None:
        """Models without -preview suffix are unchanged."""
        assert get_compact_name("gpt-4o") == "gpt-4o"
        assert get_compact_name("o3-mini") == "o3-mini"

    # --- Unknown models (not in catalog) ---

    def test_unknown_model_passthrough(self) -> None:
        """Unknown models return cleaned name (prefix stripped)."""
        assert get_compact_name("some-future-model") == "some-future-model"

    def test_unknown_model_with_prefix(self) -> None:
        """Unknown models with prefix get prefix stripped."""
        assert get_compact_name("provider/some-future-model") == "some-future-model"

    def test_unknown_model_with_preview(self) -> None:
        """Unknown models with -preview get it stripped."""
        assert get_compact_name("provider/some-model-preview") == "some-model"
