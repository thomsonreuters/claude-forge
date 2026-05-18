"""OpenRouter client implementation.

Uses OpenAI SDK to call OpenRouter's API directly (no LiteLLM).
OpenRouter is OpenAI-compatible, so this is a thin wrapper that adds
OpenRouter-specific headers and translates parameters to OpenRouter's format.
"""

import json
import logging
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..credentials import CredentialManager
from ..detection import ProviderType
from ..errors import AuthenticationError, ProviderError
from ..types import (
    CompletionResponse,
    Message,
    ModelHyperparameters,
    StreamEvent,
    ToolCallDelta,
)
from .base import estimate_message_tokens, merge_hyperparams
from .openai_compat import (
    ToolCallAccumulator,
    build_chat_completion_kwargs,
    extract_cached_tokens,
    is_retryable_error,
    message_to_openai,
    openai_response_to_completion,
)

logger = logging.getLogger(__name__)


class OpenRouterClient:
    """OpenRouter client using OpenAI SDK.

    Calls OpenRouter's API directly at https://openrouter.ai/api/v1.
    Uses Chat Completions only (no Responses API).
    """

    def __init__(
        self,
        model: str,
        provider: ProviderType,
        credentials: CredentialManager | None = None,
        default_hyperparams: ModelHyperparameters | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._credentials = credentials or CredentialManager.default()
        self._default_hyperparams = default_hyperparams
        self._client: AsyncOpenAI | None = None

    @property
    def model(self) -> str:
        return self._model

    async def _get_client(self) -> AsyncOpenAI:
        if self._client is not None:
            return self._client

        creds = await self._credentials.get_credentials(self._provider)

        self._client = AsyncOpenAI(
            api_key=creds["api_key"],
            base_url=creds["base_url"],
            default_headers=creds.get("extra_headers", {}),
        )
        return self._client

    @staticmethod
    def _translate_params(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Translate Forge params to OpenRouter's API format.

        OpenRouter uses ``reasoning: {effort: ...}`` (object) and top-level
        ``verbosity``, passed via ``extra_body`` since the OpenAI SDK does not
        accept them as direct kwargs.
        """
        extra_body: dict[str, Any] = kwargs.pop("extra_body", None) or {}
        effort = kwargs.pop("reasoning_effort", None)
        if effort is not None:
            extra_body["reasoning"] = {"effort": effort}
        verbosity = kwargs.pop("verbosity", None)
        if verbosity is not None:
            extra_body["verbosity"] = verbosity
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    _is_retryable_error = staticmethod(is_retryable_error)

    @retry(
        retry=retry_if_exception(lambda e: isinstance(e, Exception) and is_retryable_error(e)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _make_completion_request(
        self,
        client: AsyncOpenAI,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        merged_params: ModelHyperparameters,
    ) -> CompletionResponse:
        kwargs = build_chat_completion_kwargs(self._model, messages, tools, merged_params)
        kwargs = self._translate_params(kwargs)
        response = await client.chat.completions.create(**kwargs)
        return openai_response_to_completion(response, self._provider)

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        hyperparams: ModelHyperparameters | None = None,
    ) -> CompletionResponse:
        merged_params = merge_hyperparams(self._default_hyperparams, hyperparams)
        client = await self._get_client()

        try:
            return await self._make_completion_request(client, messages, tools, merged_params)
        except (ProviderError, AuthenticationError):
            raise
        except Exception as e:
            error_str = str(e).lower()
            if "authentication" in error_str or "unauthorized" in error_str:
                await self._credentials.invalidate(self._provider)
                await self._close_client()
                raise AuthenticationError(self._provider, str(e)) from e
            raise ProviderError(self._provider, e) from e

    async def _close_client(self) -> None:
        """Close and discard the cached HTTP client.

        Forces credential re-resolution on next request, preventing
        stale credentials from being reused after invalidation.
        """
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        hyperparams: ModelHyperparameters | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        merged_params = merge_hyperparams(self._default_hyperparams, hyperparams)
        client = await self._get_client()

        accumulator = ToolCallAccumulator()
        usage_data: dict[str, int] | None = None

        try:
            kwargs = build_chat_completion_kwargs(self._model, messages, tools, merged_params)
            kwargs = self._translate_params(kwargs)
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}

            stream_resp = await client.chat.completions.create(**kwargs)

            async for chunk in stream_resp:
                if chunk.usage:
                    usage_data = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }
                    cached = extract_cached_tokens(chunk.usage)
                    if cached:
                        usage_data["cached_tokens"] = cached

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                if delta.content:
                    yield StreamEvent(type="text_delta", text=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx is None and len(delta.tool_calls) == 1:
                            idx = accumulator.default_index()
                        tool_delta = ToolCallDelta(
                            index=idx,
                            id=tc_delta.id,
                            name=tc_delta.function.name if tc_delta.function else None,
                            arguments_json=(tc_delta.function.arguments or "") if tc_delta.function else "",
                        )
                        accumulator.add_delta(tool_delta)
                        yield StreamEvent(type="tool_call_delta", tool_call_delta=tool_delta)

            if usage_data:
                yield StreamEvent(type="usage", usage=usage_data)

            final_tool_calls = accumulator.finalize() if accumulator.has_pending() else None
            yield StreamEvent(
                type="response_end",
                tool_calls=final_tool_calls,
                usage=usage_data,
            )

        except Exception as e:
            error_str = str(e).lower()
            if "authentication" in error_str or "unauthorized" in error_str:
                await self._credentials.invalidate(self._provider)
                await self._close_client()
            yield StreamEvent(type="error", error=str(e))

    async def count_tokens(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        openai_messages = [message_to_openai(m) for m in messages]
        total = estimate_message_tokens(openai_messages)

        if tools:
            tools_json = json.dumps(tools)
            total += len(tools_json) // 4

        return total
