"""LiteLLM client implementation.

Uses OpenAI SDK to communicate with LiteLLM endpoints (remote or local).
Supports both non-streaming and streaming completions with tool support.

GPT-5 Models:
    GPT-5 family models use the Responses API which supports tools, verbosity
    control, and reasoning_effort together. Chat Completions API is only used
    for non-GPT-5 models (it does NOT support reasoning_effort with function
    tools for GPT-5).
"""

import json
import logging
import ssl
import time
from typing import Any, AsyncGenerator

import httpx
from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from forge.runtime_config import get_runtime_config

from ..credentials import CredentialManager
from ..detection import ProviderType
from ..errors import AuthenticationError, ProviderError
from ..types import (
    CompletionResponse,
    Message,
    ModelHyperparameters,
    StreamEvent,
    ToolCall,
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


# GPT-5 family models that use the Responses API (supports tools + verbosity + reasoning_effort)
GPT5_MODELS = frozenset(
    {
        "gpt-5",
        "gpt-5-chat",
        "gpt-5-codex",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-5-pro",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5.1-mini",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.2-pro",
        "gpt-5.3-codex",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
    }
)


class LiteLLMClient:
    """LiteLLM client using OpenAI SDK.

    Supports both remote LiteLLM and local LiteLLM instances.
    Uses Chat Completions API for standard models, and Responses API for GPT-5
    family models (which supports tools, verbosity, and reasoning_effort together).
    """

    def __init__(
        self,
        model: str,
        provider: ProviderType,
        credentials: CredentialManager | None = None,
        default_hyperparams: ModelHyperparameters | None = None,
    ) -> None:
        """Initialize LiteLLM client.

        Args:
            model: Model identifier (e.g., "openai/gpt-5.5").
            provider: Provider type (litellm_remote or litellm_local).
            credentials: Credential manager (uses default if not provided).
            default_hyperparams: Default hyperparameters for all calls.
        """
        self._model = model
        self._provider = provider
        self._credentials = credentials or CredentialManager.default()
        self._default_hyperparams = default_hyperparams
        self._client: AsyncOpenAI | None = None

    @property
    def model(self) -> str:
        """The model this client is configured for."""
        return self._model

    async def _get_client(self) -> AsyncOpenAI:
        """Get or create the OpenAI client with credentials."""
        if self._client is not None:
            return self._client

        creds = await self._credentials.get_credentials(self._provider)

        http_client = None
        ssl_cert = creds.get("ssl_cert")
        if ssl_cert:
            # Custom SSL certificate (e.g., remote proxy root CA)
            ssl_context = ssl.create_default_context(cafile=ssl_cert)
            http_client = httpx.AsyncClient(verify=ssl_context)

        version = get_runtime_config().user_agent_claude_code_version or "unknown"
        self._client = AsyncOpenAI(
            api_key=creds["api_key"],
            base_url=creds["base_url"],
            http_client=http_client,
            default_headers={"User-Agent": f"claude-cli/{version} (external, cli)"},
        )
        return self._client

    _is_retryable_error = staticmethod(is_retryable_error)

    def _is_gpt5_model(self) -> bool:
        """Check if the current model is a GPT-5 family model.

        GPT-5 models support the Responses API with verbosity control.
        Uses exact match against known GPT-5 model names.

        Returns:
            True if the model is a GPT-5 family model.
        """
        model_name = self._model.split("/")[-1].lower()
        return model_name in GPT5_MODELS

    def _should_use_responses_api(
        self,
        tools: list[dict[str, Any]] | None,
        hyperparams: ModelHyperparameters,
    ) -> bool:
        """Determine if Responses API should be used.

        GPT-5 models always use Responses API which supports tools, verbosity,
        and reasoning_effort together. Chat Completions API does NOT support
        reasoning_effort with function tools for GPT-5.
        """
        return self._is_gpt5_model()

    @staticmethod
    def _convert_messages_for_responses(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert canonical Messages to Responses API structured input format.

        Handles tool call history by converting:
        - assistant messages with tool_calls -> assistant message + function_call items
        - tool role messages -> function_call_output items
        - system/user/assistant text -> standard role messages
        - multimodal content -> Responses API format (input_text, input_image)
        """
        input_items: list[dict[str, Any]] = []

        for msg in messages:
            content: Any = msg.content

            # Convert multimodal content to Responses API format
            if isinstance(content, list):
                converted_parts: list[dict[str, Any]] = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        converted_parts.append({"type": "input_text", "text": item.get("text", "")})
                    elif item.get("type") == "image_url":
                        image_data = item.get("image_url", {})
                        url = image_data.get("url", "") if isinstance(image_data, dict) else str(image_data)
                        converted_parts.append({"type": "input_image", "image_url": url})
                content = converted_parts if converted_parts else ""

            if msg.role in ("system", "user"):
                input_items.append({"role": msg.role, "content": content or ""})

            elif msg.role == "assistant":
                if content:
                    input_items.append({"role": "assistant", "content": content})
                # Convert tool_calls to Responses API function_call items
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        input_items.append(
                            {
                                "type": "function_call",
                                "call_id": tc.id,
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            }
                        )
                elif not content:
                    input_items.append({"role": "assistant", "content": ""})

            elif msg.role == "tool":
                # Convert tool result to Responses API function_call_output
                if isinstance(content, (dict, list)):
                    output_str = json.dumps(content)
                else:
                    output_str = str(content) if content else ""
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id or "",
                        "output": output_str,
                    }
                )

        return input_items

    @staticmethod
    def _convert_tools_for_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Chat Completions tool format to Responses API format.

        Chat Completions: {type: "function", function: {name, description, parameters}}
        Responses API:    {type: "function", name, description, parameters}
        """
        responses_tools = []
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                func = tool["function"]
                resp_tool: dict[str, Any] = {
                    "type": "function",
                    "name": func.get("name"),
                    "parameters": func.get("parameters", {}),
                }
                if func.get("description"):
                    resp_tool["description"] = func["description"]
                if func.get("strict") is not None:
                    resp_tool["strict"] = func["strict"]
                responses_tools.append(resp_tool)
            else:
                responses_tools.append(tool)
        return responses_tools

    @staticmethod
    def _parse_responses_output(response: Any, model: str) -> CompletionResponse:
        """Parse Responses API output into canonical CompletionResponse.

        Extracts text content and tool calls from the response output items.
        Checks response.status for incomplete/truncated responses.
        """
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for item in getattr(response, "output", []):
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", []):
                    if getattr(part, "type", None) == "output_text":
                        text_parts.append(getattr(part, "text", ""))
            elif item_type == "function_call":
                args_raw = getattr(item, "arguments", "{}")
                if isinstance(args_raw, dict):
                    arguments = args_raw
                else:
                    try:
                        arguments = json.loads(args_raw) if args_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                tool_calls.append(
                    ToolCall(
                        id=getattr(item, "call_id", ""),
                        name=getattr(item, "name", ""),
                        arguments=arguments,
                    )
                )

        text = "".join(text_parts)

        usage = None
        resp_usage = getattr(response, "usage", None)
        if resp_usage:
            input_tokens = getattr(resp_usage, "input_tokens", 0) or 0
            output_tokens = getattr(resp_usage, "output_tokens", 0) or 0
            usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            cached = extract_cached_tokens(resp_usage)
            if cached:
                usage["cached_tokens"] = cached

        status = getattr(response, "status", "completed")
        if status == "incomplete":
            finish_reason = "length"
        elif status in ("failed", "cancelled"):
            finish_reason = "error"
        elif tool_calls:
            finish_reason = "tool_calls"
        else:
            finish_reason = "stop"

        return CompletionResponse(
            text=text,
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
            raw={
                "id": getattr(response, "id", f"responses-{int(time.time())}"),
                "object": "responses",
                "model": model,
                "finish_reason": finish_reason,
            },
        )

    def _message_to_openai(self, msg: Message) -> dict[str, Any]:
        """Convert canonical Message to OpenAI format."""
        return message_to_openai(msg)

    def _openai_to_completion(self, response: Any) -> CompletionResponse:
        """Convert OpenAI response to canonical CompletionResponse."""
        return openai_response_to_completion(response, self._provider)

    def _build_request_kwargs(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        hyperparams: ModelHyperparameters,
    ) -> dict[str, Any]:
        """Build kwargs for OpenAI chat completion request."""
        kwargs = build_chat_completion_kwargs(self._model, messages, tools, hyperparams)

        # GPT-5 Chat Completions API doesn't support reasoning_effort with
        # function tools. Runs AFTER extras merge so callers can't reintroduce it.
        if tools and "reasoning_effort" in kwargs and self._is_gpt5_model():
            dropped = kwargs.pop("reasoning_effort")
            logger.warning(
                f"Stripped reasoning_effort={dropped} - "
                f"not supported with function tools on Chat Completions API for {self._model}"
            )

        return kwargs

    async def _complete_with_responses_api(
        self,
        client: AsyncOpenAI,
        messages: list[Message],
        hyperparams: ModelHyperparameters,
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResponse:
        """Complete using GPT-5 Responses API.

        The Responses API supports tools, verbosity, and reasoning_effort together.
        This is extracted as a separate method (without retry decorator) so that
        both complete() and stream() can call it without nesting retries (3x3=9).
        """
        input_items = self._convert_messages_for_responses(messages)

        # Responses API requires max_output_tokens >= 16
        max_tokens = max(hyperparams.max_tokens, 16)

        request_params: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }

        if hyperparams.verbosity is not None:
            request_params["text"] = {"verbosity": hyperparams.verbosity}

        if hyperparams.reasoning_effort is not None:
            request_params["reasoning"] = {"effort": hyperparams.reasoning_effort}

        if hyperparams.temperature is not None:
            request_params["temperature"] = hyperparams.temperature

        if tools:
            request_params["tools"] = self._convert_tools_for_responses(tools)

        # Forward extra_headers (e.g., User-Agent from incoming Claude Code request)
        extra_headers = hyperparams.extra.get("openai", {}).get("extra_headers")
        if extra_headers:
            request_params["extra_headers"] = extra_headers

        tools_log = f", tools={len(tools)}" if tools else ""
        logger.info(
            f"GPT-5 Responses API call: model={self._model}, "
            f"verbosity={hyperparams.verbosity}, reasoning={hyperparams.reasoning_effort}{tools_log}"
        )

        response = await client.responses.create(**request_params)

        return self._parse_responses_output(response, self._model)

    @retry(
        retry=retry_if_exception(lambda e: isinstance(e, Exception) and LiteLLMClient._is_retryable_error(e)),
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
        """Make the completion request with retry logic.

        Retry is applied here (not on complete()) so tenacity sees raw
        OpenAI exceptions (RateLimitError, APIStatusError) before they
        are wrapped into ProviderError/AuthenticationError.
        """
        if self._should_use_responses_api(tools, merged_params):
            return await self._complete_with_responses_api(client, messages, merged_params, tools=tools)

        kwargs = self._build_request_kwargs(messages, tools, merged_params)
        response = await client.chat.completions.create(**kwargs)
        return self._openai_to_completion(response)

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        hyperparams: ModelHyperparameters | None = None,
    ) -> CompletionResponse:
        """Non-streaming completion.

        For GPT-5 models, uses Responses API. Otherwise, uses Chat Completions API.

        Args:
            messages: List of messages in the conversation.
            tools: Optional list of tool definitions.
            hyperparams: Optional hyperparameters to override defaults.

        Returns:
            CompletionResponse with text, optional tool_calls, and usage.

        Raises:
            ProviderError: If the API call fails.
            AuthenticationError: If authentication fails.
        """
        merged_params = merge_hyperparams(self._default_hyperparams, hyperparams)
        client = await self._get_client()

        try:
            return await self._make_completion_request(client, messages, tools, merged_params)
        except (ProviderError, AuthenticationError):
            # Already wrapped, re-raise as-is
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

        Forces credential re-resolution on next request. Especially
        important when a custom httpx.AsyncClient with SSL context was
        created (remote LiteLLM with root CA).
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
        """Streaming completion.

        For GPT-5 models, falls back to non-streaming Responses API
        since it doesn't support streaming.

        Yields canonical StreamEvent objects. For tool calls, accumulate
        ToolCallDelta events until response_end.

        Args:
            messages: List of messages in the conversation.
            tools: Optional list of tool definitions.
            hyperparams: Optional hyperparameters to override defaults.

        Yields:
            StreamEvent objects (text_delta, tool_call_delta, response_end, usage, error).
        """
        merged_params = merge_hyperparams(self._default_hyperparams, hyperparams)
        client = await self._get_client()

        # GPT-5 models use Responses API (doesn't support streaming),
        # so we fall back to non-streaming and emit synthetic stream events
        if self._should_use_responses_api(tools, merged_params):
            try:
                logger.info(
                    f"GPT-5 Responses API (streaming fallback): model={self._model}, "
                    f"verbosity={merged_params.verbosity}"
                )
                response = await self._complete_with_responses_api(client, messages, merged_params, tools=tools)

                if response.text:
                    yield StreamEvent(type="text_delta", text=response.text)

                # Emit tool call deltas so callers can accumulate them
                if response.tool_calls:
                    for i, tc in enumerate(response.tool_calls):
                        yield StreamEvent(
                            type="tool_call_delta",
                            tool_call_delta=ToolCallDelta(
                                index=i,
                                id=tc.id,
                                name=tc.name,
                                arguments_json=json.dumps(tc.arguments),
                            ),
                        )

                if response.usage:
                    yield StreamEvent(type="usage", usage=response.usage)

                yield StreamEvent(
                    type="response_end",
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                )
                return

            except Exception as e:
                error_str = str(e).lower()
                if "authentication" in error_str or "unauthorized" in error_str:
                    await self._credentials.invalidate(self._provider)
                    await self._close_client()
                yield StreamEvent(type="error", error=str(e))
                return

        # Standard Chat Completions API streaming path
        accumulator = ToolCallAccumulator()
        usage_data: dict[str, int] | None = None

        try:
            kwargs = self._build_request_kwargs(messages, tools, merged_params)
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}

            stream = await client.chat.completions.create(**kwargs)

            async for chunk in stream:
                # Handle usage from final chunk
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
        """Estimate token count for messages and tools.

        Uses simple estimation (4 chars per token) since LiteLLM
        doesn't provide a tokenization endpoint.

        Args:
            messages: List of messages to count.
            tools: Optional list of tool definitions to include in count.

        Returns:
            Estimated token count.
        """
        openai_messages = [self._message_to_openai(m) for m in messages]
        total = estimate_message_tokens(openai_messages)

        if tools:
            tools_json = json.dumps(tools)
            total += len(tools_json) // 4

        return total
