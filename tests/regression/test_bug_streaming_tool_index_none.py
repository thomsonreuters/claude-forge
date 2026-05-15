"""Regression: streaming tool-call deltas with index=None must not be silently dropped.

Bug: Some OpenRouter providers send tool-call streaming chunks without an index field
when there's only one tool call in the chunk. The ToolCallAccumulator rejected these
(index=None -> early return), silently dropping all argument chunks.

Root cause: Client streaming loops passed tc_delta.index through verbatim to the
accumulator. The accumulator's strict None rejection is correct as defense-in-depth,
but clients must normalize unambiguous cases (single tool call in chunk -> index 0).

Fix: Normalize index in the client streaming loops (openrouter.py, litellm.py) when
len(delta.tool_calls) == 1 and tc_delta.index is None. Multi-tool chunks with missing
indexes remain dropped (ambiguous).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.core.llm.clients.openai_compat import ToolCallAccumulator
from forge.core.llm.clients.openrouter import OpenRouterClient
from forge.core.llm.types import Message, ModelHyperparameters, ToolCallDelta

pytestmark = pytest.mark.regression


# ── Helpers for fake streaming chunks ────────────────────────────


def _make_tool_call_delta(*, index: int | None, tc_id: str | None = None, name: str | None = None, args: str = ""):
    """Build a mock ChoiceDeltaToolCall matching the OpenAI SDK shape."""
    tc = MagicMock()
    tc.index = index
    tc.id = tc_id
    if name or args:
        tc.function = MagicMock()
        tc.function.name = name
        tc.function.arguments = args
    else:
        tc.function = None
    return tc


def _make_chunk(*, tool_calls: list | None = None, content: str | None = None):
    """Build a mock streaming chunk matching the OpenAI SDK ChatCompletionChunk."""
    chunk = MagicMock()
    chunk.usage = None
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    chunk.choices = [MagicMock(delta=delta)]
    return chunk


def _make_final_chunk():
    """Usage-only terminal chunk with no choices."""
    chunk = MagicMock()
    chunk.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    chunk.usage.prompt_tokens_details = None
    chunk.choices = []
    return chunk


def _make_openrouter_client() -> OpenRouterClient:
    return OpenRouterClient(
        model="anthropic/claude-sonnet-4.6",
        provider="openrouter",
        default_hyperparams=ModelHyperparameters(max_tokens=4096),
    )


# ── Client-level tests: exercise the actual normalization code ───


@pytest.mark.asyncio
async def test_bug_openrouter_stream_normalizes_none_index() -> None:
    """OpenRouterClient.stream() normalizes index=None in single-tool chunks."""
    client = _make_openrouter_client()

    async def fake_stream():
        # Chunk 1: tool call id + name, index=None (single tool in chunk)
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, tc_id="call_1", name="Read", args="")])
        # Chunk 2: argument fragment, index=None
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args='{"file_path":')])
        # Chunk 3: argument fragment, index=None
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args=' "/tmp/test"}')])
        yield _make_final_chunk()

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
    client._client = mock_client

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    end_event = next(e for e in events if e.type == "response_end")
    assert end_event.tool_calls is not None, "Tool calls should not be dropped"
    assert len(end_event.tool_calls) == 1
    assert end_event.tool_calls[0].name == "Read"
    assert end_event.tool_calls[0].arguments == {"file_path": "/tmp/test"}


@pytest.mark.asyncio
async def test_bug_litellm_stream_normalizes_none_index() -> None:
    """LiteLLMClient.stream() normalizes index=None in single-tool chunks."""
    from forge.core.llm.clients.litellm import LiteLLMClient

    client = LiteLLMClient(
        model="openai/gpt-4o",
        provider="litellm_remote",
        default_hyperparams=ModelHyperparameters(max_tokens=4096),
    )

    async def fake_stream():
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, tc_id="call_1", name="Write", args="")])
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args='{"content":')])
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args=' "hello"}')])
        yield _make_final_chunk()

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
    client._client = mock_client

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    end_event = next(e for e in events if e.type == "response_end")
    assert end_event.tool_calls is not None, "Tool calls should not be dropped"
    assert len(end_event.tool_calls) == 1
    assert end_event.tool_calls[0].name == "Write"
    assert end_event.tool_calls[0].arguments == {"content": "hello"}


# ── Client-level: multi-pending drops unindexed deltas ───────────


@pytest.mark.asyncio
async def test_bug_openrouter_stream_drops_none_index_when_multi_pending() -> None:
    """With multiple tool calls pending, index=None deltas must be dropped.

    Prevents silent misrouting: if tool calls 0 and 1 are both open, an
    unindexed delta could belong to either -- appending to 0 would corrupt it.
    """
    client = _make_openrouter_client()

    async def fake_stream():
        # Open two tool calls with proper indexes
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=0, tc_id="call_1", name="Read", args="")])
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=0, args='{"file_path": "/a"}')])
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=1, tc_id="call_2", name="Write", args="")])
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=1, args='{"content": "b"}')])
        # Late chunk with index=None -- ambiguous, should be dropped
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args='CORRUPT')])
        yield _make_final_chunk()

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
    client._client = mock_client

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    end_event = next(e for e in events if e.type == "response_end")
    assert end_event.tool_calls is not None
    assert len(end_event.tool_calls) == 2
    assert end_event.tool_calls[0].arguments == {"file_path": "/a"}
    assert end_event.tool_calls[1].arguments == {"content": "b"}


@pytest.mark.asyncio
async def test_bug_openrouter_stream_routes_to_sole_pending_index() -> None:
    """With one tool call pending at index=1, index=None normalizes to 1, not 0."""
    client = _make_openrouter_client()

    async def fake_stream():
        # Open tool call at index 1 (not 0)
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=1, tc_id="call_1", name="Read", args="")])
        # Continue with index=None -- should route to 1, not 0
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args='{"file_path":')])
        yield _make_chunk(tool_calls=[_make_tool_call_delta(index=None, args=' "/tmp"}')])
        yield _make_final_chunk()

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
    client._client = mock_client

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    end_event = next(e for e in events if e.type == "response_end")
    assert end_event.tool_calls is not None
    assert len(end_event.tool_calls) == 1
    assert end_event.tool_calls[0].name == "Read"
    assert end_event.tool_calls[0].arguments == {"file_path": "/tmp"}


# ── Accumulator-level tests: verify defense-in-depth invariant ───


def test_bug_accumulator_still_rejects_none_index() -> None:
    """ToolCallAccumulator must still reject None index (defense-in-depth)."""
    accumulator = ToolCallAccumulator()

    delta = ToolCallDelta(index=None, id="call_123", name="Read", arguments_json='{"x":1}')
    accumulator.add_delta(delta)

    assert not accumulator.has_pending()


def test_bug_default_index_no_pending() -> None:
    """default_index() returns 0 when no calls are pending."""
    assert ToolCallAccumulator().default_index() == 0


def test_bug_default_index_one_pending() -> None:
    """default_index() returns the sole pending index."""
    acc = ToolCallAccumulator()
    acc.add_delta(ToolCallDelta(index=3, id="call_1", name="Read", arguments_json=""))
    assert acc.default_index() == 3


def test_bug_default_index_multi_pending() -> None:
    """default_index() returns None when multiple calls are pending."""
    acc = ToolCallAccumulator()
    acc.add_delta(ToolCallDelta(index=0, id="call_1", name="Read", arguments_json=""))
    acc.add_delta(ToolCallDelta(index=1, id="call_2", name="Write", arguments_json=""))
    assert acc.default_index() is None


def test_bug_normal_indexed_deltas_unchanged() -> None:
    """Standard indexed deltas must work as before (no regression)."""
    accumulator = ToolCallAccumulator()

    accumulator.add_delta(ToolCallDelta(index=0, id="call_1", name="Read", arguments_json=""))
    accumulator.add_delta(ToolCallDelta(index=0, arguments_json='{"path": "/tmp"}'))
    accumulator.add_delta(ToolCallDelta(index=1, id="call_2", name="Write", arguments_json=""))
    accumulator.add_delta(ToolCallDelta(index=1, arguments_json='{"content": "hello"}'))

    result = accumulator.finalize()
    assert len(result) == 2
    assert result[0].name == "Read"
    assert result[0].arguments == {"path": "/tmp"}
    assert result[1].name == "Write"
    assert result[1].arguments == {"content": "hello"}
