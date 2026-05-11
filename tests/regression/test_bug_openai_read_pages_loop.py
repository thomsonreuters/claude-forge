"""Regression: OpenAI-family models must not send invalid Read parameters to Claude Code."""

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest

from forge.proxy.converters import (
    convert_openai_to_anthropic,
    convert_openai_to_anthropic_sse,
    sanitize_tool_input,
)
from forge.proxy.data_models import MessagesRequest

pytestmark = pytest.mark.regression


def test_sanitize_read_strips_empty_optional_params_for_non_pdf() -> None:
    cleaned = sanitize_tool_input(
        "Read",
        {
            "file_path": "/workspace/README.md",
            "pages": "",
            "offset": 0,
            "limit": None,
        },
    )

    assert cleaned == {"file_path": "/workspace/README.md"}


def test_sanitize_read_strips_non_empty_pages_for_non_pdf() -> None:
    cleaned = sanitize_tool_input(
        "Read",
        {
            "file_path": "/workspace/README.md",
            "pages": "1",
            "offset": 100,
            "limit": 20,
        },
    )

    assert cleaned == {
        "file_path": "/workspace/README.md",
        "offset": 100,
        "limit": 20,
    }


def test_sanitize_read_preserves_pages_for_pdf() -> None:
    cleaned = sanitize_tool_input(
        "Read",
        {
            "file_path": "/workspace/spec.pdf",
            "pages": "1-5",
        },
    )

    assert cleaned == {
        "file_path": "/workspace/spec.pdf",
        "pages": "1-5",
    }


def test_sanitize_unknown_tool_returns_original_input() -> None:
    tool_input = {"timeout": 0}

    assert sanitize_tool_input("Bash", tool_input) == tool_input


def test_non_streaming_openai_tool_call_sanitizes_read_pages() -> None:
    response = {
        "id": "chatcmpl-read",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps(
                                    {
                                        "file_path": "/workspace/README.md",
                                        "pages": "1",
                                        "offset": 0,
                                    }
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }

    with patch("forge.proxy.converters.asyncio.create_task", side_effect=lambda coro: coro.close()):
        result = convert_openai_to_anthropic(response, "claude-sonnet-4-6")

    tool_blocks = [block for block in result.content if block.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].input == {"file_path": "/workspace/README.md"}


def test_non_streaming_sanitized_read_logs_stripped_params_event() -> None:
    captured_events: list[dict] = []

    async def capture_tool_event(**kwargs) -> None:
        captured_events.append(kwargs)

    def run_now(coro):
        try:
            coro.send(None)
        except StopIteration:
            return None
        return None

    response = {
        "id": "chatcmpl-read",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps(
                                    {
                                        "file_path": "/workspace/README.md",
                                        "pages": "1",
                                        "offset": 0,
                                    }
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }

    with (
        patch("forge.proxy.converters.log_tool_event", new=capture_tool_event),
        patch("forge.proxy.converters.asyncio.create_task", side_effect=run_now),
    ):
        convert_openai_to_anthropic(response, "claude-sonnet-4-6")

    sanitized_events = [
        event for event in captured_events if event["details"].get("event") == "tool_args_sanitized"
    ]
    assert len(sanitized_events) == 1
    assert sanitized_events[0]["tool_name"] == "Read"
    assert sanitized_events[0]["status"] == "success"
    assert sanitized_events[0]["stage"] == "client_response"
    assert sanitized_events[0]["details"] == {
        "event": "tool_args_sanitized",
        "streaming": False,
        "stripped_params": ["pages", "offset"],
        "tool_id": "call_read",
    }


@pytest.mark.asyncio
async def test_streaming_openai_tool_call_sanitizes_read_pages() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_read",
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
                                "function": {
                                    "arguments": json.dumps(
                                        {
                                            "file_path": "/workspace/README.md",
                                            "pages": "",
                                            "limit": 0,
                                        }
                                    )
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    events = await _collect_sse_events(chunks)
    json_deltas = [
        event["data"]["delta"]["partial_json"]
        for event in events
        if event["event"] == "content_block_delta" and event["data"]["delta"]["type"] == "input_json_delta"
    ]

    assert len(json_deltas) == 1
    assert json.loads(json_deltas[0]) == {"file_path": "/workspace/README.md"}


@pytest.mark.asyncio
async def test_streaming_sanitized_read_logs_stripped_params_event() -> None:
    captured_events: list[dict] = []

    async def capture_tool_event(**kwargs) -> None:
        captured_events.append(kwargs)

    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_read",
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
                                "function": {
                                    "arguments": json.dumps(
                                        {
                                            "file_path": "/workspace/README.md",
                                            "pages": "",
                                            "limit": 0,
                                        }
                                    )
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    with patch("forge.proxy.converters.log_tool_event", new=capture_tool_event):
        await _collect_sse_events(chunks)
        await asyncio.sleep(0)

    sanitized_events = [
        event for event in captured_events if event["details"].get("event") == "tool_args_sanitized"
    ]
    assert len(sanitized_events) == 1
    assert sanitized_events[0]["tool_name"] == "Read"
    assert sanitized_events[0]["status"] == "success"
    assert sanitized_events[0]["stage"] == "client_response"
    assert sanitized_events[0]["details"] == {
        "event": "tool_args_sanitized",
        "streaming": True,
        "stripped_params": ["pages", "limit"],
        "tool_id": "call_read",
        "block_index": 0,
    }


@pytest.mark.asyncio
async def test_streaming_unknown_tool_keeps_incremental_argument_deltas() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_write",
                                "function": {"name": "Write", "arguments": ""},
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
                                "function": {"arguments": '{"file_path":"/workspace/out.txt",'},
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
                                "function": {"arguments": '"content":"hello"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    events = await _collect_sse_events(chunks)
    json_deltas = [
        event["data"]["delta"]["partial_json"]
        for event in events
        if event["event"] == "content_block_delta" and event["data"]["delta"]["type"] == "input_json_delta"
    ]

    assert json_deltas == [
        '{"file_path":"/workspace/out.txt",',
        '"content":"hello"}',
    ]


async def _collect_sse_events(chunks: list[dict]) -> list[dict]:
    async def gen() -> AsyncGenerator[dict, None]:
        for chunk in chunks:
            yield chunk

    request = MessagesRequest(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "read README"}],
        max_tokens=100,
        stream=True,
    )
    events = []
    async for sse_text in convert_openai_to_anthropic_sse(gen(), request, "test-request"):
        for event_text in sse_text.strip().split("\n\n"):
            if not event_text:
                continue
            lines = event_text.splitlines()
            event_name = lines[0].removeprefix("event: ")
            data_line = next(line for line in lines if line.startswith("data: "))
            events.append(
                {
                    "event": event_name,
                    "data": json.loads(data_line.removeprefix("data: ")),
                }
            )
    return events
