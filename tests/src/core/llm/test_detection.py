"""Tests for provider detection."""

import pytest

from forge.core.llm.detection import (
    LITELLM_LOCAL_PREFIXES,
    LITELLM_REMOTE_PREFIXES,
    detect_provider,
    is_implemented,
)


class TestDetectProvider:
    """Tests for detect_provider function."""

    @pytest.mark.parametrize(
        "model,expected",
        [
            # Remote LiteLLM prefixes
            ("openai/gpt-5.2", "litellm_remote"),
            ("openai/gpt-4o-mini", "litellm_remote"),
            ("anthropic/claude-sonnet-4", "litellm_remote"),
            ("vertex_ai/gemini-2.5-pro", "litellm_remote"),
            ("bedrock/anthropic.claude-3", "litellm_remote"),
            ("replicate/meta/llama-3", "litellm_remote"),
            ("together_ai/mistral-7b", "litellm_remote"),
            # Local LiteLLM prefixes
            ("gemini/gemini-2.0-flash", "litellm_local"),
            ("gemini/gemini-1.5-pro", "litellm_local"),
        ],
    )
    def test_prefixed_models(self, model: str, expected: str):
        assert detect_provider(model) == expected

    def test_case_insensitive(self):
        """Provider detection is case-insensitive."""
        assert detect_provider("OpenAI/gpt-5.2") == "litellm_remote"
        assert detect_provider("VERTEX_AI/gemini-2.5-pro") == "litellm_remote"
        assert detect_provider("Gemini/gemini-2.0-flash") == "litellm_local"

    def test_unprefixed_model_raises(self):
        """Unprefixed models are not supported in core.llm v1."""
        with pytest.raises(ValueError, match="Unprefixed model ID"):
            detect_provider("gpt-5.2")

        with pytest.raises(ValueError, match="Unprefixed model ID"):
            detect_provider("claude-sonnet-4")

        with pytest.raises(ValueError, match="Unprefixed model ID"):
            detect_provider("gemini-2.0-flash")

    def test_unknown_prefix_raises_value_error(self):
        """Unknown prefixes are rejected (fail-closed)."""
        with pytest.raises(ValueError, match="Unknown model prefix"):
            detect_provider("custom_provider/some-model")

    def test_error_message_suggests_prefix(self):
        """Error message should suggest using a prefix."""
        with pytest.raises(ValueError) as exc_info:
            detect_provider("gpt-4")
        assert "openai/gpt-4" in str(exc_info.value) or "prefixed" in str(exc_info.value)


class TestIsImplemented:
    """Tests for is_implemented function."""

    def test_litellm_providers_implemented(self):
        assert is_implemented("litellm_remote") is True
        assert is_implemented("litellm_local") is True

    def test_deferred_providers_not_implemented(self):
        assert is_implemented("anthropic") is False

    def test_openrouter_implemented(self):
        """OpenRouter has a client implementation."""
        assert is_implemented("openrouter") is True


class TestPrefixConstants:
    """Tests for prefix constants."""

    def test_remote_prefixes_exist(self):
        assert "openai/" in LITELLM_REMOTE_PREFIXES
        assert "anthropic/" in LITELLM_REMOTE_PREFIXES
        assert "vertex_ai/" in LITELLM_REMOTE_PREFIXES

    def test_local_prefixes_exist(self):
        assert "gemini/" in LITELLM_LOCAL_PREFIXES
