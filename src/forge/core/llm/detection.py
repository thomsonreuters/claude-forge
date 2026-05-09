"""Provider detection for LLM client routing.

This module provides prefix-based provider detection for model IDs.
core.llm only supports prefixed canonical IDs (e.g., "openai/gpt-5.2").
"""

from typing import Literal

# Provider type - all supported providers (some may not be implemented yet)
ProviderType = Literal["litellm_remote", "litellm_local", "anthropic", "openrouter"]

# Prefixes that route to remote LiteLLM
LITELLM_REMOTE_PREFIXES = (
    "openai/",
    "anthropic/",
    "vertex_ai/",
    "bedrock/",
    "replicate/",
    "together_ai/",
)

# Prefixes that route to local LiteLLM (personal API keys)
LITELLM_LOCAL_PREFIXES = ("gemini/",)


def detect_provider(model: str) -> ProviderType:
    """Detect provider from prefixed model ID.

    IMPORTANT: core.llm only supports prefixed canonical IDs.
    Unprefixed models (claude-*, gpt-*) are NOT supported in v1.

    Args:
        model: Model identifier with provider prefix (e.g., "openai/gpt-5.2")

    Returns:
        ProviderType indicating which provider should handle this model.

    Raises:
        ValueError: If model ID is not prefixed (unprefixed models not supported).

    Examples:
        >>> detect_provider("openai/gpt-5.2")
        'litellm_remote'
        >>> detect_provider("vertex_ai/gemini-3.1-pro-preview")
        'litellm_remote'
        >>> detect_provider("gemini/gemini-2.0-flash")
        'litellm_local'
        >>> detect_provider("anthropic/claude-sonnet-4")
        'litellm_remote'
    """
    clean_name = model.lower()

    # Check for remote LiteLLM prefixes
    if any(clean_name.startswith(prefix) for prefix in LITELLM_REMOTE_PREFIXES):
        return "litellm_remote"

    # Check for local LiteLLM prefixes
    if any(clean_name.startswith(prefix) for prefix in LITELLM_LOCAL_PREFIXES):
        return "litellm_local"

    # Unprefixed models are not supported in core.llm v1
    # The user should use prefixed model IDs
    if "/" not in model:
        raise ValueError(
            f"Unprefixed model ID '{model}' not supported in core.llm. "
            f"Use prefixed canonical IDs like 'openai/{model}' or 'anthropic/{model}'."
        )

    # Unknown prefix -- fail-closed (reject rather than silently route to wrong backend)
    known = sorted({*LITELLM_REMOTE_PREFIXES, *LITELLM_LOCAL_PREFIXES})
    raise ValueError(
        f"Unknown model prefix in '{model}'. Known prefixes: {', '.join(known)}. "
        "Use a prefixed canonical ID like 'openai/gpt-5.2' or 'gemini/gemini-3.1-pro-preview'."
    )


def is_implemented(provider: ProviderType) -> bool:
    """Check if a provider has an implemented client.

    Args:
        provider: Provider type to check.

    Returns:
        True if the provider's client is implemented, False otherwise.
    """
    return provider in ("litellm_remote", "litellm_local", "openrouter")
