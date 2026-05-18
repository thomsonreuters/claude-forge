"""Regression: proxy adapter must skip tool_call_delta with index=None.

Bug: the proxy adapter coerced `delta.index=None` to `0` when relaying
streaming tool-call deltas, causing ambiguous multi-tool fragments to
corrupt tool call 0's arguments.

The core LLM clients (LiteLLMClient, OpenRouterClient) deliberately yield
`index=None` when they cannot unambiguously route a fragment -- for example,
a late chunk in a multi-tool stream where two tool calls are already open
and the upstream chunk omits the index. The ToolCallAccumulator in
core/llm/clients/openai_compat.py correctly drops these as defense-in-depth
(`if idx is None: return`). The proxy adapter, however, coerced `None`
to `0`, appending the stray argument fragment to tool call 0's
arguments_json and producing malformed JSON downstream.

Root cause: parallel guard implementations across two layers with
inconsistent contracts -- the accumulator dropped None, the adapter coerced
None. Only the accumulator path had regression coverage; the proxy adapter
SSE path that production traffic flows through was untested for this case.

Affected file: src/forge/proxy/client_adapter.py (tool_call_delta handler).

Fix: skip the delta entirely when index is None, mirroring the
accumulator's behavior. Both layers now treat `index=None` as the
upstream's explicit "drop this fragment" signal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from forge.core.llm.types import StreamEvent, ToolCallDelta
from forge.proxy.client_adapter import CoreLLMClientAdapter

pytestmark = pytest.mark.regression


@pytest.fixture
def adapter():
    """Create a CoreLLMClientAdapter with a mocked core client."""
    adapter = CoreLLMClientAdapter.__new__(CoreLLMClientAdapter)
    adapter.model_name = "claude-sonnet-4"
    adapter._provider = "litellm_local"
    adapter._client = AsyncMock()
    return adapter


def _tool_call_event(*, index: int | None, tc_id: str | None, name: str | None, args: str) -> StreamEvent:
    return StreamEvent(
        type="tool_call_delta",
        tool_call_delta=ToolCallDelta(index=index, id=tc_id, name=name, arguments_json=args),
    )


@pytest.mark.asyncio
async def test_adapter_skips_index_none_in_multi_tool_stream(adapter) -> None:
    """An index=None straggler must NOT corrupt tool call 0's arguments.

    Reproduces the production failure: two tool calls in flight, then a late
    chunk arrives with index=None. Before the fix, the adapter coerced None to 0
    and appended the stray arguments to tool 0. After the fix, the delta is
    dropped (matching the core accumulator's behavior).
    """
    events = [
        _tool_call_event(index=0, tc_id="call_A", name="Read", args=""),
        _tool_call_event(index=0, tc_id=None, name=None, args='{"path": "/a"}'),
        _tool_call_event(index=1, tc_id="call_B", name="Write", args=""),
        _tool_call_event(index=1, tc_id=None, name=None, args='{"content": "b"}'),
        # Ambiguous straggler -- core client signals "can't route this"
        _tool_call_event(index=None, tc_id=None, name=None, args="CORRUPT"),
        StreamEvent(type="response_end"),
    ]

    async def mock_stream(*args, **kwargs):
        for e in events:
            yield e

    adapter._client.stream = mock_stream

    tool_call_chunks: list[dict] = []
    request = {"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "test"}]}
    async for chunk in adapter.create_streaming_completion(request, "req-1"):
        if "choices" not in chunk or not chunk["choices"]:
            continue
        delta = chunk["choices"][0].get("delta", {})
        if "tool_calls" in delta:
            tc = delta["tool_calls"][0]
            tool_call_chunks.append({"index": tc["index"], "args": tc["function"]["arguments"]})

    # Exactly four real fragments emitted -- the index=None straggler must be dropped.
    assert len(tool_call_chunks) == 4, f"Expected 4 tool-call chunks (None dropped), got {tool_call_chunks}"

    # Crucially, no chunk should carry "CORRUPT" -- proves the stray delta
    # was not silently reattached to tool 0 (or any other index).
    for chunk in tool_call_chunks:
        assert "CORRUPT" not in chunk["args"], f"Stray index=None delta corrupted args: {chunk}"


@pytest.mark.asyncio
async def test_adapter_skips_index_none_with_no_pending_calls(adapter) -> None:
    """An index=None delta arriving with no active tool calls must be dropped.

    Before the fix, the adapter would create a phantom tool call at index 0.
    """
    events = [
        _tool_call_event(index=None, tc_id="phantom", name="Read", args='{"x": 1}'),
        StreamEvent(type="response_end"),
    ]

    async def mock_stream(*args, **kwargs):
        for e in events:
            yield e

    adapter._client.stream = mock_stream

    chunks: list[dict] = []
    request = {"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "test"}]}
    async for chunk in adapter.create_streaming_completion(request, "req-2"):
        if "choices" in chunk and chunk["choices"]:
            delta = chunk["choices"][0].get("delta", {})
            if "tool_calls" in delta:
                chunks.append(delta["tool_calls"][0])

    assert chunks == [], f"index=None must not produce phantom tool calls; got {chunks}"
