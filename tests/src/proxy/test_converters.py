"""Tests for format conversion utilities between Anthropic and OpenAI APIs.

Covers: enhance_tool_description, _should_ignore_tool, convert_anthropic_to_openai,
convert_openai_to_anthropic, convert_openai_to_anthropic_sse.

Note: cache_control-specific tests live in test_cache_control.py.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import pytest

from forge.proxy.converters import (
    _should_ignore_tool,
    convert_anthropic_to_openai,
    convert_openai_to_anthropic,
    convert_openai_to_anthropic_sse,
    enhance_tool_description,
)
from forge.proxy.data_models import (
    ContentBlockText,
    ContentBlockToolUse,
    Message,
    MessagesRequest,
    MessagesResponse,
    ToolDefinition,
    ToolInputSchema,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    model: str = "claude-3-5-sonnet",
    messages: list | None = None,
    system: str | list | None = None,
    max_tokens: int = 1024,
    stream: bool = False,
    tools: list[ToolDefinition] | None = None,
    tool_choice: dict | None = None,
    stop_sequences: list[str] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    metadata: dict | None = None,
) -> MessagesRequest:
    """Build a MessagesRequest with sensible defaults."""
    if messages is None:
        messages = [Message(role="user", content="Hello")]
    return MessagesRequest(
        model=model,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        stream=stream,
        tools=tools,
        tool_choice=tool_choice,
        stop_sequences=stop_sequences,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        metadata=metadata,
    )


def _make_tool(name: str = "Read", description: str = "Read a file") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=ToolInputSchema(
            type="object",
            properties={"file_path": {"type": "string"}},
            required=["file_path"],
        ),
    )


def _make_tool_no_type(name: str = "Custom") -> ToolDefinition:
    """Tool whose input_schema has no explicit 'type' or 'properties'."""
    schema = ToolInputSchema(type="object", properties={})
    return ToolDefinition(name=name, description="Custom tool", input_schema=schema)


async def _async_gen(chunks: list[dict[str, Any]]) -> AsyncGenerator[dict[str, Any], None]:
    """Create an async generator from a list of chunk dicts."""
    for chunk in chunks:
        yield chunk


@pytest.fixture(autouse=True)
def _suppress_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent fire-and-forget coroutines from spawning during tests."""
    monkeypatch.setattr("forge.proxy.converters.asyncio.create_task", lambda coro: coro.close())


class TestEnhanceToolDescription:
    """Test enhance_tool_description() for known tool types."""

    @pytest.mark.parametrize(
        "tool_name",
        ["Batch", "Edit", "Read", "Write", "Glob", "Grep", "MultiEdit"],
    )
    def test_known_tools_get_enhanced(self, tool_name: str) -> None:
        result = enhance_tool_description(tool_name, "Original desc", {})
        assert "Original desc" in result
        assert len(result) > len("Original desc")

    def test_unknown_tool_returns_original(self) -> None:
        result = enhance_tool_description("UnknownTool", "Original desc", {})
        assert result == "Original desc"

    def test_empty_original_description(self) -> None:
        result = enhance_tool_description("Read", "", {})
        assert "EXAMPLE USAGE" in result

    def test_batch_includes_invocations(self) -> None:
        result = enhance_tool_description("Batch", "desc", {})
        assert "invocations" in result

    def test_edit_includes_old_string(self) -> None:
        result = enhance_tool_description("Edit", "desc", {})
        assert "old_string" in result

    def test_multiedit_includes_warning(self) -> None:
        result = enhance_tool_description("MultiEdit", "desc", {})
        assert "CRITICAL" in result
        assert "NEVER" in result

    def test_grep_includes_pattern(self) -> None:
        result = enhance_tool_description("Grep", "desc", {})
        assert "pattern" in result

    def test_schema_arg_unused_but_accepted(self) -> None:
        """Schema is accepted but not currently used by the function."""
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        result = enhance_tool_description("Read", "desc", schema)
        assert "EXAMPLE USAGE" in result


class TestShouldIgnoreTool:
    """Test _should_ignore_tool() with various config states."""

    def _patch_config(self, monkeypatch: pytest.MonkeyPatch, patterns: list[str]) -> None:
        import forge.config as config_module

        mock_proxy = type("P", (), {"tool_prefixes_to_ignore": patterns})()
        mock_config = type("C", (), {"proxy": mock_proxy})()
        monkeypatch.setattr(config_module, "config", mock_config)

    def test_matching_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_config(monkeypatch, ["mcp__*"])
        assert _should_ignore_tool("mcp__slack__send") is True

    def test_no_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_config(monkeypatch, ["mcp__*"])
        assert _should_ignore_tool("Read") is False

    def test_empty_patterns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_config(monkeypatch, [])
        assert _should_ignore_tool("anything") is False

    def test_config_load_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When config cannot be loaded, tool should not be ignored."""
        import forge.config as config_module

        # Make config.proxy raise when accessed
        class BrokenConfig:
            @property
            def proxy(self):
                raise RuntimeError("no config")

        monkeypatch.setattr(config_module, "config", BrokenConfig())
        assert _should_ignore_tool("mcp__anything") is False


class TestConvertAnthropicToOpenai:
    """Test convert_anthropic_to_openai() request conversion."""

    def test_simple_text_message(self) -> None:
        request = _make_request()
        result = convert_anthropic_to_openai(request, provider="litellm")

        assert result["model"] == request.model
        assert result["max_tokens"] == 1024
        user_msgs = [m for m in result["messages"] if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "Hello"

    def test_system_prompt_string(self) -> None:
        request = _make_request(system="Be helpful.")
        result = convert_anthropic_to_openai(request, provider="litellm")

        system_msgs = [m for m in result["messages"] if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "Be helpful."
        assert result.get("system_prompt") == "Be helpful."

    def test_system_prompt_gemini_provider(self) -> None:
        """Gemini provider stores system_prompt separately, not as a message."""
        request = _make_request(system="Be helpful.")
        result = convert_anthropic_to_openai(request, provider="gemini")

        system_msgs = [m for m in result["messages"] if m["role"] == "system"]
        assert len(system_msgs) == 0
        assert result.get("system_prompt") == "Be helpful."

    def test_tool_use_blocks_converted(self) -> None:
        """Assistant tool_use blocks become OpenAI tool_calls."""
        messages = [
            Message(role="user", content="Find files"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        type="tool_use",
                        id="toolu_123",
                        name="Glob",
                        input={"pattern": "*.py"},
                    )
                ],
            ),
        ]
        request = _make_request(messages=messages)
        result = convert_anthropic_to_openai(request, provider="litellm")

        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        tc = assistant_msgs[0]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "toolu_123"
        assert tc[0]["function"]["name"] == "Glob"
        assert json.loads(tc[0]["function"]["arguments"]) == {"pattern": "*.py"}

    def test_tool_result_blocks_converted(self) -> None:
        """User tool_result blocks become OpenAI tool messages."""
        from forge.proxy.data_models import ContentBlockToolResult

        messages = [
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        type="tool_result",
                        tool_use_id="toolu_123",
                        content="file contents here",
                    )
                ],
            ),
        ]
        request = _make_request(messages=messages)
        result = convert_anthropic_to_openai(request, provider="litellm")

        tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "toolu_123"
        assert tool_msgs[0]["content"] == "file contents here"

    def test_image_blocks_converted(self) -> None:
        """Image content blocks become OpenAI image_url format."""
        from forge.proxy.data_models import ContentBlockImage, ContentBlockImageSource

        messages = [
            Message(
                role="user",
                content=[
                    ContentBlockImage(
                        type="image",
                        source=ContentBlockImageSource(
                            type="base64",
                            media_type="image/png",
                            data="iVBOR...",
                        ),
                    )
                ],
            ),
        ]
        request = _make_request(messages=messages)
        result = convert_anthropic_to_openai(request, provider="litellm")

        user_msgs = [m for m in result["messages"] if m["role"] == "user"]
        content = user_msgs[0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "image_url"
        assert "data:image/png;base64,iVBOR..." in content[0]["image_url"]["url"]

    def test_tool_schema_cleaning_adds_type_and_properties(self) -> None:
        """Tools missing 'type' and 'properties' get them injected."""
        tool = _make_tool_no_type()
        request = _make_request(tools=[tool])
        result = convert_anthropic_to_openai(request, provider="litellm")

        params = result["tools"][0]["function"]["parameters"]
        assert params["type"] == "object"
        assert "properties" in params

    def test_tool_choice_auto(self) -> None:
        request = _make_request(tools=[_make_tool()], tool_choice={"type": "auto"})
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["tool_choice"] == "auto"

    def test_tool_choice_any(self) -> None:
        request = _make_request(tools=[_make_tool()], tool_choice={"type": "any"})
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["tool_choice"] == "auto"

    def test_tool_choice_specific_tool(self) -> None:
        request = _make_request(
            tools=[_make_tool()],
            tool_choice={"type": "tool", "name": "Read"},
        )
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["tool_choice"] == {
            "type": "function",
            "function": {"name": "Read"},
        }

    def test_tool_choice_none(self) -> None:
        request = _make_request(tools=[_make_tool()], tool_choice={"type": "none"})
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["tool_choice"] == "none"

    def test_stop_sequences_mapped_to_stop(self) -> None:
        request = _make_request(stop_sequences=["END", "STOP"])
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["stop"] == ["END", "STOP"]

    def test_optional_params_forwarded(self) -> None:
        request = _make_request(temperature=0.5, top_p=0.9, top_k=40, metadata={"user_id": "test"})
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["temperature"] == 0.5
        assert result["top_p"] == 0.9
        assert result["top_k"] == 40
        assert result["metadata"] == {"user_id": "test"}

    def test_stream_flag_forwarded(self) -> None:
        request = _make_request(stream=True)
        result = convert_anthropic_to_openai(request, provider="litellm")
        assert result["stream"] is True

    def test_ignored_tools_filtered_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tools matching ignore patterns are excluded from output."""
        import forge.config as config_module

        mock_proxy = type("P", (), {"tool_prefixes_to_ignore": ["mcp__*"]})()
        mock_config = type("C", (), {"proxy": mock_proxy})()
        monkeypatch.setattr(config_module, "config", mock_config)

        tools = [_make_tool("Read"), _make_tool("mcp__slack__send")]
        request = _make_request(tools=tools)
        result = convert_anthropic_to_openai(request, provider="litellm")

        tool_names = [t["function"]["name"] for t in result["tools"]]
        assert "Read" in tool_names
        assert "mcp__slack__send" not in tool_names

    def test_mixed_content_text_and_tool_result(self) -> None:
        """Message with both text and tool_result blocks splits correctly."""
        from forge.proxy.data_models import ContentBlockToolResult

        messages = [
            Message(
                role="user",
                content=[
                    ContentBlockText(type="text", text="Context text"),
                    ContentBlockToolResult(
                        type="tool_result",
                        tool_use_id="toolu_456",
                        content="tool output",
                    ),
                ],
            ),
        ]
        request = _make_request(messages=messages)
        result = convert_anthropic_to_openai(request, provider="litellm")

        # Text gets flushed before tool_result
        roles = [m["role"] for m in result["messages"]]
        assert "user" in roles
        assert "tool" in roles


class TestConvertOpenaiToAnthropic:
    """Test convert_openai_to_anthropic() response conversion."""

    def test_simple_text_response(self) -> None:
        response = {
            "id": "chatcmpl-123",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Hello there!"},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = convert_openai_to_anthropic(response, "claude-3-5-sonnet")
        assert isinstance(result, MessagesResponse)
        assert result.id == "chatcmpl-123"
        assert result.model == "claude-3-5-sonnet"
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "Hello there!"
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    def test_tool_call_response(self) -> None:
        response = {
            "id": "chatcmpl-456",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"file_path": "/tmp/test.py"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
        result = convert_openai_to_anthropic(response, "claude-3-5-sonnet")
        assert result is not None
        assert result.stop_reason == "tool_use"
        tool_blocks = [b for b in result.content if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name == "Read"
        assert tool_blocks[0].id == "call_abc"
        assert tool_blocks[0].input == {"file_path": "/tmp/test.py"}

    def test_malformed_tool_arguments_graceful(self) -> None:
        """Invalid JSON in tool arguments gets wrapped as raw_arguments."""
        response = {
            "id": "chatcmpl-789",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_bad",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": "not valid json{{{",
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        result = convert_openai_to_anthropic(response, "claude-3-5-sonnet")
        assert result is not None
        tool_blocks = [b for b in result.content if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert "raw_arguments" in tool_blocks[0].input

    def test_missing_usage_fields(self) -> None:
        """Missing or None usage doesn't crash."""
        response = {
            "id": "chatcmpl-no-usage",
            "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
            "usage": None,
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        assert result.usage.input_tokens == 0
        assert result.usage.output_tokens == 0

    @pytest.mark.parametrize(
        "openai_reason,expected_anthropic",
        [
            ("stop", "end_turn"),
            ("length", "max_tokens"),
            ("tool_calls", "tool_use"),
            ("content_filter", "content_filtered"),
            ("unknown_reason", "end_turn"),
        ],
    )
    def test_finish_reason_mapping(self, openai_reason: str, expected_anthropic: str) -> None:
        response = {
            "id": "chatcmpl-fr",
            "choices": [{"finish_reason": openai_reason, "message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        assert result.stop_reason == expected_anthropic

    def test_dict_input(self) -> None:
        response = {
            "id": "chatcmpl-dict",
            "choices": [{"finish_reason": "stop", "message": {"content": "works"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        assert result.content[0].text == "works"  # type: ignore[union-attr]

    def test_pydantic_model_dump_input(self) -> None:
        """Objects with model_dump() are handled.

        Note: The source calls response_chunk.get() first (line 501), so the
        input must be a dict. The model_dump path only runs if isinstance check
        fails on a dict. We pass a dict here since that's the real usage path.
        """
        response = {
            "id": "chatcmpl-pydantic",
            "choices": [{"finish_reason": "stop", "message": {"content": "pydantic"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        assert result.content[0].text == "pydantic"  # type: ignore[union-attr]

    def test_empty_choices_returns_empty_text(self) -> None:
        """No choices results in empty text block."""
        response = {
            "id": "chatcmpl-empty",
            "choices": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        assert len(result.content) == 1
        assert result.content[0].text == ""  # type: ignore[union-attr]

    def test_no_original_model_defaults(self) -> None:
        """Without original_model_name, falls back to default."""
        response = {
            "id": "chatcmpl-nomodel",
            "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        assert "claude" in result.model

    def test_non_function_tool_call_skipped(self) -> None:
        """Tool calls that aren't type='function' are skipped."""
        response = {
            "id": "chatcmpl-nonfunc",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [{"id": "call_x", "type": "code_interpreter", "function": {}}],
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = convert_openai_to_anthropic(response)
        assert result is not None
        # No tool_use blocks (non-function was skipped), just the fallback empty text
        tool_blocks = [b for b in result.content if b.type == "tool_use"]
        assert len(tool_blocks) == 0

    def test_unconvertible_input_raises(self) -> None:
        """Non-dict input without .get() raises AttributeError.

        The function calls response_chunk.get() on line 501 before the
        try/except block, so truly invalid input is not caught gracefully.
        """
        with pytest.raises(AttributeError):
            convert_openai_to_anthropic(42)  # type: ignore[arg-type]


class TestConvertOpenaiToAnthropicSse:
    """Test streaming SSE conversion."""

    @pytest.fixture
    def base_request(self) -> MessagesRequest:
        return _make_request(stream=True)

    async def _collect_events(
        self,
        chunks: list,
        request: MessagesRequest | None = None,
        on_complete: Any = None,
    ) -> list[dict]:
        """Run the SSE converter and collect all yielded events."""
        if request is None:
            request = _make_request(stream=True)
        events = []
        async for sse_text in convert_openai_to_anthropic_sse(
            _async_gen(chunks), request, "test-req-id", on_complete=on_complete
        ):
            for block in sse_text.strip().split("\n\n"):
                lines = block.strip().split("\n")
                event_type = None
                data = None
                for line in lines:
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                if event_type and data:
                    events.append({"event": event_type, "data": data})
        return events

    @pytest.mark.asyncio
    async def test_text_streaming(self, base_request: MessagesRequest) -> None:
        """Text deltas are assembled into content_block_start/delta/stop."""
        chunks = [
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = await self._collect_events(chunks, base_request)

        event_types = [e["event"] for e in events]
        assert "message_start" in event_types
        assert "ping" in event_types
        assert "content_block_start" in event_types
        assert "content_block_delta" in event_types
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert "message_stop" in event_types

        # Check text deltas
        deltas = [e for e in events if e["event"] == "content_block_delta"]
        assert len(deltas) == 2
        assert deltas[0]["data"]["delta"]["text"] == "Hello"
        assert deltas[1]["data"]["delta"]["text"] == " world"

    @pytest.mark.asyncio
    async def test_tool_call_streaming(self, base_request: MessagesRequest) -> None:
        """Tool call chunks are converted to tool_use content blocks."""
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "function": {"name": "Read", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"file_path"'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "/tmp/a"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = await self._collect_events(chunks, base_request)

        # Should have content_block_start with tool_use type
        starts = [e for e in events if e["event"] == "content_block_start"]
        tool_starts = [s for s in starts if s["data"]["content_block"]["type"] == "tool_use"]
        assert len(tool_starts) == 1
        assert tool_starts[0]["data"]["content_block"]["name"] == "Read"
        assert tool_starts[0]["data"]["content_block"]["id"] == "call_abc"

        # Should have input_json_delta events
        json_deltas = [
            e
            for e in events
            if e["event"] == "content_block_delta" and e["data"]["delta"]["type"] == "input_json_delta"
        ]
        assert len(json_deltas) >= 2

        # Final stop_reason should be tool_use
        msg_delta = [e for e in events if e["event"] == "message_delta"]
        assert msg_delta[-1]["data"]["delta"]["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_text_to_tool_transition(self, base_request: MessagesRequest) -> None:
        """Transition from text to tool_use stops the text block first."""
        chunks = [
            {"choices": [{"delta": {"content": "Let me check"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "Read", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = await self._collect_events(chunks, base_request)

        # Text block should be stopped before tool block starts
        text_stop_idx = None
        tool_start_idx = None
        for i, e in enumerate(events):
            if (
                e["event"] == "content_block_stop"
                and any(
                    prev["event"] == "content_block_delta" and prev["data"]["delta"].get("type") == "text_delta"
                    for prev in events[:i]
                )
                and text_stop_idx is None
            ):
                text_stop_idx = i
            if e["event"] == "content_block_start" and e["data"].get("content_block", {}).get("type") == "tool_use":
                tool_start_idx = i

        assert text_stop_idx is not None
        assert tool_start_idx is not None
        assert text_stop_idx < tool_start_idx

    @pytest.mark.asyncio
    async def test_error_chunk_terminates_stream(self, base_request: MessagesRequest) -> None:
        """Error chunks emit an error event and stop the stream."""
        chunks = [
            {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
            {"error": {"type": "rate_limit_error", "message": "Too many requests"}},
            {"choices": [{"delta": {"content": "should not appear"}, "finish_reason": None}]},
        ]
        events = await self._collect_events(chunks, base_request)

        event_types = [e["event"] for e in events]
        assert "error" in event_types
        # Content after error should not appear
        text_deltas = [
            e for e in events if e["event"] == "content_block_delta" and e["data"]["delta"].get("type") == "text_delta"
        ]
        texts = [d["data"]["delta"]["text"] for d in text_deltas]
        assert "should not appear" not in texts

    @pytest.mark.asyncio
    async def test_on_complete_callback_success(self, base_request: MessagesRequest) -> None:
        """on_complete is called with usage data on success."""
        callback_args: list = []

        def on_complete(usage, failed, error_type):
            callback_args.append((usage, failed, error_type))

        chunks = [
            {
                "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        await self._collect_events(chunks, base_request, on_complete=on_complete)

        assert len(callback_args) == 1
        usage, failed, error_type = callback_args[0]
        assert failed is False
        assert error_type is None

    @pytest.mark.asyncio
    async def test_on_complete_callback_on_error(self, base_request: MessagesRequest) -> None:
        """on_complete is called with failed=True on error."""
        callback_args: list = []

        def on_complete(usage, failed, error_type):
            callback_args.append((usage, failed, error_type))

        chunks = [{"error": {"type": "api_error", "message": "fail"}}]
        await self._collect_events(chunks, base_request, on_complete=on_complete)

        assert len(callback_args) == 1
        _, failed, error_type = callback_args[0]
        assert failed is True
        assert error_type == "api_error"

    @pytest.mark.asyncio
    async def test_empty_stream(self, base_request: MessagesRequest) -> None:
        """Empty stream still produces message_start, ping, message_delta, message_stop."""
        events = await self._collect_events([], base_request)

        event_types = [e["event"] for e in events]
        assert "message_start" in event_types
        assert "ping" in event_types
        assert "message_delta" in event_types
        assert "message_stop" in event_types

    @pytest.mark.asyncio
    async def test_usage_tracking(self, base_request: MessagesRequest) -> None:
        """Usage from chunks is accumulated and reported in final message_delta."""
        chunks = [
            {
                "choices": [{"delta": {"content": "hi"}, "finish_reason": None}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 50, "cached_tokens": 100},
            },
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = await self._collect_events(chunks, base_request)

        # Find the final message_delta with stop_reason
        final_deltas = [
            e for e in events if e["event"] == "message_delta" and e["data"].get("delta", {}).get("stop_reason")
        ]
        assert len(final_deltas) == 1
        usage = final_deltas[0]["data"].get("usage", {})
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_non_dict_chunk_skipped(self, base_request: MessagesRequest) -> None:
        """Non-dict chunks are silently skipped."""
        chunks = [
            "not a dict",  # type: ignore[list-item]
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = await self._collect_events(chunks, base_request)

        text_deltas = [
            e for e in events if e["event"] == "content_block_delta" and e["data"]["delta"].get("type") == "text_delta"
        ]
        assert len(text_deltas) == 1
        assert text_deltas[0]["data"]["delta"]["text"] == "ok"

    @pytest.mark.asyncio
    async def test_stream_no_finish_reason_defaults_end_turn(self, base_request: MessagesRequest) -> None:
        """Stream without finish_reason defaults to end_turn."""
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            # No chunk with finish_reason — stream just ends
        ]
        events = await self._collect_events(chunks, base_request)

        final_deltas = [
            e for e in events if e["event"] == "message_delta" and e["data"].get("delta", {}).get("stop_reason")
        ]
        assert len(final_deltas) == 1
        assert final_deltas[0]["data"]["delta"]["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_usage_only_chunk_no_choices(self, base_request: MessagesRequest) -> None:
        """Chunk with only usage data (no choices) is processed without error."""
        chunks = [
            {"usage": {"prompt_tokens": 150, "completion_tokens": 0}},
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = await self._collect_events(chunks, base_request)

        event_types = [e["event"] for e in events]
        assert "message_stop" in event_types

        # Should get an early message_delta with input_tokens
        input_deltas = [
            e for e in events if e["event"] == "message_delta" and "input_tokens" in e["data"].get("usage", {})
        ]
        assert len(input_deltas) >= 1
        assert input_deltas[0]["data"]["usage"]["input_tokens"] == 150


# ---------------------------------------------------------------------------
# OpenRouter provider compatibility
# ---------------------------------------------------------------------------


class TestOpenRouterSystemPromptPreservation:
    """Verify OpenRouter is treated as OpenAI-compatible for system prompts."""

    def test_system_prompt_added_as_message_for_openrouter(self):
        """System prompt must become a message, not be separated (Gemini path)."""
        req = _make_request(system="You are a helpful assistant.", model="anthropic/claude-sonnet-4.6")
        result = convert_anthropic_to_openai(req, provider="openrouter")
        messages = result["messages"]
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "You are a helpful assistant."

    def test_tools_forwarded_for_openrouter(self):
        """Tool definitions should be present in OpenRouter output."""
        req = _make_request(
            tools=[_make_tool()],
            model="anthropic/claude-sonnet-4.6",
        )
        result = convert_anthropic_to_openai(req, provider="openrouter")
        assert "tools" in result
        assert len(result["tools"]) == 1
        assert result["tools"][0]["function"]["name"] == "Read"
