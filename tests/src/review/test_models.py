"""Tests for forge.review.models."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from forge.core.models.catalog import get_compact_name, get_default_model
from forge.review.models import (
    AVAILABLE_MODELS,
    DEFAULT_MODELS,
    MODEL_ALIASES,
    ModelSpec,
    MultiReviewOutput,
    ReviewResult,
    available_model_specs,
    check_model_availability,
    resolve_model_specs,
)

# DEFAULT_MODELS keys use canonical model names; compact names remain accepted aliases.
OPENAI_DEFAULT = get_default_model("openai", "opus")
GEMINI_DEFAULT = get_default_model("gemini", "opus")
GEMINI_COMPACT = get_compact_name(GEMINI_DEFAULT)
ANTHROPIC_DEFAULT = get_default_model("anthropic", "opus")


class TestModelSpec:
    def test_dataclass_fields(self):
        spec = ModelSpec(name="test", proxy="my-proxy", model_flag="opus", description="Test")
        assert spec.name == "test"
        assert spec.proxy == "my-proxy"
        assert spec.model_flag == "opus"

    def test_none_proxy_for_direct(self):
        spec = ModelSpec(name="direct", proxy=None, model_flag="opus", description="Direct")
        assert spec.proxy is None

    def test_prompt_defaults_to_none(self):
        spec = ModelSpec(name="test", proxy="p", model_flag=None, description="Test")
        assert spec.prompt is None

    def test_prompt_can_be_set(self):
        spec = ModelSpec(name="test", proxy="p", model_flag=None, description="Test", prompt="custom")
        assert spec.prompt == "custom"


class TestDefaultModels:
    def test_default_quorum_names_are_explicit(self):
        assert list(DEFAULT_MODELS) == [OPENAI_DEFAULT, GEMINI_DEFAULT, "claude-opus"]

    def test_has_expected_entries(self):
        assert OPENAI_DEFAULT in DEFAULT_MODELS
        assert GEMINI_DEFAULT in DEFAULT_MODELS
        assert "claude-opus" in DEFAULT_MODELS

    def test_gpt_uses_proxy(self):
        assert DEFAULT_MODELS[OPENAI_DEFAULT].proxy == "openrouter-openai"

    def test_gemini_uses_proxy(self):
        assert DEFAULT_MODELS[GEMINI_DEFAULT].proxy == "openrouter-gemini"

    def test_compact_gemini_alias_is_accepted(self):
        assert MODEL_ALIASES[GEMINI_COMPACT] == GEMINI_DEFAULT

        specs = resolve_model_specs(GEMINI_COMPACT)

        assert [s.name for s in specs] == [GEMINI_DEFAULT]
        assert [s.effective_worker_id for s in specs] == [GEMINI_COMPACT]

    def test_claude_is_direct(self):
        assert DEFAULT_MODELS["claude-opus"].proxy is None
        assert DEFAULT_MODELS["claude-opus"].model_flag == ANTHROPIC_DEFAULT
        assert DEFAULT_MODELS["claude-opus"].direct is True
        assert DEFAULT_MODELS["claude-opus"].direct_model == "claude-opus-4-6"

    def test_explicit_claude_47_is_selectable_not_default(self):
        assert "claude-opus-4.7" in AVAILABLE_MODELS
        assert "claude-opus-4.7" not in DEFAULT_MODELS

        spec = AVAILABLE_MODELS["claude-opus-4.7"]
        assert spec.direct is True
        assert spec.direct_model == "claude-opus-4-7"
        assert spec.prompt is not None
        assert spec.prompt_mode == "prefix"
        assert "file:line" in spec.prompt


class TestReviewResult:
    def test_success_result(self):
        r = ReviewResult(
            model_name="test",
            stdout="good output",
            stderr="",
            success=True,
            duration_seconds=1.5,
        )
        assert r.success
        assert r.error is None

    def test_failure_result(self):
        r = ReviewResult(
            model_name="test",
            stdout="",
            stderr="error output",
            success=False,
            duration_seconds=0.5,
            error="Exit code 1",
        )
        assert not r.success
        assert r.error == "Exit code 1"


class TestMultiReviewOutput:
    def test_successful_count(self):
        output = MultiReviewOutput(
            prompt="test",
            results=[
                ReviewResult("a", "ok", "", True, 1.0),
                ReviewResult("b", "", "", False, 1.0, error="fail"),
                ReviewResult("c", "ok", "", True, 1.0),
            ],
        )
        assert output.successful == 2
        assert output.failed == 1

    def test_empty_results(self):
        output = MultiReviewOutput(prompt="test")
        assert output.successful == 0
        assert output.failed == 0


class TestResolveModelSpecs:
    def test_none_returns_all_defaults(self):
        specs = resolve_model_specs(None)
        assert len(specs) == len(DEFAULT_MODELS)
        assert [s.name for s in specs] == list(DEFAULT_MODELS.keys())

    def test_empty_string_returns_all_defaults(self):
        specs = resolve_model_specs("")
        assert len(specs) == len(DEFAULT_MODELS)

    def test_specific_models_in_order(self):
        specs = resolve_model_specs(f"{OPENAI_DEFAULT},claude-opus")
        assert [s.name for s in specs] == [OPENAI_DEFAULT, "claude-opus"]

    def test_specific_direct_claude_versions_have_distinct_specs(self):
        specs = resolve_model_specs("claude-opus-4.6,claude-opus-4.7")

        assert [s.name for s in specs] == ["claude-opus-4.6", "claude-opus-4.7"]
        assert [s.effective_worker_id for s in specs] == ["claude-opus-4.6", "claude-opus-4.7"]
        assert [s.direct_model for s in specs] == ["claude-opus-4-6", "claude-opus-4-7"]

    def test_unknown_model_raises(self):
        with pytest.raises(ValueError, match="nonexistent"):
            resolve_model_specs("nonexistent")

    def test_mixed_valid_invalid_raises(self):
        with pytest.raises(ValueError, match="nonexistent"):
            resolve_model_specs(f"{OPENAI_DEFAULT},nonexistent")

    def test_available_model_specs_includes_selectable_extras(self):
        names = [spec.name for spec in available_model_specs()]

        assert "claude-opus" in names
        assert "claude-opus-4.6" in names
        assert "claude-opus-4.6-1m" in names
        assert "claude-opus-4.7" in names
        assert "deepseek-v4-pro" in names
        assert "minimax-m2.7" in names


DEEPSEEK_DEFAULT = get_default_model("deepseek", "opus")
MINIMAX_DEFAULT = get_default_model("minimax", "opus")
QWEN_DEFAULT = get_default_model("qwen", "opus")
GLM_DEFAULT = get_default_model("glm", "opus")
KIMI_DEFAULT = get_default_model("kimi", "opus")

_OSS_FAMILIES = {
    "deepseek": ("openrouter-deepseek", DEEPSEEK_DEFAULT),
    "minimax": ("openrouter-minimax", MINIMAX_DEFAULT),
    "qwen": ("openrouter-qwen", QWEN_DEFAULT),
    "glm": ("openrouter-glm", GLM_DEFAULT),
    "kimi": ("openrouter-kimi", KIMI_DEFAULT),
}


class TestOssWorkflowModels:
    """Open-source models are selectable but not in the default quorum."""

    @pytest.mark.parametrize("family,proxy,model", [(f, p, m) for f, (p, m) in _OSS_FAMILIES.items()])
    def test_oss_model_is_selectable_not_default(self, family, proxy, model):
        assert model in AVAILABLE_MODELS, f"{family} opus model '{model}' not in AVAILABLE_MODELS"
        assert model not in DEFAULT_MODELS

        spec = AVAILABLE_MODELS[model]
        assert spec.proxy == proxy
        assert spec.direct is False
        assert spec.model_flag is None

    def test_resolve_cheap_pair(self):
        specs = resolve_model_specs(f"{DEEPSEEK_DEFAULT},{MINIMAX_DEFAULT}")
        assert [s.name for s in specs] == [DEEPSEEK_DEFAULT, MINIMAX_DEFAULT]
        assert [s.proxy for s in specs] == ["openrouter-deepseek", "openrouter-minimax"]

    def test_resolve_mixed_oss_and_default(self):
        specs = resolve_model_specs(f"{DEEPSEEK_DEFAULT},{OPENAI_DEFAULT}")
        assert [s.name for s in specs] == [DEEPSEEK_DEFAULT, OPENAI_DEFAULT]


def _spec(
    name: str = "test-model",
    proxy: str | None = "test-proxy",
) -> ModelSpec:
    return ModelSpec(name=name, proxy=proxy, model_flag=None, description="Test")


class TestCheckModelAvailability:
    @patch(
        "forge.core.reactive.proxy.check_proxy_reachable",
        return_value=(True, "", "http://localhost:8085"),
    )
    @patch(
        "forge.core.auth.template_secrets.resolve_env_or_credential",
        return_value="sk-test",
    )
    def test_all_ready(self, _mock_cred, _mock_proxy):
        specs = [_spec("a", proxy="p1"), _spec("b", proxy=None)]
        result = check_model_availability(specs)
        assert all(a.status == "ready" for a in result)
        assert len(result) == 2

    @patch(
        "forge.core.reactive.proxy.check_proxy_reachable",
        return_value=(False, "Proxy 'p1' not responding at http://localhost:8085", "http://localhost:8085"),
    )
    def test_proxy_unavailable(self, _mock_proxy):
        result = check_model_availability([_spec("a", proxy="p1")])
        assert result[0].status == "unavailable"
        assert "not responding" in result[0].reason

    @patch(
        "forge.core.auth.template_secrets.resolve_env_or_credential",
        return_value="sk-test",
    )
    def test_direct_ready_with_key(self, _mock_cred):
        result = check_model_availability([_spec("opus", proxy=None)])
        assert result[0].status == "ready"

    @patch(
        "forge.core.auth.template_secrets.resolve_env_or_credential",
        return_value=None,
    )
    def test_direct_unavailable_no_key(self, _mock_cred):
        result = check_model_availability([_spec("opus", proxy=None)])
        assert result[0].status == "unavailable"
        assert "ANTHROPIC_API_KEY" in result[0].reason

    @patch(
        "forge.core.auth.template_secrets.resolve_env_or_credential",
        return_value="sk-test",
    )
    def test_explicit_direct_worker_ready_with_key(self, _mock_cred):
        result = check_model_availability([AVAILABLE_MODELS["claude-opus-4.7"]])

        assert result[0].status == "ready"

    @patch(
        "forge.core.reactive.proxy.check_proxy_reachable",
        side_effect=RuntimeError("unexpected"),
    )
    def test_unexpected_error(self, _mock_proxy):
        result = check_model_availability([_spec("a", proxy="p1")])
        assert result[0].status == "error"
        assert "unexpected" in result[0].reason

    @patch(
        "forge.core.reactive.proxy.check_proxy_reachable",
        return_value=(True, "", "http://localhost:8085"),
    )
    def test_deduplicates_proxy_checks(self, mock_proxy):
        specs = [_spec("a", proxy="same"), _spec("b", proxy="same")]
        result = check_model_availability(specs)
        assert len(result) == 2
        assert all(a.status == "ready" for a in result)
        mock_proxy.assert_called_once()

    @patch(
        "forge.core.reactive.proxy.check_proxy_reachable",
        return_value=(True, "", "http://localhost:8085"),
    )
    @patch(
        "forge.core.auth.template_secrets.resolve_env_or_credential",
        return_value="sk-test",
    )
    def test_defaults_to_all_models(self, _mock_cred, _mock_proxy):
        result = check_model_availability()
        assert len(result) == len(DEFAULT_MODELS)
