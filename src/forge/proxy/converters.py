"""Format conversion utilities between Anthropic and OpenAI APIs.

This module handles the conversion between two API formats:
1. Anthropic Claude API format (client-facing)
2. OpenAI format (backend - used by LiteLLM)

Conversion Flow:
- Request: Anthropic → OpenAI
- Response: OpenAI → Anthropic

Key Components:
- Tool description enhancement with usage examples
- Streaming and non-streaming response handling
- Comprehensive tool event logging for diagnostics
"""

import asyncio
import json
import logging
import traceback
import uuid
from typing import Any, AsyncGenerator, Callable, Dict, List, Literal, Optional, Union

from forge.proxy.data_models import (
    ContentBlock,
    ContentBlockText,
    ContentBlockToolUse,
    MessagesRequest,
    MessagesResponse,
    Usage,
)
from forge.proxy.utils import (
    log_tool_event,
    smart_format_str,
)

logger = logging.getLogger(__name__)

# on_complete(usage, failed, error_type) -- called when SSE stream finishes
_OnCompleteCallback = Callable[[Dict[str, int], bool, Optional[str]], None]


def enhance_tool_description(tool_name: str, original_description: str, schema: Dict) -> str:
    """
    Enhance tool descriptions with concrete examples to help Gemini generate proper tool calls.

    This function adds detailed usage examples for tools that have shown high failure rates
    in client execution reports. Examples are formatted to match the schema structure and
    highlight required parameters.

    Args:
        tool_name: The name of the tool
        original_description: The original tool description
        schema: The cleaned schema for this tool

    Returns:
        Enhanced description with appropriate usage examples
    """
    enhanced_description = original_description

    # Library of tool examples for problematic tools
    if tool_name == "Batch":
        example = (
            "\n\nEXAMPLE USAGE (Always include the invocations array):\n"
            "{\n"
            '  "description": "Run multiple tools in parallel",\n'
            '  "invocations": [  // REQUIRED: Array of tool invocations to execute\n'
            "    {\n"
            '      "tool_name": "Read",  // Name of the tool to invoke\n'
            '      "input": {  // Parameters for the tool\n'
            '        "file_path": "/path/to/file.txt"\n'
            "      }\n"
            "    },\n"
            "    {\n"
            '      "tool_name": "Grep",\n'
            '      "input": {\n'
            '        "pattern": "search term",\n'
            '        "include": "*.py"\n'
            "      }\n"
            "    }\n"
            "  ]\n"
            "}"
        )
        enhanced_description += example
        logger.debug("Enhanced Batch tool description with usage example")

    elif tool_name == "Edit":
        example = (
            "\n\nEXAMPLE USAGE:\n"
            "{\n"
            '  "file_path": "/path/to/file.py",  // REQUIRED: Absolute path to the file\n'
            '  "old_string": "def old_function(x, y):\\n    return x + y",  // REQUIRED: Exact text to replace\n'
            '  "new_string": "def old_function(x, y):\\n    # Add comment\\n    return x + y",  // REQUIRED: New text\n'
            '  "expected_replacements": 1  // Optional: Number of replacements to perform\n'
            "}"
        )
        enhanced_description += example
        logger.debug("Enhanced Edit tool description with usage example")

    elif tool_name == "Read":
        example = (
            "\n\nEXAMPLE USAGE:\n"
            "{\n"
            '  "file_path": "/path/to/file.txt"  // REQUIRED: Absolute path to the file\n'
            "}"
        )
        enhanced_description += example
        logger.debug("Enhanced Read tool description with usage example")

    elif tool_name == "Write":
        example = (
            "\n\nEXAMPLE USAGE:\n"
            "{\n"
            '  "file_path": "/path/to/file.txt",  // REQUIRED: Absolute path to the file\n'
            '  "content": "Contents to write to the file"  // REQUIRED: Content to write\n'
            "}"
        )
        enhanced_description += example
        logger.debug("Enhanced Write tool description with usage example")

    elif tool_name == "Glob":
        example = (
            "\n\nEXAMPLE USAGE:\n"
            "{\n"
            '  "pattern": "**/*.py"  // REQUIRED: The glob pattern to match files against\n'
            "}"
        )
        enhanced_description += example
        logger.debug("Enhanced Glob tool description with usage example")

    elif tool_name == "Grep":
        example = (
            "\n\nEXAMPLE USAGE:\n"
            "{\n"
            '  "pattern": "function",  // REQUIRED: The regex pattern to search for\n'
            '  "include": "*.py"  // Optional: File pattern to include in search\n'
            "}"
        )
        enhanced_description += example
        logger.debug("Enhanced Grep tool description with usage example")

    elif tool_name == "MultiEdit":
        example = (
            "\n\n⚠︎ CRITICAL: This is a TOOL CALL, not Python code! DO NOT use print(), default_api, or any Python syntax!\n"
            "✔ CORRECT JSON FORMAT:\n"
            "{\n"
            '  "file_path": "/absolute/path/to/file.py",\n'
            '  "edits": [\n'
            "    {\n"
            '      "old_string": "exact text to find",\n'
            '      "new_string": "replacement text",\n'
            '      "replace_all": false\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "✘ NEVER DO THIS:\n"
            "- print(default_api.MultiEdit(...))\n"
            "- default_api.MultieditEdits(...)\n"
            "- MultiEdit(file_path=..., edits=[...])\n"
            "- Any Python function call syntax\n\n"
            "Remember: You are calling a TOOL via JSON, not writing Python code!"
        )
        enhanced_description += example
        logger.debug("Enhanced MultiEdit tool description with usage example")

    # Add more tool examples as needed based on failure patterns in logs

    return enhanced_description


def _should_ignore_tool(tool_name: str) -> bool:
    """Return True if tool_name matches any configured ignore glob pattern."""
    try:
        from fnmatch import fnmatch

        from forge.config import config

        patterns = config.proxy.tool_prefixes_to_ignore
    except Exception as e:
        logger.debug("Cannot load tool ignore config: %s", e)
        return False
    for pattern in patterns:
        if fnmatch(tool_name, pattern):
            return True
    return False


def _model_supports_cache_control(model_name: str) -> bool:
    """Check if model requires explicit cache_control in requests.

    Anthropic/Bedrock: requires cache_control on content blocks to enable caching.
    OpenAI/Deepseek: automatic caching (≥1024 tokens), no field needed.
    Gemini: separate Context Caching API (not supported here).

    For non-Anthropic models, cache_control is silently stripped to avoid 400 errors.
    """
    if not model_name:
        return False
    name = model_name.lower()
    return "anthropic/" in name or "claude" in name or "bedrock/anthropic" in name


def convert_anthropic_to_openai(request: MessagesRequest, provider: str = "gemini") -> Dict[str, Any]:
    """Convert Anthropic API request to intermediate OpenAI format.

    Transforms Anthropic's message-based format into an OpenAI format that's
    easier to process before final conversion to provider-specific format. Handles system messages,
    content blocks, tool calls/results, and various parameter conversions.

    Args:
        request: The validated Anthropic API request with messages and parameters
        provider: Target provider ("gemini", "openai", "litellm") - affects schema normalization

    Returns:
        Dict[str, Any]: Request in OpenAI-compatible format with mapped parameters
    """
    openai_messages = []

    # system_cache_control is preserved and forwarded for Anthropic models only
    system_text = None
    system_cache_control = None

    if request.system:
        if isinstance(request.system, str):
            system_text = request.system
        else:
            text_parts = []
            for block in request.system:
                if block.type == "text":
                    text_parts.append(block.text)
                    if block.cache_control and _model_supports_cache_control(request.model):
                        system_cache_control = {"type": block.cache_control.type}
            system_text = "\n".join(text_parts) if text_parts else None

        if system_text:
            if provider in ("openai", "litellm", "openrouter"):
                # Auto-inject cache_control if configured and no explicit cache_control
                if not system_cache_control and _model_supports_cache_control(request.model):
                    try:
                        from forge.config import config as forge_config

                        provider_cfg = forge_config.proxy.get_provider(forge_config.proxy.preferred_provider)
                        if provider_cfg.prompt_caching == "auto_inject":
                            estimated_tokens = len(system_text) // 4
                            if estimated_tokens >= provider_cfg.auto_cache_min_tokens:
                                system_cache_control = {"type": "ephemeral"}
                                logger.debug(
                                    f"Auto-injected cache_control for system prompt "
                                    f"(~{estimated_tokens} tokens >= {provider_cfg.auto_cache_min_tokens})"
                                )
                    except RuntimeError:
                        logger.debug("Config not loaded, skipping cache_control auto-injection")

                # Use content block array when cache_control present (Anthropic requirement)
                if system_cache_control:
                    system_content = [
                        {
                            "type": "text",
                            "text": system_text,
                            "cache_control": system_cache_control,
                        }
                    ]
                    openai_messages.append({"role": "system", "content": system_content})
                else:
                    openai_messages.append({"role": "system", "content": system_text})

                logger.debug(
                    f"System prompt added as message for {provider}"
                    + (" with cache_control" if system_cache_control else "")
                )
            else:
                # For Gemini: store separately
                logger.debug("System prompt extracted for Vertex SDK.")
        else:
            system_text = None  # Ensure it's None if empty

    for msg in request.messages:
        is_tool_response_message = False
        content_list = []
        tool_calls_list: list[Dict[str, Any]] = []

        if isinstance(msg.content, str):
            content_list.append({"type": "text", "text": msg.content})
        elif isinstance(msg.content, list):
            for block in msg.content:  # type: ignore[assignment]  # Pydantic ContentBlock union
                if block.type in ("thinking", "redacted_thinking"):
                    # Anthropic thinking blocks appear in --resume history;
                    # non-Anthropic providers don't support them — strip for conversion.
                    logger.debug("Stripping %s block (unsupported by target provider)", block.type)
                    continue
                if block.type == "text":
                    text_block: Dict[str, Any] = {"type": "text", "text": block.text}
                    if block.cache_control and _model_supports_cache_control(request.model):
                        text_block["cache_control"] = {"type": block.cache_control.type}
                    content_list.append(text_block)
                elif block.type == "image" and msg.role == "user":  # Images only supported for user role
                    content_list.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{block.source.media_type};base64,{block.source.data}"},
                        }
                    )
                    logger.debug("Image block added to intermediate format.")
                elif block.type == "tool_use" and msg.role == "assistant":
                    tool_calls_list.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.input),
                            },  # Arguments must be JSON string
                        }
                    )
                    logger.debug(f"Assistant tool_use '{block.name}' converted to intermediate tool_calls.")
                elif block.type == "tool_result" and msg.role == "user":
                    if content_list:
                        openai_messages.append({"role": "user", "content": content_list})
                        content_list = []

                    tool_content = block.content
                    # Ensure content is a string (JSON if possible) for OpenAI format
                    if not isinstance(tool_content, str):
                        try:
                            tool_content = json.dumps(tool_content)
                        except Exception:
                            tool_content = str(tool_content)  # Fallback to string representation

                    openai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "content": tool_content,
                        }
                    )
                    logger.debug(f"User tool_result for '{block.tool_use_id}' converted to intermediate tool message.")
                    is_tool_response_message = True
                    # Don't break - process all tool_result blocks in this message

        # Flush any remaining content after tool_result blocks
        if is_tool_response_message and content_list:
            openai_messages.append({"role": "user", "content": content_list})
            content_list = []

        if not is_tool_response_message:
            openai_message: Dict[str, Any] = {"role": msg.role}
            # Simplify content if only text AND no extra metadata (like cache_control)
            first_item = content_list[0] if len(content_list) == 1 else None
            if (
                isinstance(first_item, dict)
                and first_item.get("type") == "text"
                and set(first_item.keys()) == {"type", "text"}
            ):
                openai_message["content"] = first_item.get("text", "")
            elif content_list:  # Keep as list for multimodal or when metadata present
                openai_message["content"] = content_list
            else:
                openai_message["content"] = None  # Or empty string ""? Let's use None for clarity

            if tool_calls_list:
                openai_message["tool_calls"] = tool_calls_list

            if openai_message.get("content") is not None or openai_message.get("tool_calls"):
                openai_messages.append(openai_message)
            elif msg.role == "assistant" and not openai_message.get("content") and not openai_message.get("tool_calls"):
                # Handle case where assistant message might be empty (e.g., after tool call)
                # OpenAI format expects content: null or content: ""
                openai_message["content"] = ""
                openai_messages.append(openai_message)

    # --- Assemble OpenAI Request Dictionary ---
    # Note: request.model already contains the *mapped* Gemini ID from the validator
    openai_request = {
        "model": request.model,
        "messages": openai_messages,
        "max_tokens": request.max_tokens,
        "stream": request.stream or False,
    }
    if request.temperature is not None:
        openai_request["temperature"] = request.temperature
    if request.top_p is not None:
        openai_request["top_p"] = request.top_p
    if request.top_k is not None:
        openai_request["top_k"] = request.top_k
    if request.stop_sequences:
        openai_request["stop"] = request.stop_sequences
    if request.metadata:
        openai_request["metadata"] = request.metadata

    if system_text:
        openai_request["system_prompt"] = system_text

    if request.tools:
        openai_tools = []
        ignored_tool_names = []
        for tool in request.tools:
            if _should_ignore_tool(tool.name):
                ignored_tool_names.append(tool.name)
                continue

            input_schema = tool.input_schema.model_dump(exclude_unset=True)
            logger.debug(f"Cleaning schema for intermediate tool format: {tool.name}")
            logger.debug(f"Original schema for tool '{tool.name}': {smart_format_str(input_schema)}")

            tool_schema_details = {
                "tool_name": tool.name,
                "original_schema": input_schema,
            }

            # Pass through original schema (no normalization needed for OpenAI/LiteLLM)
            cleaned_schema = input_schema
            logger.debug(f"[{provider.upper()}] Using original schema for tool '{tool.name}'")
            asyncio.create_task(
                log_tool_event(
                    request_id="schema_" + str(uuid.uuid4())[:8],
                    tool_name=tool.name,
                    status="attempt",
                    stage="openai_request",
                    details=tool_schema_details,
                )
            )

            # Default to an empty object schema when unspecified.
            if "type" not in cleaned_schema:
                cleaned_schema["type"] = "object"
                logger.debug(f"Added missing 'type': 'object' to schema root for tool '{tool.name}'")
            if cleaned_schema.get("type") == "object" and "properties" not in cleaned_schema:
                cleaned_schema["properties"] = {}
                logger.debug(f"Added missing empty 'properties' object for tool '{tool.name}'")

            enhanced_description = enhance_tool_description(tool.name, tool.description or "", cleaned_schema)

            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": enhanced_description,
                        "parameters": cleaned_schema,
                    },
                }
            )

        if openai_tools:
            openai_request["tools"] = openai_tools
            logger.debug(f"Converted {len(openai_tools)} tools to intermediate OpenAI format.")

        if len(ignored_tool_names) > 0:
            logger.info(f"Skipping {len(ignored_tool_names)} tool(s) due to TOOL_PREFIXES_TO_IGNORE")
            ignored_names = ", ".join(ignored_tool_names)
            logger.debug(f"Skipped tool(s): {ignored_names}")

    # Note: Vertex has a different `tool_config`, this mapping might be approximate
    if request.tool_choice:
        choice_type = request.tool_choice.get("type")
        if choice_type == "any" or choice_type == "auto":
            openai_request["tool_choice"] = "auto"
        elif choice_type == "tool" and "name" in request.tool_choice:
            openai_request["tool_choice"] = {
                "type": "function",
                "function": {"name": request.tool_choice["name"]},
            }
        else:  # Includes 'none' or other types
            openai_request["tool_choice"] = "none"
        logger.debug(f"Converted tool_choice '{choice_type}' to intermediate format '{openai_request['tool_choice']}'.")

    logger.debug(f"Intermediate OpenAI Request Prepared: {smart_format_str(openai_request)}")
    return openai_request


def convert_openai_to_anthropic(
    response_chunk: Union[Dict, Any], original_model_name: Optional[str] = None
) -> Optional[MessagesResponse]:
    """Convert OpenAI-format response to Anthropic API response format.

    Transforms a completed (non-streaming) response from the intermediate OpenAI
    format back to the Anthropic API response format expected by Claude clients.
    Handles content blocks, tool calls, and finish reason mapping.

    Args:
        response_chunk: Response in OpenAI format from the intermediate conversion
        original_model_name: The original Claude model name requested by the client

    Returns:
        Optional[MessagesResponse]: Response in Anthropic format, or None if conversion fails
    """
    request_id = response_chunk.get("request_id", "unknown")  # Get request ID if passed through
    logger.info(f"[{request_id}] Converting adapted OpenAI response to Anthropic MessagesResponse format.")
    try:
        # Ensure input is a dictionary
        resp_dict = {}
        if isinstance(response_chunk, dict):
            resp_dict = response_chunk
        elif hasattr(response_chunk, "model_dump"):
            resp_dict = response_chunk.model_dump()
        else:
            try:
                resp_dict = vars(response_chunk)  # Fallback for simple objects
            except TypeError as e:
                logger.error(f"[{request_id}] Cannot convert response_chunk of type {type(response_chunk)} to dict.")
                raise ValueError(
                    "Input response_chunk is not convertible to dict.",
                ) from e

        resp_id = resp_dict.get("id") or f"msg_{uuid.uuid4().hex[:24]}"
        choices = resp_dict.get("choices", [])
        usage_data = resp_dict.get("usage", {}) or {}

        anthropic_content: List[ContentBlock] = []
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "content_filtered",
        }
        openai_finish_reason = "stop"  # Default

        if choices:
            choice = choices[0]  # Assume only one choice
            openai_finish_reason = choice.get("finish_reason", "stop")
            message = choice.get("message", {}) or {}

            text_content = message.get("content")
            tool_calls = message.get("tool_calls")

            if text_content and isinstance(text_content, str):
                anthropic_content.append(ContentBlockText(type="text", text=text_content))
                logger.debug(f"[{request_id}] Added text content block.")

            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and tc.get("type") == "function":
                        func = tc.get("function", {})
                        args_str = func.get("arguments", "{}")
                        tool_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}")
                        tool_name = func.get("name", "unknown_tool")

                        try:
                            args_input = json.loads(args_str)
                        except json.JSONDecodeError:
                            logger.warning(
                                f"[{request_id}] Non-streaming: Failed to parse tool arguments JSON: {args_str}. Sending raw string."
                            )
                            args_input = {"raw_arguments": args_str}
                        except Exception as e:
                            logger.error(
                                f"[{request_id}] Non-streaming: Error parsing tool arguments: {e}. Args: {args_str}"
                            )
                            args_input = {
                                "error_parsing_arguments": str(e),
                                "raw_arguments": args_str,
                            }

                        anthropic_content.append(
                            ContentBlockToolUse(
                                type="tool_use",
                                id=tool_id,
                                name=tool_name,
                                input=args_input,
                            )
                        )
                        logger.debug(f"[{request_id}] Added tool_use content block: id={tool_id}, name={tool_name}")

                        asyncio.create_task(
                            log_tool_event(
                                request_id=request_id,
                                tool_name=tool_name,
                                status="success",
                                stage="client_response",
                                details={"tool_id": tool_id, "streaming": False},
                            )
                        )
                    else:
                        logger.warning(
                            f"[{request_id}] Skipping conversion of non-function tool_call in response: {tc}"
                        )

        # Ensure there's always at least one content block (even if empty text)
        # Anthropic requires content to be a non-empty list.
        if not anthropic_content:
            logger.warning(f"[{request_id}] No content generated, adding empty text block.")
            anthropic_content.append(ContentBlockText(type="text", text=""))

        anthropic_stop_reason = stop_reason_map.get(openai_finish_reason, "end_turn")
        logger.debug(
            f"[{request_id}] Mapped finish_reason '{openai_finish_reason}' to stop_reason '{anthropic_stop_reason}'."
        )

        model_name = original_model_name if original_model_name else "claude-3.7-sonnet"

        return MessagesResponse(
            id=resp_id,
            model=model_name,
            type="message",
            role="assistant",
            content=anthropic_content,
            stop_reason=anthropic_stop_reason,  # type: ignore[arg-type]  # values from controlled stop_reason_map
            stop_sequence=None,  # not returned in OpenAI format
            usage=Usage(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
            ),
        )
    except Exception as e:
        logger.error(
            f"[{request_id}] Failed to convert adapted OpenAI response to Anthropic format: {e}",
            exc_info=True,
        )
        model_name = original_model_name if original_model_name else "claude-3.7-sonnet"

        return MessagesResponse(
            id=f"error_{uuid.uuid4().hex[:24]}",
            model=model_name,
            type="message",
            role="assistant",
            content=[ContentBlockText(type="text", text=f"Error processing model response: {str(e)}")],
            stop_reason="end_turn",  # Or maybe a custom error reason?
            usage=Usage(input_tokens=0, output_tokens=0),
        )


async def convert_openai_to_anthropic_sse(
    response_generator: AsyncGenerator[Dict[str, Any], None],
    request: MessagesRequest,
    request_id: str,
    on_complete: Optional["_OnCompleteCallback"] = None,
):
    """Convert OpenAI streaming format to Anthropic Server-Sent Events (SSE) format.

    Transforms a stream of OpenAI-format chunks into the Anthropic streaming format
    using Server-Sent Events. Handles the complex event structure required by Anthropic:
    - message_start/stop events
    - content_block_start/stop events
    - content_block_delta events
    - message_delta events with finish information
    - ping events for connection maintenance

    Args:
        response_generator: Async generator yielding OpenAI-format response chunks
        request: The original MessagesRequest from the client
        request_id: Unique identifier for logging and tracking this request

    Yields:
        SSE-formatted text chunks following the Anthropic streaming protocol
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    response_model_name = request.original_model_name or request.model  # fallback to mapped ID if original is missing
    logger.info(
        f"[{request_id}] Starting Anthropic SSE stream conversion (message {message_id}, model: {response_model_name})"
    )

    # --- Stream Initialization ---
    start_event_data = {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": response_model_name,
            "content": [],  # Content starts empty
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(start_event_data)}\n\n"
    logger.debug(f"[{request_id}] Sent message_start")

    yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
    logger.debug(f"[{request_id}] Sent initial ping")

    # --- Stream Processing ---
    content_block_index = -1
    current_block_type: Optional[Literal["text", "tool_use"]] = None
    text_started = False
    tool_calls_buffer: Dict[int, Dict[str, Any]] = (
        {}
    )  # {openai_tc_index: {id: str, name: str, args: str, block_idx: int}}
    final_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    _stream_failed = False
    _stream_error_type: Optional[str] = None
    final_stop_reason: Optional[str] = None

    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "content_filtered",
    }

    try:
        async for chunk in response_generator:
            logger.debug(f"[{request_id}] Processing adapted OpenAI Chunk: {chunk}")

            if not isinstance(chunk, dict):
                logger.warning(f"[{request_id}] Skipping invalid chunk format: {type(chunk)}")
                continue

            # Handle error chunks from stream generator.
            # stream_generator() catches ToolCallError/ProxyStreamError and yields
            # error dicts instead of raising — so no exception reaches the except
            # block below. We must set the failure flag here for metrics.
            if "error" in chunk:
                error_data = chunk["error"]
                _stream_failed = True
                _stream_error_type = error_data.get("type", "stream_error")
                error_event = {
                    "type": "error",
                    "error": {
                        "type": error_data.get("type", "api_error"),
                        "message": error_data.get("message", "Unknown streaming error"),
                    },
                }
                yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
                return  # End stream after error

            # --- Check for usage-only chunk (LiteLLM sends usage in chunk with empty choices) ---
            chunk_usage = chunk.get("usage")
            if chunk_usage and isinstance(chunk_usage, dict):
                prompt_tokens = chunk_usage.get("prompt_tokens", 0)
                completion_tokens = chunk_usage.get("completion_tokens", 0)

                if prompt_tokens > 0 and final_usage["input_tokens"] == 0:
                    # First time seeing input_tokens - send immediately
                    final_usage["input_tokens"] = prompt_tokens
                    usage_update_event = {
                        "type": "message_delta",
                        "delta": {},
                        "usage": {"input_tokens": prompt_tokens},
                    }
                    yield f"event: message_delta\ndata: {json.dumps(usage_update_event)}\n\n"
                    logger.debug(f"[{request_id}] Sent immediate message_delta with input_tokens={prompt_tokens}")

                if completion_tokens > 0:
                    final_usage["output_tokens"] = completion_tokens
                    logger.debug(f"[{request_id}] Updated output_tokens={completion_tokens}")

                # Accumulate cached_tokens (propagated from client_adapter since Step 2)
                cached_tokens = chunk_usage.get("cached_tokens", 0)
                if cached_tokens > 0:
                    final_usage["cached_tokens"] = cached_tokens

                logger.debug(f"[{request_id}] Updated usage from chunk: {final_usage}")

            choices = chunk.get("choices", [])
            if not choices or not isinstance(choices, list):
                # Skip chunk if no choices AND no usage (truly empty chunk)
                if not chunk_usage:
                    logger.warning(f"[{request_id}] Skipping chunk with missing or invalid 'choices': {chunk}")
                continue

            if len(choices) == 0:
                # Empty choices is OK if we just processed usage
                if chunk_usage:
                    logger.debug(f"[{request_id}] Processed usage-only chunk (empty choices)")
                    continue
                else:
                    logger.warning(f"[{request_id}] Skipping chunk with empty 'choices' list: {chunk}")
                    continue

            choice = choices[0]

            if not isinstance(choice, dict):
                logger.warning(
                    f"[{request_id}] Skipping chunk with invalid choice format (type={type(choice)}): {choice}"
                )
                continue

            delta = choice.get("delta", {}) or {}
            finish_reason = choice.get("finish_reason")

            # --- Process Delta Content ---
            text_delta = delta.get("content")
            tool_calls_delta = delta.get("tool_calls")

            if text_delta and isinstance(text_delta, str):
                # If currently in a tool_use block, stop it first
                if current_block_type == "tool_use":
                    if tool_calls_buffer:
                        last_tool_block_idx = tool_calls_buffer[max(tool_calls_buffer.keys())]["block_idx"]
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': last_tool_block_idx})}\n\n"
                        logger.debug(f"[{request_id}] Stopped tool block {last_tool_block_idx} due to incoming text.")
                    else:
                        logger.warning(
                            f"[{request_id}] current_block_type is 'tool_use' but tool_calls_buffer is empty"
                        )
                    current_block_type = None

                if not text_started:
                    content_block_index += 1
                    current_block_type = "text"
                    text_started = True
                    start_event = {
                        "type": "content_block_start",
                        "index": content_block_index,
                        "content_block": {
                            "type": "text",
                            "text": "",
                        },
                    }
                    yield f"event: content_block_start\ndata: {json.dumps(start_event)}\n\n"
                    logger.debug(f"[{request_id}] Started text block {content_block_index}")

                delta_event = {
                    "type": "content_block_delta",
                    "index": content_block_index,
                    "delta": {"type": "text_delta", "text": text_delta},
                }
                yield f"event: content_block_delta\ndata: {json.dumps(delta_event)}\n\n"
                logger.debug(f"[{request_id}] Sent text delta: '{text_delta[:50]}...'")

            if tool_calls_delta and isinstance(tool_calls_delta, list):
                logger.debug(f"[{request_id}] Received tool_calls_delta: {tool_calls_delta}")
                if current_block_type == "text" and text_started:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': content_block_index})}\n\n"
                    logger.debug(f"[{request_id}] Stopped text block {content_block_index} due to incoming tool call.")
                    current_block_type = None
                    text_started = False

                for tc_delta in tool_calls_delta:
                    if not isinstance(tc_delta, dict):
                        continue  # Skip invalid format

                    # OpenAI tool index (usually 0 for the first tool, 1 for second, etc.)
                    # We rely on this index to aggregate arguments for the *same* tool call.
                    tc_openai_index = tc_delta.get("index", 0)
                    tc_id = tc_delta.get("id")
                    func_delta = tc_delta.get("function", {}) or {}
                    func_name = func_delta.get("name")
                    args_delta = func_delta.get("arguments")

                    # --- Start a new tool_use block if necessary ---
                    if tc_openai_index not in tool_calls_buffer:
                        if tc_id and func_name:
                            content_block_index += 1
                            current_block_type = "tool_use"
                            tool_calls_buffer[tc_openai_index] = {
                                "id": tc_id,
                                "name": func_name,
                                "args": "",
                                "block_idx": content_block_index,
                            }
                            start_event = {
                                "type": "content_block_start",
                                "index": content_block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc_id,
                                    "name": func_name,
                                    "input": {},
                                },
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(start_event)}\n\n"
                            logger.debug(
                                f"[{request_id}] Started tool_use block {content_block_index} (id: {tc_id}, name: {func_name})"
                            )

                            # Log successful tool event for client in streaming
                            asyncio.create_task(
                                log_tool_event(
                                    request_id=request_id,
                                    tool_name=func_name,
                                    status="success",
                                    stage="client_response",
                                    details={
                                        "tool_id": tc_id,
                                        "streaming": True,
                                        "block_index": content_block_index,
                                    },
                                )
                            )
                        # ID can arrive before name in some providers; buffer until name arrives
                        elif tc_id and not func_name:
                            tool_calls_buffer[tc_openai_index] = {
                                "id": tc_id,
                                "name": None,
                                "args": "",
                                "block_idx": None,
                            }
                            logger.debug(
                                f"[{request_id}] Received tool ID {tc_id} first for index {tc_openai_index}, waiting for name."
                            )
                        else:
                            logger.warning(
                                f"[{request_id}] Cannot start tool block for index {tc_openai_index} without ID and/or Name. Delta: {tc_delta}"
                            )
                            continue  # Cannot start block yet

                    # --- If name arrives later for an existing ID ---
                    elif (
                        tc_openai_index in tool_calls_buffer
                        and func_name
                        and tool_calls_buffer[tc_openai_index]["name"] is None
                    ):
                        tool_info = tool_calls_buffer[tc_openai_index]
                        if tool_info["id"] == tc_id:  # Ensure ID matches if provided again
                            content_block_index += 1
                            current_block_type = "tool_use"
                            tool_info["name"] = func_name
                            tool_info["block_idx"] = content_block_index
                            start_event = {
                                "type": "content_block_start",
                                "index": content_block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tool_info["id"],
                                    "name": func_name,
                                    "input": {},
                                },
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(start_event)}\n\n"
                            logger.debug(
                                f"[{request_id}] Started tool_use block {content_block_index} for index {tc_openai_index} after receiving name ({func_name})"
                            )
                        else:
                            logger.warning(
                                f"[{request_id}] Received name '{func_name}' for index {tc_openai_index}, but ID mismatch (expected {tool_info['id']}, got {tc_id}). Skipping."
                            )

                    # --- Append argument fragments if block has started ---
                    if (
                        tc_openai_index in tool_calls_buffer
                        and args_delta
                        and tool_calls_buffer[tc_openai_index]["block_idx"] is not None
                    ):
                        tool_info = tool_calls_buffer[tc_openai_index]
                        tool_info["args"] += args_delta
                        delta_event = {
                            "type": "content_block_delta",
                            "index": tool_info["block_idx"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": args_delta,
                            },
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(delta_event)}\n\n"
                        logger.debug(
                            f"[{request_id}] Sent tool args delta for block {tool_info['block_idx']}: '{args_delta[:50]}...'"
                        )

            # --- Process Finish Reason ---
            if finish_reason:
                final_stop_reason = stop_reason_map.get(finish_reason, "end_turn")
                logger.info(
                    f"[{request_id}] Received final finish_reason: '{finish_reason}' -> Mapped to stop_reason: '{final_stop_reason}'"
                )
                break

        # --- End of Stream ---
        if current_block_type == "text" and text_started:
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': content_block_index})}\n\n"
            logger.debug(f"[{request_id}] Stopped final text block {content_block_index}")
        elif current_block_type == "tool_use":
            if tool_calls_buffer:
                last_tool_block_idx = tool_calls_buffer[max(tool_calls_buffer.keys())]["block_idx"]
                if last_tool_block_idx is not None:
                    stop_event_data = {
                        "type": "content_block_stop",
                        "index": last_tool_block_idx,
                    }
                    logger.debug(
                        f"[{request_id}] Yielding content_block_stop for tool_use: {json.dumps(stop_event_data)}"
                    )
                    yield f"event: content_block_stop\ndata: {json.dumps(stop_event_data)}\n\n"
                    logger.debug(f"[{request_id}] Stopped final tool_use block {last_tool_block_idx}")
            else:
                logger.warning(
                    f"[{request_id}] Current block type is tool_use, but buffer is empty. Cannot stop block."
                )

        if final_stop_reason is None:
            logger.warning(
                f"[{request_id}] Stream finished without receiving a finish_reason. Defaulting to 'end_turn'."
            )
            final_stop_reason = "end_turn"

        final_delta_event = {
            "type": "message_delta",
            "delta": {
                "stop_reason": final_stop_reason,
                "stop_sequence": None,  # not returned in OpenAI stream format
            },
        }

        # Only include usage if we have valid data (not zeros)
        # Sending zeros overwrites any previously displayed usage in Claude Code UI
        input_tokens = final_usage.get("input_tokens", 0)
        output_tokens = final_usage.get("output_tokens", 0)
        if input_tokens > 0 or output_tokens > 0:
            final_delta_event["usage"] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

        logger.debug(f"[{request_id}] Yielding final message_delta: {json.dumps(final_delta_event)}")
        yield f"event: message_delta\ndata: {json.dumps(final_delta_event)}\n\n"
        logger.debug(
            f"[{request_id}] Sent final message_delta (stop_reason: {final_stop_reason}, "
            f"usage: {final_delta_event.get('usage', 'not included')})"
        )

        stop_event_data = {"type": "message_stop"}
        logger.debug(f"[{request_id}] Yielding message_stop: {json.dumps(stop_event_data)}")
        yield f"event: message_stop\ndata: {json.dumps(stop_event_data)}\n\n"
        logger.debug(f"[{request_id}] Sent message_stop")

    except Exception as e:
        _stream_failed = True
        _stream_error_type = "internal_error"
        logger.error(
            f"[{request_id}] Error during Anthropic SSE stream conversion: {e}, "
            f"Full traceback:\n{traceback.format_exc()}"
        )
        try:
            error_payload = {
                "type": "error",
                "error": {
                    "type": "internal_server_error",
                    "message": f"Stream processing error: {str(e)}",
                },
            }
            yield f"event: error\ndata: {json.dumps(error_payload)}\n\n"
            # Always send message_stop after an error
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
            logger.debug(f"[{request_id}] Sent error event and message_stop after exception.")
        except Exception as e2:
            logger.error(f"[{request_id}] Failed to send error event to client: {e2}")
    finally:
        logger.info(f"[{request_id}] Anthropic SSE stream conversion finished.")
        if on_complete is not None:
            try:
                on_complete(final_usage, _stream_failed, _stream_error_type)
            except Exception:
                logger.debug(f"[{request_id}] on_complete callback failed", exc_info=True)
