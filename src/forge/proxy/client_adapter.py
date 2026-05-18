"""Adapter to use core.llm clients with proxy's converter pipeline.

This adapter bridges between:
- core.llm interface (StreamEvent, CompletionResponse)
- Proxy's expected interface (OpenAI dict format)

The adapter allows the proxy to use core.llm for LLM calls while keeping
the existing Anthropic ↔ OpenAI converters unchanged.

Supports LiteLLM providers via core.llm's get_client().
"""

import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from forge.core.llm import (
    CompletionResponse,
    Message,
    ModelHyperparameters,
    get_client,
)
from forge.core.llm.types import ToolCall
from forge.proxy.base_client import ProxyStreamError

logger = logging.getLogger(__name__)

AdapterProviderType = Literal["litellm_remote", "litellm_local"]


def _extract_cache_info(usage: dict[str, int] | None) -> dict[str, Any]:
    """Extract cache hit info from a usage dict.

    Core.llm clients include ``cached_tokens`` in the usage dict when the
    provider reports prompt caching metrics (via ``prompt_tokens_details``).

    Returns:
        Dict with ``cached_tokens`` and ``cache_hit_rate`` (percentage),
        or empty dict if no cache data is present.
    """
    if not usage:
        return {}
    cached_tokens = usage.get("cached_tokens", 0)
    if not cached_tokens:
        return {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    cache_hit_rate = (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0
    return {"cached_tokens": cached_tokens, "cache_hit_rate": cache_hit_rate}


def _sanitize_header_value(value: str, max_length: int = 256) -> str:
    """Sanitize a header value to prevent header injection and cap length.

    Strips all ASCII control characters (0x00-0x1F, 0x7F), not just CR/LF,
    to prevent log injection and downstream parsing issues.
    """
    sanitized = "".join(ch for ch in value if 0x20 <= ord(ch) < 0x7F or ord(ch) > 0x7F)
    return sanitized[:max_length]


class CoreLLMClientAdapter:
    """Adapts core.llm client interface to proxy's expected format.

    The proxy expects clients with:
    - create_completion(openai_request, request_id) -> dict
    - create_streaming_completion(openai_request, request_id) -> AsyncGenerator[dict, None]

    This adapter wraps core.llm clients to provide that interface.
    Supports LiteLLM providers (remote and local).
    """

    def __init__(
        self,
        model: str,
        provider: AdapterProviderType,
        max_tokens_override: Optional[int] = None,
        tier: str = "sonnet",
        default_hyperparams: ModelHyperparameters | None = None,
    ) -> None:
        """Initialize the adapter.

        Args:
            model: Model identifier (e.g., "openai/gpt-5.5").
            provider: Provider type (litellm_remote, litellm_local).
            max_tokens_override: Optional max_tokens cap.
            tier: Tier name for hyperparameter lookup.
            default_hyperparams: Default hyperparameters.
        """
        self.model_name = model
        self._provider = provider
        self.max_tokens_override = max_tokens_override
        self.tier = tier
        self.default_hyperparams = default_hyperparams

        # Model includes vendor prefix (e.g., "openai/gpt-5.5")
        self._client = get_client(
            model,
            provider=provider,  # type: ignore  # AdapterProviderType is subset of ProviderType
            default_hyperparams=default_hyperparams,
        )

        logger.info(f"CoreLLMClientAdapter initialized: model={model}, provider={provider}, tier={tier}")

    def _openai_messages_to_core(self, openai_messages: List[Dict[str, Any]]) -> List[Message]:
        """Convert OpenAI format messages to core.llm Messages.

        Args:
            openai_messages: Messages in OpenAI format.

        Returns:
            Messages in core.llm format.
        """

        def _tool_calls_to_core(tool_calls: object) -> list[ToolCall] | None:
            if not isinstance(tool_calls, list):
                return None

            out: list[ToolCall] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                if tc.get("type") != "function":
                    continue

                tc_id = tc.get("id")
                func = tc.get("function") or {}
                if not isinstance(func, dict):
                    continue

                name = func.get("name")
                args_raw = func.get("arguments")

                if not isinstance(tc_id, str) or not isinstance(name, str):
                    continue

                arguments: dict[str, Any]
                if isinstance(args_raw, str):
                    try:
                        parsed = json.loads(args_raw) if args_raw.strip() else {}
                        arguments = parsed if isinstance(parsed, dict) else {"_raw": parsed}
                    except json.JSONDecodeError:
                        arguments = {"raw_arguments": args_raw}
                elif isinstance(args_raw, dict):
                    arguments = args_raw
                else:
                    arguments = {}

                out.append(ToolCall(id=tc_id, name=name, arguments=arguments))

            return out or None

        messages = []
        for msg in openai_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content is None:
                # OpenAI allows content=null when tool_calls are present; core.llm does not.
                content = ""

            if role not in ("system", "user", "assistant", "tool"):
                role = "user"  # Fallback for unknown roles

            tool_call_id = msg.get("tool_call_id")
            tool_calls = _tool_calls_to_core(msg.get("tool_calls"))

            messages.append(
                Message(
                    role=role,
                    content=content,
                    tool_call_id=tool_call_id,
                    tool_calls=tool_calls,
                )
            )

        return messages

    def _core_response_to_openai(self, response: CompletionResponse, model: str) -> Dict[str, Any]:
        """Convert core.llm CompletionResponse to OpenAI format.

        Args:
            response: CompletionResponse from core.llm.
            model: Model identifier.

        Returns:
            Response in OpenAI format.
        """
        tool_calls = None
        if response.tool_calls:
            tool_calls = []
            for tc in response.tool_calls:
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                )

        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response.text,
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": response.usage.get("prompt_tokens", 0) if response.usage else 0,
                "completion_tokens": response.usage.get("completion_tokens", 0) if response.usage else 0,
                "total_tokens": response.usage.get("total_tokens", 0) if response.usage else 0,
                "cached_tokens": response.usage.get("cached_tokens", 0) if response.usage else 0,
            },
        }

    async def create_completion(self, openai_request: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        """Create a non-streaming completion.

        Args:
            openai_request: Request in OpenAI format.
            request_id: Request ID for logging.

        Returns:
            Response in OpenAI format.
        """
        logger.debug(f"[{request_id}] CoreLLMClientAdapter.create_completion: model={self.model_name}")

        messages = self._openai_messages_to_core(openai_request.get("messages", []))
        tools = openai_request.get("tools")

        # IMPORTANT: Only set fields that are explicitly provided by the request.
        # Otherwise, core.llm's merge_hyperparams() will treat unset defaults as overrides.
        hyperparams_data: dict[str, Any] = {}

        if "max_tokens" in openai_request and openai_request["max_tokens"] is not None:
            max_tokens = int(openai_request["max_tokens"])
            if self.max_tokens_override is not None:
                max_tokens = min(max_tokens, self.max_tokens_override)
            hyperparams_data["max_tokens"] = max_tokens

        for key in ("temperature", "top_p", "reasoning_effort", "verbosity"):
            if key in openai_request and openai_request[key] is not None:
                hyperparams_data[key] = openai_request[key]

        # Forward User-Agent to upstream if server injected it
        user_agent = openai_request.get("_user_agent")
        if isinstance(user_agent, str) and user_agent:
            openai_extra = hyperparams_data.setdefault("extra", {}).setdefault("openai", {})
            openai_extra["extra_headers"] = {"User-Agent": _sanitize_header_value(user_agent)}

        hyperparams = ModelHyperparameters(**hyperparams_data)

        response = await self._client.complete(messages, tools=tools, hyperparams=hyperparams)

        if response.usage:
            cache_info = _extract_cache_info(response.usage)
            cache_log = ""
            if cache_info:
                cache_log = (
                    f" | cached_tokens={cache_info['cached_tokens']}"
                    f" ({cache_info['cache_hit_rate']:.1f}% cache hit)"
                )
            logger.info(
                f"[{request_id}] <<< Response from {self.model_name} | "
                f"input_tokens={response.usage.get('prompt_tokens', 0)} | "
                f"output_tokens={response.usage.get('completion_tokens', 0)} | "
                f"total_tokens={response.usage.get('total_tokens', 0)}{cache_log}"
            )

        return self._core_response_to_openai(response, self.model_name)

    async def create_streaming_completion(
        self, openai_request: Dict[str, Any], request_id: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Create a streaming completion.

        Args:
            openai_request: Request in OpenAI format.
            request_id: Request ID for logging.

        Yields:
            Streaming chunks in OpenAI format.
        """
        logger.debug(f"[{request_id}] CoreLLMClientAdapter.create_streaming_completion: model={self.model_name}")

        messages = self._openai_messages_to_core(openai_request.get("messages", []))
        tools = openai_request.get("tools")

        # IMPORTANT: Only set fields that are explicitly provided by the request.
        # Otherwise, core.llm's merge_hyperparams() will treat unset defaults as overrides.
        hyperparams_data: dict[str, Any] = {}

        if "max_tokens" in openai_request and openai_request["max_tokens"] is not None:
            max_tokens = int(openai_request["max_tokens"])
            if self.max_tokens_override is not None:
                max_tokens = min(max_tokens, self.max_tokens_override)
            hyperparams_data["max_tokens"] = max_tokens

        for key in ("temperature", "top_p", "reasoning_effort", "verbosity"):
            if key in openai_request and openai_request[key] is not None:
                hyperparams_data[key] = openai_request[key]

        # Forward User-Agent to upstream if server injected it
        user_agent = openai_request.get("_user_agent")
        if isinstance(user_agent, str) and user_agent:
            openai_extra = hyperparams_data.setdefault("extra", {}).setdefault("openai", {})
            openai_extra["extra_headers"] = {"User-Agent": _sanitize_header_value(user_agent)}

        hyperparams = ModelHyperparameters(**hyperparams_data)

        # Track accumulated tool calls by OpenAI index (not id — id only in first chunk)
        accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
        response_id = f"chatcmpl-{int(time.time())}"
        final_usage: dict[str, int] = {}

        async for event in self._client.stream(messages, tools=tools, hyperparams=hyperparams):
            if event.type == "text_delta":
                yield {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": event.text,
                            },
                            "finish_reason": None,
                        }
                    ],
                }

            elif event.type == "tool_call_delta":
                delta = event.tool_call_delta
                if delta is None or delta.index is None:
                    # Core clients yield index=None for ambiguous fragments they can't
                    # route (e.g., late chunks in multi-tool streams). Mirrors
                    # ToolCallAccumulator.add_delta -- coercing to 0 corrupts tool 0.
                    continue
                tc_idx = delta.index

                if tc_idx not in accumulated_tool_calls:
                    accumulated_tool_calls[tc_idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                entry = accumulated_tool_calls[tc_idx]
                if delta.id:
                    entry["id"] = delta.id
                if delta.name:
                    entry["function"]["name"] = delta.name
                entry["function"]["arguments"] += delta.arguments_json

                yield {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": tc_idx,
                                        "id": delta.id,
                                        "type": "function" if delta.id else None,
                                        "function": {
                                            "name": delta.name,
                                            "arguments": delta.arguments_json,
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }

            elif event.type == "usage":
                if event.usage:
                    final_usage = event.usage
                yield {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_name,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": event.usage.get("prompt_tokens", 0) if event.usage else 0,
                        "completion_tokens": event.usage.get("completion_tokens", 0) if event.usage else 0,
                        "total_tokens": event.usage.get("total_tokens", 0) if event.usage else 0,
                        "cached_tokens": event.usage.get("cached_tokens", 0) if event.usage else 0,
                    },
                }

            elif event.type == "response_end":
                finish_reason = "tool_calls" if accumulated_tool_calls else "stop"
                yield {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason,
                        }
                    ],
                }

            elif event.type == "error":
                logger.error(f"[{request_id}] Stream error: {event.error}")
                # Detect error type from message for proper HTTP status mapping
                error_msg = event.error or "Unknown streaming error"
                error_lower = error_msg.lower()
                if "authentication" in error_lower or "unauthorized" in error_lower:
                    error_type = "authentication_error"
                elif "rate limit" in error_lower or "rate_limit" in error_lower:
                    error_type = "rate_limit_error"
                elif "invalid" in error_lower or "bad request" in error_lower:
                    error_type = "invalid_request_error"
                else:
                    error_type = "api_error"
                raise ProxyStreamError(error_msg, error_type=error_type)

        if final_usage:
            cache_info = _extract_cache_info(final_usage)
            cache_log = ""
            if cache_info:
                cache_log = (
                    f" | cached_tokens={cache_info['cached_tokens']}"
                    f" ({cache_info['cache_hit_rate']:.1f}% cache hit)"
                )
            logger.info(
                f"[{request_id}] <<< Stream complete from {self.model_name} | "
                f"input_tokens={final_usage.get('prompt_tokens', 0)} | "
                f"output_tokens={final_usage.get('completion_tokens', 0)} | "
                f"total_tokens={final_usage.get('total_tokens', 0)}{cache_log}"
            )

    async def count_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """Count tokens for messages and tools.

        Args:
            messages: Messages in OpenAI format.
            tools: Optional tools.

        Returns:
            Estimated token count.
        """
        core_messages = self._openai_messages_to_core(messages)
        return await self._client.count_tokens(core_messages, tools)

    async def aclose(self):
        """Clean up resources."""
        # core.llm clients don't have aclose yet, but we keep the interface
        pass
