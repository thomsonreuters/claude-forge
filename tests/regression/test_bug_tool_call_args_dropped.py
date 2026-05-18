"""Regression: streaming tool call arguments dropped when id absent.

Bug: The core.llm ToolCallAccumulator and client_adapter both gated tool call
processing on `delta.id` being truthy. In OpenAI streaming, `id` is only present
on the first chunk for each tool call — subsequent argument chunks use `index`
for correlation and have `id=None`. This caused all argument fragments after the
first chunk to be silently dropped, resulting in empty tool call arguments `{}`.

Root cause: Migration from raw-chunk proxy (which passes OpenAI dicts with `index`
intact) to core.llm abstraction (ToolCallDelta) stripped the `index` field and
used `id`-only gating.

Affected files:
- src/forge/core/llm/clients/litellm.py (ToolCallAccumulator.add_delta)
- src/forge/proxy/client_adapter.py (tool_call_delta handler)
"""

import pytest

from forge.core.llm.clients.openai_compat import ToolCallAccumulator
from forge.core.llm.types import ToolCallDelta

pytestmark = pytest.mark.regression


class TestToolCallAccumulatorIndexCorrelation:
    """ToolCallAccumulator must use index, not id, for chunk correlation."""

    def test_argument_only_chunks_not_dropped(self):
        """Argument chunks without id must still be accumulated."""
        acc = ToolCallAccumulator()

        # First chunk: id + name + index (first chunk in OpenAI streaming)
        acc.add_delta(ToolCallDelta(index=0, id="call_abc", name="Bash", arguments_json='{"co'))

        # Second chunk: argument-only (no id, no name — real OpenAI behavior)
        acc.add_delta(ToolCallDelta(index=0, arguments_json='mmand":'))

        # Third chunk: more arguments
        acc.add_delta(ToolCallDelta(index=0, arguments_json=' "ls -la"}'))

        results = acc.finalize()
        assert len(results) == 1
        assert results[0].id == "call_abc"
        assert results[0].name == "Bash"
        assert results[0].arguments == {"command": "ls -la"}

    def test_multiple_tool_calls_with_interleaved_args(self):
        """Multiple tool calls with interleaved argument chunks."""
        acc = ToolCallAccumulator()

        # Tool 0: start
        acc.add_delta(ToolCallDelta(index=0, id="call_1", name="Read", arguments_json='{"file'))
        # Tool 1: start
        acc.add_delta(ToolCallDelta(index=1, id="call_2", name="Grep", arguments_json='{"pat'))
        # Tool 0: args (no id)
        acc.add_delta(ToolCallDelta(index=0, arguments_json='_path": "/tmp/a"}'))
        # Tool 1: args (no id)
        acc.add_delta(ToolCallDelta(index=1, arguments_json='tern": "foo"}'))

        results = acc.finalize()
        assert len(results) == 2

        by_name = {r.name: r for r in results}
        assert by_name["Read"].arguments == {"file_path": "/tmp/a"}
        assert by_name["Grep"].arguments == {"pattern": "foo"}

    def test_no_index_delta_ignored(self):
        """Deltas without index are safely ignored."""
        acc = ToolCallAccumulator()
        acc.add_delta(ToolCallDelta(id="call_1", name="Bash", arguments_json="{}"))
        assert acc.finalize() == []


class TestToolCallDeltaIndex:
    """ToolCallDelta carries index for OpenAI streaming correlation."""

    def test_index_field_exists(self):
        delta = ToolCallDelta(index=0, id="call_1", name="Bash")
        assert delta.index == 0

    def test_index_defaults_to_none(self):
        delta = ToolCallDelta(id="call_1")
        assert delta.index is None
