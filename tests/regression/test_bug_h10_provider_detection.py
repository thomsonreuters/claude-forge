"""Regression test for H10: unknown model prefix silently routed to remote LiteLLM.

Bug: detect_provider() defaulted unknown model prefixes to "litellm_remote"
(commented as "fail-open"). A typo like "openaai/gpt-5.2" silently routed to the
remote LiteLLM endpoint instead of erroring. In environments without forge.config,
every unknown model routed to remote infra by default.

Fix: Treat unknown prefixes as errors (raise ValueError). Unprefixed models also
rejected with a helpful error message listing known prefixes.

Fixed in: src/forge/core/llm/detection.py (action plan Step 4, H10)
"""

import pytest

pytestmark = pytest.mark.regression


def test_unknown_prefix_raises_valueerror() -> None:
    """Typo in prefix must raise, not silently route to remote."""
    from forge.core.llm.detection import detect_provider

    with pytest.raises(ValueError, match="Unknown model prefix"):
        detect_provider("openaai/gpt-5.2")


def test_unprefixed_model_raises_valueerror() -> None:
    """Bare model name without provider prefix must be rejected."""
    from forge.core.llm.detection import detect_provider

    with pytest.raises(ValueError, match="Unprefixed model ID"):
        detect_provider("claude-opus-4")


def test_error_message_lists_known_prefixes() -> None:
    """Error message must include the list of valid prefixes for discoverability."""
    from forge.core.llm.detection import detect_provider

    with pytest.raises(ValueError, match="openai/") as exc_info:
        detect_provider("typo/model")

    msg = str(exc_info.value)
    assert "gemini/" in msg
    assert "openai/" in msg


@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.2", "litellm_remote"),
        ("anthropic/claude-sonnet-4", "litellm_remote"),
        ("vertex_ai/gemini-3.1-pro-preview", "litellm_remote"),
        ("gemini/gemini-2.5-flash", "litellm_local"),
    ],
)
def test_known_prefixes_still_work(model: str, expected: str) -> None:
    """Valid prefixed model IDs must route correctly."""
    from forge.core.llm.detection import detect_provider

    assert detect_provider(model) == expected
