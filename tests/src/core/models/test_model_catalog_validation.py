"""Tests for model catalog validation errors."""

import pytest

from forge.core.models import ModelCatalogError
from forge.core.models.catalog import (
    _parse_model_spec,
    _validate_and_build_catalog,
    load_model_catalog,
)
from forge.core.models.types import ModelSpec, TemperatureSpec
from forge.proxy.server import _EFFORT_RANK


def _minimal_model_data() -> dict:
    """Return minimal valid model data for testing."""
    return {
        "friendly_name": "Test Model",
        "context_window_tokens": 100000,
        "max_output_tokens": 10000,
        "max_thinking_tokens": None,
        "supports_thinking": False,
        "supports_images": False,
        "temperature_constraint": "range",
        "temperature": {"min": 0.0, "default": 1.0, "max": 2.0},
        "intelligence_score": 50,
        "tags": [],
    }


def _minimal_catalog_data() -> dict:
    """Return minimal valid catalog data for testing."""
    return {
        "schema_version": 1,
        "models": {"test-model": _minimal_model_data()},
        "aliases": {},
    }


class TestModelFieldValidation:
    """Tests for individual model field validation."""

    def test_rejects_missing_required_field(self):
        """Missing required field raises ModelCatalogError."""
        data = _minimal_model_data()
        del data["context_window_tokens"]

        with pytest.raises(ModelCatalogError, match="missing required fields"):
            _parse_model_spec("test", data)

    def test_rejects_zero_context_window(self):
        """context_window_tokens <= 0 raises error."""
        data = _minimal_model_data()
        data["context_window_tokens"] = 0

        with pytest.raises((ModelCatalogError, ValueError), match="context_window_tokens must be > 0"):
            _parse_model_spec("test", data)

    def test_rejects_negative_max_output(self):
        """max_output_tokens <= 0 raises error."""
        data = _minimal_model_data()
        data["max_output_tokens"] = -1

        with pytest.raises((ModelCatalogError, ValueError), match="max_output_tokens must be > 0"):
            _parse_model_spec("test", data)

    def test_rejects_invalid_intelligence_score(self):
        """intelligence_score outside 0-100 raises error."""
        data = _minimal_model_data()
        data["intelligence_score"] = 150

        with pytest.raises((ModelCatalogError, ValueError), match="intelligence_score must be 0-100"):
            _parse_model_spec("test", data)


class TestTemperatureValidation:
    """Tests for temperature spec validation."""

    def test_rejects_invalid_temperature_dict_structure(self):
        """Temperature dict without required keys raises error."""
        data = _minimal_model_data()
        data["temperature"] = {"min": 0.0}  # Missing default and max

        with pytest.raises(ModelCatalogError, match="temperature dict must have min/default/max"):
            _parse_model_spec("test", data)

    def test_rejects_temperature_invariant_violation(self):
        """Temperature where min > default raises error."""
        data = _minimal_model_data()
        data["temperature"] = {"min": 2.0, "default": 1.0, "max": 2.0}

        with pytest.raises((ModelCatalogError, ValueError), match="Temperature invariant violated"):
            _parse_model_spec("test", data)

    def test_rejects_negative_temperature_min(self):
        """Negative temperature min raises error."""
        data = _minimal_model_data()
        data["temperature"] = {"min": -1.0, "default": 1.0, "max": 2.0}

        with pytest.raises((ModelCatalogError, ValueError), match="min must be >= 0"):
            _parse_model_spec("test", data)

    def test_fixed_temperature_requires_min_equals_max(self):
        """Fixed temperature constraint requires min == max."""
        data = _minimal_model_data()
        data["temperature_constraint"] = "fixed"
        data["temperature"] = {"min": 0.5, "default": 1.0, "max": 1.5}

        with pytest.raises(
            (ModelCatalogError, ValueError),
            match="Fixed temperature constraint requires min == max",
        ):
            _parse_model_spec("test", data)

    def test_accepts_single_value_temperature(self):
        """Single value temperature is expanded to min=default=max."""
        data = _minimal_model_data()
        data["temperature_constraint"] = "fixed"
        data["temperature"] = 1.0

        spec = _parse_model_spec("test", data)
        assert spec.temperature.min == 1.0
        assert spec.temperature.default == 1.0
        assert spec.temperature.max == 1.0


class TestTemperatureConstraintValidation:
    """Tests for temperature_constraint enum validation."""

    def test_rejects_invalid_temperature_constraint(self):
        """Invalid temperature_constraint value raises error."""
        data = _minimal_model_data()
        data["temperature_constraint"] = "flexible"

        with pytest.raises(ModelCatalogError, match="invalid temperature_constraint"):
            _parse_model_spec("test", data)


class TestOptionalFieldsValidation:
    """Tests for optional field validation."""

    def test_accepts_optional_reasoning_fields(self):
        """Optional reasoning fields are parsed correctly."""
        data = _minimal_model_data()
        data["litellm_reasoning_efforts"] = ["low", "medium", "high"]
        data["default_reasoning_effort"] = "medium"

        spec = _parse_model_spec("test", data)
        assert spec.litellm_reasoning_efforts == ("low", "medium", "high")
        assert spec.default_reasoning_effort == "medium"

    def test_accepts_null_reasoning_fields(self):
        """Null reasoning fields are handled correctly."""
        data = _minimal_model_data()
        data["litellm_reasoning_efforts"] = None
        data["default_reasoning_effort"] = None

        spec = _parse_model_spec("test", data)
        assert spec.litellm_reasoning_efforts is None
        assert spec.default_reasoning_effort is None

    def test_accepts_verbosity_fields(self):
        """Verbosity fields are parsed correctly."""
        data = _minimal_model_data()
        data["supports_verbosity"] = True
        data["verbosity_levels"] = ["low", "medium", "high"]

        spec = _parse_model_spec("test", data)
        assert spec.supports_verbosity is True
        assert spec.verbosity_levels == ("low", "medium", "high")

    def test_accepts_use_responses_api(self):
        """use_responses_api field is parsed correctly."""
        data = _minimal_model_data()
        data["use_responses_api"] = True

        spec = _parse_model_spec("test", data)
        assert spec.use_responses_api is True

    def test_accepts_capability_profile_fields(self):
        """New capability profile metadata is parsed correctly."""
        data = _minimal_model_data()
        data["thinking_modes"] = ["adaptive"]
        data["supports_sampling_overrides"] = False
        data["supports_1m_context"] = True
        data["supports_top_p"] = False
        data["token_estimate_multiplier"] = 1.35

        spec = _parse_model_spec("test", data)
        assert spec.thinking_modes == ("adaptive",)
        assert spec.supports_sampling_overrides is False
        assert spec.supports_1m_context is True
        assert spec.supports_top_p is False
        assert spec.token_estimate_multiplier == 1.35


class TestAliasValidation:
    """Tests for alias validation."""

    def test_rejects_alias_to_unknown_model(self):
        """Alias pointing to non-existent model raises error."""
        raw = _minimal_catalog_data()
        raw["aliases"] = {"alias/model": "nonexistent-model"}

        with pytest.raises(ModelCatalogError, match="points to unknown model"):
            _validate_and_build_catalog(raw)

    def test_rejects_alias_pointing_to_non_model(self):
        """Alias pointing to non-existent model (including alias names) raises error.

        Note: Since alias targets must be in the models dict, an alias that
        tries to point to another alias name will fail the "unknown model" check.
        This effectively prevents alias chaining.
        """
        raw = _minimal_catalog_data()
        raw["aliases"] = {
            "alias1": "test-model",
            "alias2": "alias1",  # alias1 is not in models, so this fails
        }

        with pytest.raises(ModelCatalogError, match="points to unknown model"):
            _validate_and_build_catalog(raw)

    def test_accepts_valid_alias(self):
        """Valid alias to existing model is accepted."""
        raw = _minimal_catalog_data()
        raw["aliases"] = {"openai/test-model": "test-model"}

        catalog = _validate_and_build_catalog(raw)
        assert catalog.aliases["openai/test-model"] == "test-model"


class TestTagsValidation:
    """Tests for tags field validation."""

    def test_rejects_non_list_tags(self):
        """Tags that is not a list raises error."""
        data = _minimal_model_data()
        data["tags"] = "code-gen"  # Should be a list

        with pytest.raises(ModelCatalogError, match="tags must be a list"):
            _parse_model_spec("test", data)

    def test_accepts_empty_tags(self):
        """Empty tags list is valid."""
        data = _minimal_model_data()
        data["tags"] = []

        spec = _parse_model_spec("test", data)
        assert spec.tags == ()

    def test_accepts_tags_list(self):
        """Tags list is converted to tuple."""
        data = _minimal_model_data()
        data["tags"] = ["code-gen", "fast"]

        spec = _parse_model_spec("test", data)
        assert spec.tags == ("code-gen", "fast")


class TestTypeValidation:
    """Tests for TypeSpec dataclass validation."""

    def test_temperature_spec_validates_range(self):
        """TemperatureSpec validates min <= default <= max."""
        with pytest.raises(ValueError, match="Temperature invariant violated"):
            TemperatureSpec(min=2.0, default=1.0, max=3.0)

    def test_model_spec_validates_positive_values(self):
        """ModelSpec validates positive numeric fields."""
        temp = TemperatureSpec(min=0.0, default=1.0, max=2.0)

        with pytest.raises(ValueError, match="context_window_tokens must be > 0"):
            ModelSpec(
                friendly_name="Test",
                intelligence_score=50,
                context_window_tokens=0,
                max_output_tokens=1000,
                supports_thinking=False,
                supports_images=False,
                temperature_constraint="range",
                temperature=temp,
            )


class TestCatalogEffortRankAlignment:
    """Ensure every reasoning effort string in the catalog is recognized by the proxy's _EFFORT_RANK.

    Prevents drift: adding a new effort level to model_catalog.yaml without
    updating _EFFORT_RANK in server.py would silently misrank comparisons.
    """

    def test_all_catalog_efforts_exist_in_effort_rank(self):
        """Every litellm_reasoning_efforts value in the real catalog must be a key in _EFFORT_RANK."""
        catalog = load_model_catalog(force_reload=True)
        missing: list[tuple[str, str]] = []

        for model_id, spec in catalog.models.items():
            if spec.litellm_reasoning_efforts is None:
                continue
            for effort in spec.litellm_reasoning_efforts:
                if effort not in _EFFORT_RANK:
                    missing.append((model_id, effort))

        assert missing == [], (
            f"Catalog effort strings not in _EFFORT_RANK: "
            f"{[f'{m}: {e!r}' for m, e in missing]}. "
            f"Update _EFFORT_RANK in server.py to include these levels."
        )

    def test_all_catalog_defaults_exist_in_effort_rank(self):
        """Every default_reasoning_effort in the real catalog must be a key in _EFFORT_RANK."""
        catalog = load_model_catalog(force_reload=True)
        missing: list[tuple[str, str]] = []

        for model_id, spec in catalog.models.items():
            if spec.default_reasoning_effort is None:
                continue
            if spec.default_reasoning_effort not in _EFFORT_RANK:
                missing.append((model_id, spec.default_reasoning_effort))

        assert missing == [], (
            f"Catalog default efforts not in _EFFORT_RANK: "
            f"{[f'{m}: {e!r}' for m, e in missing]}. "
            f"Update _EFFORT_RANK in server.py to include these levels."
        )

    def test_effort_rank_is_strictly_ordered(self):
        """_EFFORT_RANK values form a strict ordering (no duplicates except aliases)."""
        known_aliases = {"disable"}  # intentional alias for "none"
        non_alias = {k: v for k, v in _EFFORT_RANK.items() if k not in known_aliases and k is not None}

        values = list(non_alias.values())
        assert len(values) == len(set(values)), (
            f"Duplicate rank values in _EFFORT_RANK (excluding aliases): " f"{non_alias}"
        )


class TestClaude47CatalogProfile:
    """Tests for the explicit Claude Opus 4.7 catalog profile."""

    def test_opus_aliases_remain_stable_on_46(self):
        catalog = load_model_catalog(force_reload=True)

        assert catalog.resolve("opus") == "claude-opus-4-6"
        assert catalog.resolve("claude-opus") == "claude-opus-4-6"

    def test_opus_47_aliases_and_metadata(self):
        catalog = load_model_catalog(force_reload=True)

        assert catalog.resolve("opus-4-7") == "claude-opus-4-7"
        assert catalog.resolve("claude-opus-4.7") == "claude-opus-4-7"
        assert catalog.resolve("anthropic/claude-opus-4.7") == "claude-opus-4-7"

        spec = catalog.get("claude-opus-4-7")
        assert spec.thinking_modes == ("adaptive",)
        assert spec.supports_sampling_overrides is False
        assert spec.supports_1m_context is True
        assert spec.litellm_reasoning_efforts is not None
        assert "xhigh" in spec.litellm_reasoning_efforts
        assert spec.token_estimate_multiplier == 1.35


class TestOpenRouterOpenModelsCatalog:
    """Tests for curated OpenRouter open-model catalog entries."""

    def test_curated_openrouter_models_resolve(self):
        catalog = load_model_catalog(force_reload=True)

        expected = {
            "deepseek/deepseek-v4-flash": ("deepseek-v4-flash", 1048576, 384000),
            "deepseek/deepseek-v4-pro": ("deepseek-v4-pro", 1048576, 384000),
            "moonshotai/kimi-k2.5": ("kimi-k2.5", 262144, 262144),
            "moonshotai/kimi-k2.6": ("kimi-k2.6", 32768, 32768),
            "qwen/qwen3.6-flash": ("qwen3.6-flash", 1000000, 65536),
            "qwen/qwen3.6-plus": ("qwen3.6-plus", 1000000, 65536),
            "qwen/qwen3.6-max-preview": ("qwen3.6-max-preview", 262144, 65536),
            "qwen/qwen3-coder": ("qwen3-coder", 262144, 65536),
            "minimax/minimax-m2.5": ("minimax-m2.5", 196608, 196608),
            "minimax/minimax-m2.7": ("minimax-m2.7", 196608, 131072),
            "z-ai/glm-4.7-flash": ("glm-4.7-flash", 202752, 16384),
            "z-ai/glm-5.1": ("glm-5.1", 202752, 202752),
            "google/gemma-4-31b-it": ("gemma-4-31b-it", 262144, 16384),
        }

        for alias, (canonical, context_tokens, output_tokens) in expected.items():
            assert catalog.resolve(alias) == canonical
            spec = catalog.get(canonical)
            assert spec.context_window_tokens == context_tokens
            assert spec.max_output_tokens == output_tokens

    def test_curated_openrouter_multimodal_flags(self):
        catalog = load_model_catalog(force_reload=True)

        for model_id in (
            "kimi-k2.5",
            "kimi-k2.6",
            "qwen3.6-flash",
            "qwen3.6-plus",
            "gemma-4-31b-it",
        ):
            assert catalog.get(model_id).supports_images is True

        assert catalog.get("qwen3.6-max-preview").supports_images is False
        assert catalog.get("qwen3-coder").supports_thinking is False


class TestSystemPromptAddendumValidation:
    """Tests for system_prompt_addendum field validation at catalog parse time."""

    def test_defaults_to_none(self):
        data = _minimal_model_data()
        spec = _parse_model_spec("test", data)
        assert spec.system_prompt_addendum is None

    def test_parses_valid_path(self):
        data = _minimal_model_data()
        data["system_prompt_addendum"] = "system_prompt_addendums/openai.md"
        spec = _parse_model_spec("test", data)
        assert spec.system_prompt_addendum == "system_prompt_addendums/openai.md"

    def test_rejects_bad_prefix(self):
        data = _minimal_model_data()
        data["system_prompt_addendum"] = "wrong_dir/openai.md"
        with pytest.raises(ModelCatalogError, match="system_prompt_addendum must be"):
            _parse_model_spec("test", data)

    def test_rejects_bad_extension(self):
        data = _minimal_model_data()
        data["system_prompt_addendum"] = "system_prompt_addendums/openai.txt"
        with pytest.raises(ModelCatalogError, match="system_prompt_addendum must be"):
            _parse_model_spec("test", data)

    def test_rejects_missing_resource(self):
        data = _minimal_model_data()
        data["system_prompt_addendum"] = "system_prompt_addendums/nonexistent.md"
        with pytest.raises(ModelCatalogError, match="resource not found"):
            _parse_model_spec("test", data)

    def test_openai_models_have_addendum(self):
        catalog = load_model_catalog()
        spec = catalog.get("gpt-5.5")
        assert spec.system_prompt_addendum == "system_prompt_addendums/openai.md"

    def test_gemini_models_have_addendum(self):
        catalog = load_model_catalog()
        spec = catalog.get("gemini-3.1-pro-preview")
        assert spec.system_prompt_addendum == "system_prompt_addendums/gemini.md"

    def test_claude_models_have_no_addendum(self):
        catalog = load_model_catalog()
        spec = catalog.get("claude-opus-4-6")
        assert spec.system_prompt_addendum is None
