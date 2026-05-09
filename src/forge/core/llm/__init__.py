"""Shared LLM client abstraction for Forge components.

This module provides a unified, async-first interface for calling LLMs
across different providers (LiteLLM, Anthropic).

Usage:
    # Async usage (Proxy)
    from forge.core.llm import get_client, Message

    client = get_client("openai/gpt-5.2")
    response = await client.complete([Message(role="user", content="Hello")])

    # Streaming
    async for event in client.stream(messages):
        if event.type == "text_delta":
            print(event.text, end="")

    # Sync usage (Guard)
    from forge.core.llm import get_client, SyncAdapter

    client = SyncAdapter(get_client("openai/gpt-5.2"))
    response = client.ask("Analyze this code...")
"""

import asyncio
from typing import Any

from .clients.litellm import LiteLLMClient
from .credentials import CredentialManager
from .detection import ProviderType, detect_provider, is_implemented
from .errors import (
    AuthenticationError,
    LLMError,
    NoApiKeyError,
    ProviderError,
    UnsupportedParamError,
)
from .protocols import LLMClient
from .types import (
    CompletionResponse,
    InjectionPoint,
    Message,
    ModelHyperparameters,
    PromptCachingConfig,
    PromptCachingPolicy,
    StreamEvent,
    ThinkingConfig,
    ToolCall,
    ToolCallDelta,
)

__all__ = [
    # Factory
    "get_client",
    "SyncAdapter",
    # Types
    "Message",
    "CompletionResponse",
    "StreamEvent",
    "ToolCall",
    "ToolCallDelta",
    "ModelHyperparameters",
    "ThinkingConfig",
    "PromptCachingConfig",
    "PromptCachingPolicy",
    "InjectionPoint",
    # Protocol
    "LLMClient",
    # Errors
    "LLMError",
    "NoApiKeyError",
    "AuthenticationError",
    "ProviderError",
    "UnsupportedParamError",
    # Detection
    "ProviderType",
    "detect_provider",
    # Credentials
    "CredentialManager",
]


def get_client(
    model: str,
    *,
    provider: ProviderType | None = None,
    credentials: CredentialManager | None = None,
    default_hyperparams: ModelHyperparameters | None = None,
) -> LLMClient:
    """Get an LLM client for the given model.

    The factory is sync; credential fetching is deferred to first
    complete()/stream() call. This allows sync code to construct
    clients without needing an event loop.

    Args:
        model: Model identifier with provider prefix (e.g., "openai/gpt-5.2").
        provider: Explicit provider override (detected from prefix if not provided).
        credentials: Custom credential manager (uses default if not provided).
        default_hyperparams: Default hyperparameters for all calls on this client.

    Returns:
        LLMClient instance for the appropriate provider.

    Raises:
        ValueError: If model ID is not prefixed (unprefixed models not supported).
        NotImplementedError: If the detected provider is not yet implemented.

    Examples:
        >>> client = get_client("openai/gpt-5.2")
        >>> client = get_client("vertex_ai/gemini-3.1-pro-preview")
        >>> client = get_client("gemini/gemini-2.0-flash")  # Local LiteLLM
    """
    resolved_provider = provider or detect_provider(model)
    creds_manager = credentials or CredentialManager.default()

    # Check if provider is implemented
    if not is_implemented(resolved_provider):
        if resolved_provider == "anthropic":
            raise NotImplementedError(
                f"Direct Anthropic client not yet implemented. "
                f"Use 'anthropic/{model.split('/')[-1] if '/' in model else model}' "
                f"prefix to route via LiteLLM."
            )
        else:
            raise NotImplementedError(f"Provider '{resolved_provider}' not yet implemented.")

    if resolved_provider == "openrouter":
        from .clients.openrouter import OpenRouterClient

        return OpenRouterClient(
            model=model,
            provider=resolved_provider,
            credentials=creds_manager,
            default_hyperparams=default_hyperparams,
        )

    return LiteLLMClient(
        model=model,
        provider=resolved_provider,
        credentials=creds_manager,
        default_hyperparams=default_hyperparams,
    )


class SyncAdapter:
    """Wraps async LLMClient for synchronous usage.

    CONSTRAINT: Cannot be used inside an event loop.
    asyncio.run() raises RuntimeError if a loop is running.
    Use the async client directly in async contexts.

    Usage:
        client = SyncAdapter(get_client("openai/gpt-5.2"))
        response = client.ask("Analyze this code...")
    """

    def __init__(self, client: LLMClient) -> None:
        """Initialize sync adapter.

        Args:
            client: Async LLMClient to wrap.
        """
        self._client = client

    @property
    def model(self) -> str:
        """The model this client is configured for."""
        return self._client.model

    def _check_no_running_loop(self) -> None:
        """Ensure we're not inside an event loop.

        Raises:
            RuntimeError: If called from inside an event loop.
        """
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "SyncAdapter cannot be used inside an event loop. " "Use the async client directly in async contexts."
            )
        except RuntimeError as e:
            # RuntimeError is raised when no loop is running - that's what we want
            if "no running event loop" not in str(e).lower():
                raise

    def ask(
        self,
        prompt: str,
        *,
        system: str | None = None,
        hyperparams: ModelHyperparameters | None = None,
    ) -> str:
        """Simple prompt-to-response interface.

        Args:
            prompt: User prompt.
            system: Optional system prompt.
            hyperparams: Optional hyperparameters.

        Returns:
            Response text from the model.
        """
        self._check_no_running_loop()

        messages = [Message(role="user", content=prompt)]
        if system:
            messages.insert(0, Message(role="system", content=system))

        response = asyncio.run(self._client.complete(messages, hyperparams=hyperparams))
        return response.text

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        hyperparams: ModelHyperparameters | None = None,
    ) -> CompletionResponse:
        """Synchronous completion with full control.

        Args:
            messages: List of messages in the conversation.
            tools: Optional list of tool definitions.
            hyperparams: Optional hyperparameters.

        Returns:
            CompletionResponse with text, optional tool_calls, and usage.
        """
        self._check_no_running_loop()
        return asyncio.run(self._client.complete(messages, tools=tools, hyperparams=hyperparams))
