"""Shared helpers for OpenAI-compatible LLM clients.

Used by both LiteLLMClient and OpenRouterClient. Extracted here so the
OpenRouter client has no import dependency on LiteLLM.
"""

import json
import logging
from typing import Any

from openai import APIError, APIStatusError, RateLimitError

from ..errors import ProviderError
from ..types import (
    CompletionResponse,
    Message,
    ModelHyperparameters,
    ToolCall,
    ToolCallDelta,
)

logger = logging.getLogger(__name__)


def is_retryable_error(error: Exception) -> bool:
    """Return True if the error should trigger tenacity retry.

    Only retries transient errors (rate limits, server errors).
    Auth failures (401/403) are excluded -- retrying with the same
    bad credentials just adds ~14s of delay.
    """
    if isinstance(error, APIStatusError):
        return error.status_code not in (400, 401, 403)
    if isinstance(error, (RateLimitError, APIError)):
        return True
    return False


def extract_cached_tokens(usage: object) -> int:
    """Extract cached_tokens from a usage object's prompt_tokens_details.

    LiteLLM and OpenRouter pass through provider cache metrics in
    ``usage.prompt_tokens_details.cached_tokens``.  The field may be an
    object (SDK model) or a plain dict depending on the response path.

    Returns 0 if no cache data is present.
    """
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details is None and isinstance(usage, dict):
        prompt_details = usage.get("prompt_tokens_details")
    if not prompt_details:
        return 0
    if isinstance(prompt_details, dict):
        raw = prompt_details.get("cached_tokens", 0) or 0
    else:
        raw = getattr(prompt_details, "cached_tokens", 0) or 0
    return int(raw)


def message_to_openai(msg: Message) -> dict[str, Any]:
    """Convert canonical Message to OpenAI chat completion format."""
    result: dict[str, Any] = {"role": msg.role, "content": msg.content}

    if msg.tool_call_id:
        result["tool_call_id"] = msg.tool_call_id

    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in msg.tool_calls
        ]

    return result


def build_chat_completion_kwargs(
    model: str,
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    hyperparams: ModelHyperparameters,
) -> dict[str, Any]:
    """Build kwargs for OpenAI chat.completions.create()."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [message_to_openai(m) for m in messages],
        "max_tokens": hyperparams.max_tokens,
    }

    if hyperparams.temperature is not None:
        kwargs["temperature"] = hyperparams.temperature

    if hyperparams.top_p is not None:
        kwargs["top_p"] = hyperparams.top_p

    if hyperparams.reasoning_effort is not None:
        kwargs["reasoning_effort"] = hyperparams.reasoning_effort

    if tools:
        kwargs["tools"] = tools

    if "openai" in hyperparams.extra:
        kwargs.update(hyperparams.extra["openai"])

    return kwargs


def openai_response_to_completion(response: Any, provider: str) -> CompletionResponse:
    """Convert OpenAI ChatCompletion response to canonical CompletionResponse."""
    if hasattr(response, "error") and response.error:
        error_msg = response.error.get("message", "Unknown error")
        error_code = response.error.get("code", "unknown")
        raise ProviderError(
            provider,
            Exception(f"API error (code={error_code}): {error_msg}"),
        )

    if not response.choices:
        raise ProviderError(
            provider,
            Exception("No choices in response"),
        )

    choice = response.choices[0]
    message = choice.message

    text = message.content or ""

    tool_calls = None
    if message.tool_calls:
        tool_calls = []
        for tc in message.tool_calls:
            try:
                arguments = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments,
                )
            )

    usage = None
    if response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        cached = extract_cached_tokens(response.usage)
        if cached:
            usage["cached_tokens"] = cached

    return CompletionResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        raw=response.model_dump(),
    )


class ToolCallAccumulator:
    """Accumulates streaming tool call deltas into complete ToolCalls.

    During streaming, tool calls arrive as fragments (id, name, argument chunks).
    OpenAI sends `id` only on the first chunk; subsequent chunks use `index`
    to correlate. This accumulator uses index-based lookup to handle both.
    """

    def __init__(self) -> None:
        self._pending: dict[int, ToolCallDelta] = {}

    def add_delta(self, delta: ToolCallDelta) -> None:
        """Add a streaming delta to the accumulator."""
        idx = delta.index
        if idx is None:
            return

        if idx not in self._pending:
            self._pending[idx] = ToolCallDelta(index=idx)

        existing = self._pending[idx]
        if delta.id:
            existing.id = delta.id
        if delta.name:
            existing.name = delta.name
        existing.arguments_json += delta.arguments_json

    def finalize(self) -> list[ToolCall]:
        """Parse accumulated deltas into complete ToolCalls.

        Returns tool calls sorted by index for deterministic ordering.
        """
        result = []
        for idx in sorted(self._pending):
            delta = self._pending[idx]
            if delta.id and delta.name:
                try:
                    arguments = json.loads(delta.arguments_json) if delta.arguments_json else {}
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse tool call arguments: {delta.arguments_json}")
                    arguments = {}

                result.append(
                    ToolCall(
                        id=delta.id,
                        name=delta.name,
                        arguments=arguments,
                    )
                )
            elif delta.arguments_json:
                logger.warning(
                    f"Dropping incomplete tool call at index {idx}: "
                    f"id={delta.id}, name={delta.name}, args_len={len(delta.arguments_json)}"
                )
        return result

    def has_pending(self) -> bool:
        """Check if there are any pending tool calls."""
        return len(self._pending) > 0
