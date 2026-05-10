"""Utility functions for logging and formatting.

Provides proxy request formatting,
and specialized tool usage event logging to JSON Lines file.

Structured JSONL logs are only written when the effective Forge log level is
"debug" (config.yaml log_level=debug or FORGE_DEBUG=1).
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Literal

from rich.pretty import pretty_repr

from forge.core.logging import get_effective_log_level
from forge.core.paths import get_forge_home

_logger = logging.getLogger(__name__)


def _should_write_structured_logs() -> bool:
    return get_effective_log_level() == "debug"


def _pid_suffix() -> str:
    return str(os.getpid())


class Colors:
    """ANSI color and formatting codes for terminal output styling."""

    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"


def log_request_beautifully(
    method: str,
    path: str,
    original_model: str,
    mapped_model: str,
    num_messages: int,
    num_tools: int,
    status_code: int,
) -> None:
    """Log API requests in a colorized, human-readable format.

    Creates a visually distinctive terminal output for request monitoring with color-coded
    status indicators, model mapping information, and request details.

    Args:
        method: HTTP method (GET, POST, etc.)
        path: Request endpoint path
        original_model: Source model requested (Claude model name)
        mapped_model: Target model used (Gemini model name)
        num_messages: Number of messages in the request
        num_tools: Number of tools in the request
        status_code: HTTP status code of the response
    """
    try:
        original_display = f"{Colors.CYAN}{original_model}{Colors.RESET}"
        endpoint = path.split("?")[0]
        mapped_display_name = mapped_model
        mapped_color = Colors.GREEN  # Green indicates target Gemini model
        mapped_display = f"{mapped_color}{mapped_display_name}{Colors.RESET}"

        tools_str = (
            f"{Colors.MAGENTA}{num_tools} tools{Colors.RESET}"
            if num_tools > 0
            else f"{Colors.DIM}{num_tools} tools{Colors.RESET}"
        )
        messages_str = f"{Colors.BLUE}{num_messages} messages{Colors.RESET}"

        status_color = Colors.GREEN if 200 <= status_code < 300 else Colors.RED
        status_symbol = "✓" if 200 <= status_code < 300 else "✗"
        status_str = f"{status_color}{status_symbol} {status_code}{Colors.RESET}"

        log_line = f"{Colors.BOLD}{method} {endpoint}{Colors.RESET} {status_str}"
        model_line = (
            f"  {original_display} → {mapped_display} ({messages_str}, {tools_str})"
        )

        # Never write ANSI-colored output to file logs.
        # Only emit these lines to an interactive terminal.
        if sys.stderr.isatty():
            print(log_line, file=sys.stderr)
            print(model_line, file=sys.stderr)

        _logger.info(
            "Request processed: %s %s - %s (model=%s->%s, msgs=%s, tools=%s)",
            method,
            endpoint,
            status_code,
            original_model,
            mapped_model,
            num_messages,
            num_tools,
        )
    except Exception as e:
        _logger.error("Error during request summary logging: %s", e)
        _logger.info(
            "%s %s %s | %s -> %s | %s msgs, %s tools",
            method,
            path,
            status_code,
            original_model,
            mapped_model,
            num_messages,
            num_tools,
        )


def smart_format_str(
    obj: object, max_string: int = 500, max_length: int = 100, indent: int = 2
) -> str:
    """Format an object to a string with rich formatting."""
    return pretty_repr(
        obj, max_string=max_string, max_length=max_length, indent_size=indent
    )


def smart_format_proto_str(
    obj: object, max_string: int = 500, max_length: int = 100, indent: int = 2
) -> str:
    """Format a proto object to a string with rich formatting."""
    formatted_obj = proto_to_dict(obj)
    return smart_format_str(formatted_obj, max_string, max_length, indent)


def proto_to_dict(obj: object) -> dict[str, object] | list[dict[str, object]] | object:
    """Convert proto objects to dictionaries recursively.

    This is used for logging/pretty-printing only.
    """
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        result = obj.to_dict()
        return result if isinstance(result, dict) else {"value": result}

    if isinstance(obj, (list, tuple)):
        items = [proto_to_dict(item) for item in obj]
        # best-effort: only keep dicts for this branch
        dict_items = [item for item in items if isinstance(item, dict)]
        return dict_items

    if isinstance(obj, dict):
        return {str(k): proto_to_dict(v) for k, v in obj.items()}

    return obj


# Tool Events Logger for JSONL file
# Create an asyncio Lock to ensure thread-safe writing to the JSONL file
_tool_events_lock = asyncio.Lock()

# Request/Response Logger for JSONL file
_request_response_lock = asyncio.Lock()


async def log_tool_event(
    request_id: str,
    tool_name: str | None,
    status: Literal["attempt", "success", "failure"],
    stage: Literal[
        "openai_request",
        "gemini_request",
        "gemini_response",
        "client_response",
        "client_execution_report",
    ],
    details: dict[str, Any] | None = None,
) -> None:
    """Log tool usage events to a separate JSON Lines file for analysis.

    This function captures structured data about tool usage events at different
    stages of the request/response cycle, writing events to a timestamped tool_events.jsonl
    file in a thread-safe manner.

    Args:
        request_id: The unique identifier for the request
        tool_name: The name of the tool being used (or None for general events)
        status: Whether this is an attempt, success, or failure
        stage: Which part of the process (request to Gemini, response from Gemini, or response to client)
        details: Optional additional information about the event
    """
    if not _should_write_structured_logs():
        return

    try:
        logs_dir = get_forge_home() / "logs" / "tool_events"
        logs_dir.mkdir(exist_ok=True, parents=True)

        datestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        jsonl_path = logs_dir / f"{datestamp}_proxy.{_pid_suffix()}.jsonl"

        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "request_id": request_id,
            "tool_name": tool_name,
            "status": status,
            "stage": stage,
        }

        if details:
            event["details"] = details

        from forge.core.state import open_secure_append

        async with _tool_events_lock:
            with open_secure_append(jsonl_path) as f:
                f.write(json.dumps(event) + "\n")

        _logger.debug(
            "Tool event logged: %s %s for %s (request_id=%s)",
            status,
            stage,
            tool_name or "unknown",
            request_id,
        )
    except Exception as e:
        # Log error but don't fail the request
        _logger.error(
            "Failed to log tool event: %s (request_id=%s)", e, request_id, exc_info=True
        )


# Tool Failure Logger — opt-in via RuntimeConfig.log_tool_failures
_tool_failure_lock = asyncio.Lock()


def _should_log_tool_failures() -> bool:
    from forge.runtime_config import get_runtime_config

    return get_runtime_config().log_tool_failures


_TOOL_FAILURE_SCHEMA_VERSION = 1
_TOOL_INPUT_MAX_STR_LEN = 1024
_TOOL_INPUT_MAX_DEPTH = 8
_ERROR_MAX_LEN = 2000


def _truncate_for_log(
    value: str | dict | list | None, max_len: int
) -> str | dict | list | None:
    """Truncate a top-level string value (used for the error field)."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + f"... ({len(value)} chars)"
    return value


def _truncate_recursive(
    value: Any,
    max_str_len: int = _TOOL_INPUT_MAX_STR_LEN,
    max_depth: int = _TOOL_INPUT_MAX_DEPTH,
) -> Any:
    """Recursively cap large string values inside nested dicts/lists.

    Edit/Write tool inputs can carry tens of KB of file content. Without
    this, a single failure can produce a multi-MB JSONL line.
    """
    if max_depth <= 0:
        return "<truncated: max depth exceeded>"
    if isinstance(value, str):
        if len(value) > max_str_len:
            return value[:max_str_len] + f"... ({len(value)} chars)"
        return value
    if isinstance(value, dict):
        return {
            k: _truncate_recursive(v, max_str_len, max_depth - 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_truncate_recursive(v, max_str_len, max_depth - 1) for v in value]
    return value


def _truncate_error_for_log(error_content: str | dict | list | None) -> Any:
    """Bound tool error payloads, including Anthropic list/dict content blocks."""
    if isinstance(error_content, str):
        return _truncate_for_log(error_content, _ERROR_MAX_LEN)
    return _truncate_recursive(error_content, max_str_len=_ERROR_MAX_LEN)


async def log_tool_failure(
    *,
    request_id: str,
    mapped_model: str,
    tool_name: str | None,
    tool_use_id: str | None,
    tool_input: dict[str, Any] | None,
    error_content: str | dict | list | None,
) -> None:
    """Log tool failure to dedicated JSONL for addendum refinement.

    Opt-in via log_tool_failures (no debug mode required). Best-effort:
    write failures are logged but never break the LLM response.
    """
    if not _should_log_tool_failures():
        return

    try:
        from forge.core.state import open_secure_append

        logs_dir = get_forge_home() / "logs" / "tool_failures"
        logs_dir.mkdir(exist_ok=True, parents=True)

        datestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        jsonl_path = logs_dir / f"{datestamp}_failures.{_pid_suffix()}.jsonl"

        record: dict[str, Any] = {
            "schema_version": _TOOL_FAILURE_SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "model": mapped_model,
            "tool": tool_name,
            "tool_input": _truncate_recursive(tool_input),
            "error": _truncate_error_for_log(error_content),
        }

        async with _tool_failure_lock:
            with open_secure_append(jsonl_path) as f:
                f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        _logger.warning("Failed to write tool failure log: %s", e)


async def log_request_response(
    request_id: str,
    original_model: str,
    mapped_model: str,
    request_body: dict[str, object],
    response_body: dict[str, object] | None,
    status_code: int,
    duration_ms: float,
    error: str | None = None,
    num_messages: int | None = None,
    num_tools: int | None = None,
    tool_names: list[str] | None = None,
    has_system: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = False,
) -> None:
    """Log request/response pairs to JSONL file for analysis and replay.

    Logs at INFO level on failure (status >= 400) and DEBUG level always.
    This provides comprehensive visibility for debugging and creating integration tests.

    Args:
        request_id: Unique request identifier
        original_model: Original model name requested
        mapped_model: Actual model used after mapping
        request_body: Full request payload for replay
        response_body: Full response payload (None for streaming)
        status_code: HTTP status code
        duration_ms: Request duration in milliseconds
        error: Error message if request failed
        num_messages: Number of messages in request
        num_tools: Number of tools in request
        tool_names: List of tool names in request
        has_system: Whether request has system message
        temperature: Temperature parameter
        max_tokens: Max tokens parameter
        streaming: Whether request is streaming
    """
    if not _should_write_structured_logs():
        return

    try:
        logs_dir = get_forge_home() / "logs" / "requests"
        logs_dir.mkdir(exist_ok=True, parents=True)

        datestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        jsonl_path = logs_dir / f"{datestamp}_requests.{_pid_suffix()}.jsonl"

        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "request_id": request_id,
            "original_model": original_model,
            "mapped_model": mapped_model,
            "num_messages": num_messages,
            "num_tools": num_tools,
            "tool_names": tool_names,
            "has_system": has_system,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "streaming": streaming,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "error": error,
        }

        is_failure = status_code >= 400

        # Always include bodies in the JSONL file for replay capability
        event["request_body"] = request_body
        event["response_body"] = response_body

        from forge.core.state import open_secure_append

        async with _request_response_lock:
            with open_secure_append(jsonl_path) as f:
                f.write(json.dumps(event, default=str) + "\n")

        if is_failure:
            _logger.info(
                "[%s] Request/Response logged (FAILURE): status=%s, model=%s->%s, "
                "messages=%s, tools=%s, duration=%sms, error=%s",
                request_id,
                status_code,
                original_model,
                mapped_model,
                num_messages,
                num_tools,
                duration_ms,
                error,
            )
            _logger.info(
                "[%s] Failed request details: tools=%s, temp=%s, max_tokens=%s",
                request_id,
                tool_names,
                temperature,
                max_tokens,
            )
        else:
            _logger.debug(
                "[%s] Request/Response logged: status=%s, model=%s->%s, "
                "messages=%s, tools=%s, duration=%sms",
                request_id,
                status_code,
                original_model,
                mapped_model,
                num_messages,
                num_tools,
                duration_ms,
            )

    except Exception as e:
        _logger.error(
            "Failed to log request/response: %s (request_id=%s)",
            e,
            request_id,
            exc_info=True,
        )
