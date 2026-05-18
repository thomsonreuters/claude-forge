"""Tests for GPT-5 Responses API support in LiteLLM client."""

import pytest

from forge.core.llm.clients.litellm import LiteLLMClient
from forge.core.llm.clients.openai_compat import extract_cached_tokens
from forge.core.llm.types import Message, ModelHyperparameters, ToolCall


class TestGPT5ModelDetection:
    """Tests for _is_gpt5_model helper."""

    @pytest.fixture
    def make_client(self):
        """Factory to create LiteLLM client with given model."""

        def _make(model: str) -> LiteLLMClient:
            return LiteLLMClient(model=model, provider="litellm_remote")

        return _make

    def test_gpt5_models_detected(self, make_client):
        """GPT-5 family models are detected correctly."""
        gpt5_model_specs = [
            "openai/gpt-5",
            "openai/gpt-5.1",
            "openai/gpt-5.2",
            "openai/gpt-5-mini",
            "openai/gpt-5-codex",
            "openai/gpt-5.1-codex",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.4-nano",
            "openai/gpt-5.5-pro",
        ]
        for model in gpt5_model_specs:
            client = make_client(model)
            assert client._is_gpt5_model(), f"Expected {model} to be detected as GPT-5"

    def test_non_gpt5_models_not_detected(self, make_client):
        """Non-GPT-5 models are not detected as GPT-5."""
        non_gpt5_specs = [
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "vertex_ai/gemini-3.1-pro-preview",
            "anthropic/claude-sonnet-4",
        ]
        for model in non_gpt5_specs:
            client = make_client(model)
            assert not client._is_gpt5_model(), f"Expected {model} to NOT be detected as GPT-5"


class TestResponsesApiSelection:
    """Tests for _should_use_responses_api logic."""

    @pytest.fixture
    def gpt5_client(self):
        """GPT-5 client (catalog: use_responses_api=true) for routing tests."""
        return LiteLLMClient(model="openai/gpt-5.5", provider="litellm_remote")

    @pytest.fixture
    def non_gpt5_client(self):
        """Non-GPT-5 client for testing."""
        return LiteLLMClient(model="openai/gpt-4o", provider="litellm_remote")

    def test_gpt5_always_uses_responses_api(self, gpt5_client):
        """GPT-5 models always use Responses API."""
        params = ModelHyperparameters()
        assert gpt5_client._should_use_responses_api(None, params)

    def test_gpt5_with_verbosity_uses_responses_api(self, gpt5_client):
        """GPT-5 with verbosity set uses Responses API."""
        params = ModelHyperparameters(verbosity="medium")
        assert gpt5_client._should_use_responses_api(None, params)

    def test_gpt5_with_tools_uses_responses_api(self, gpt5_client):
        """GPT-5 with tools uses Responses API (not Chat Completions)."""
        params = ModelHyperparameters(verbosity="high")
        tools = [{"type": "function", "function": {"name": "test"}}]
        assert gpt5_client._should_use_responses_api(tools, params)

    def test_gpt5_with_tools_and_reasoning_uses_responses_api(self, gpt5_client):
        """GPT-5 with tools and reasoning_effort uses Responses API."""
        params = ModelHyperparameters(reasoning_effort="high")
        tools = [{"type": "function", "function": {"name": "test"}}]
        assert gpt5_client._should_use_responses_api(tools, params)

    def test_non_gpt5_never_uses_responses_api(self, non_gpt5_client):
        """Non-GPT-5 models never use Responses API."""
        params = ModelHyperparameters(verbosity="high")
        assert not non_gpt5_client._should_use_responses_api(None, params)


class TestConvertMessagesForResponses:
    """Tests for _convert_messages_for_responses."""

    def test_basic_messages(self):
        """System, user, and assistant messages convert to role items."""
        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello!"),
            Message(role="assistant", content="Hi there!"),
        ]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        assert result == [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]

    def test_assistant_with_tool_calls(self):
        """Assistant message with tool_calls converts to function_call items."""
        messages = [
            Message(
                role="assistant",
                content="Let me search.",
                tool_calls=[
                    ToolCall(id="call_1", name="search", arguments={"query": "test"}),
                ],
            ),
        ]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        assert len(result) == 2
        assert result[0] == {"role": "assistant", "content": "Let me search."}
        assert result[1] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"query": "test"}',
        }

    def test_assistant_with_tool_calls_no_content(self):
        """Assistant with tool_calls but no content omits empty assistant message."""
        messages = [
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="call_1", name="search", arguments={"query": "test"}),
                ],
            ),
        ]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        # Should only have the function_call item, no empty assistant message
        assert len(result) == 1
        assert result[0]["type"] == "function_call"

    def test_tool_result_message(self):
        """Tool role messages convert to function_call_output items."""
        messages = [
            Message(role="tool", content="Result: 42", tool_call_id="call_123"),
        ]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        assert result == [
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "Result: 42",
            }
        ]

    def test_multimodal_content(self):
        """Multimodal content converts to Responses API format."""
        messages = [
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "What's in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            ),
        ]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        assert len(result) == 1
        content = result[0]["content"]
        assert len(content) == 2
        assert content[0] == {"type": "input_text", "text": "What's in this image?"}
        assert content[1] == {"type": "input_image", "image_url": "data:image/png;base64,abc"}

    def test_empty_assistant_message(self):
        """Assistant with no content and no tool_calls emits empty content."""
        messages = [Message(role="assistant", content="")]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        assert result == [{"role": "assistant", "content": ""}]

    def test_full_tool_call_roundtrip(self):
        """Full conversation with tool calls converts correctly."""
        messages = [
            Message(role="system", content="You can use tools."),
            Message(role="user", content="Search for cats"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="call_1", name="search", arguments={"q": "cats"})],
            ),
            Message(role="tool", content="Found 3 results", tool_call_id="call_1"),
            Message(role="assistant", content="I found 3 results about cats."),
        ]
        result = LiteLLMClient._convert_messages_for_responses(messages)

        assert len(result) == 5
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert result[2]["type"] == "function_call"
        assert result[3]["type"] == "function_call_output"
        assert result[4]["role"] == "assistant"


class TestConvertToolsForResponses:
    """Tests for _convert_tools_for_responses."""

    def test_basic_tool_conversion(self):
        """Chat Completions tool format converts to Responses API format."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the web",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ]
        result = LiteLLMClient._convert_tools_for_responses(tools)

        assert len(result) == 1
        assert result[0] == {
            "type": "function",
            "name": "search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }

    def test_strict_mode_preserved(self):
        """strict field is preserved in conversion."""
        tools = [
            {
                "type": "function",
                "function": {"name": "test", "strict": True, "parameters": {}},
            }
        ]
        result = LiteLLMClient._convert_tools_for_responses(tools)

        assert result[0]["strict"] is True

    def test_non_function_tool_passthrough(self):
        """Non-function tools pass through unchanged."""
        tools = [{"type": "web_search"}]
        result = LiteLLMClient._convert_tools_for_responses(tools)

        assert result == [{"type": "web_search"}]


class TestParseResponsesOutput:
    """Tests for _parse_responses_output."""

    def test_text_only_response(self):
        """Parse response with text output only."""

        class FakePart:
            type = "output_text"
            text = "Hello world"

        class FakeMessage:
            type = "message"
            content = [FakePart()]

        class FakeUsage:
            input_tokens = 10
            output_tokens = 5

        class FakeResponse:
            id = "resp_123"
            output = [FakeMessage()]
            usage = FakeUsage()
            status = "completed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")

        assert result.text == "Hello world"
        assert result.tool_calls is None
        assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        assert result.raw["finish_reason"] == "stop"

    def test_tool_call_response(self):
        """Parse response with function_call output."""

        class FakeToolCall:
            type = "function_call"
            call_id = "call_1"
            name = "search"
            arguments = '{"query": "test"}'

        class FakeUsage:
            input_tokens = 10
            output_tokens = 5

        class FakeResponse:
            id = "resp_456"
            output = [FakeToolCall()]
            usage = FakeUsage()
            status = "completed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")

        assert result.text == ""
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_1"
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "test"}
        assert result.raw["finish_reason"] == "tool_calls"

    def test_incomplete_response(self):
        """Parse response with incomplete status (truncated)."""

        class FakePart:
            type = "output_text"
            text = "Partial..."

        class FakeMessage:
            type = "message"
            content = [FakePart()]

        class FakeResponse:
            id = "resp_789"
            output = [FakeMessage()]
            usage = None
            status = "incomplete"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")

        assert result.raw["finish_reason"] == "length"
        assert result.usage is None

    def test_null_safe_usage(self):
        """Parse response with None usage (no crash)."""

        class FakeResponse:
            id = "resp_null"
            output = []
            usage = None
            status = "completed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")
        assert result.usage is None

    def test_mixed_text_and_tool_calls(self):
        """Parse response with both text and tool calls."""

        class FakePart:
            type = "output_text"
            text = "I'll search for that."

        class FakeMessage:
            type = "message"
            content = [FakePart()]

        class FakeToolCall:
            type = "function_call"
            call_id = "call_2"
            name = "search"
            arguments = '{"q": "cats"}'

        class FakeUsage:
            input_tokens = 20
            output_tokens = 15

        class FakeResponse:
            id = "resp_mixed"
            output = [FakeMessage(), FakeToolCall()]
            usage = FakeUsage()
            status = "completed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")

        assert result.text == "I'll search for that."
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search"
        assert result.raw["finish_reason"] == "tool_calls"

    def test_malformed_tool_call_arguments(self):
        """Parse response with invalid JSON in tool call arguments."""

        class FakeToolCall:
            type = "function_call"
            call_id = "call_bad"
            name = "test"
            arguments = "not valid json"

        class FakeResponse:
            id = "resp_bad"
            output = [FakeToolCall()]
            usage = None
            status = "completed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments == {}

    def test_dict_arguments_passthrough(self):
        """Parse response where arguments is already a dict (not JSON string)."""

        class FakeToolCall:
            type = "function_call"
            call_id = "call_dict"
            name = "search"
            arguments = {"query": "test"}

        class FakeResponse:
            id = "resp_dict"
            output = [FakeToolCall()]
            usage = None
            status = "completed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments == {"query": "test"}

    def test_failed_response_status(self):
        """Parse response with failed status."""

        class FakeResponse:
            id = "resp_fail"
            output = []
            usage = None
            status = "failed"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")
        assert result.raw["finish_reason"] == "error"

    def test_cancelled_response_status(self):
        """Parse response with cancelled status."""

        class FakeResponse:
            id = "resp_cancel"
            output = []
            usage = None
            status = "cancelled"

        result = LiteLLMClient._parse_responses_output(FakeResponse(), "gpt-5.2")
        assert result.raw["finish_reason"] == "error"


class TestBuildRequestKwargsSafetyNet:
    """Tests for reasoning_effort stripping safety net in _build_request_kwargs."""

    def test_gpt5_strips_reasoning_effort_with_tools(self):
        """GPT-5 Chat Completions path strips reasoning_effort when tools present."""
        client = LiteLLMClient(model="openai/gpt-5.2", provider="litellm_remote")
        messages = [Message(role="user", content="test")]
        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        params = ModelHyperparameters(reasoning_effort="high")

        kwargs = client._build_request_kwargs(messages, tools, params)

        assert "reasoning_effort" not in kwargs
        assert "tools" in kwargs

    def test_non_gpt5_keeps_reasoning_effort_with_tools(self):
        """Non-GPT-5 models keep reasoning_effort with tools."""
        client = LiteLLMClient(model="openai/o3", provider="litellm_remote")
        messages = [Message(role="user", content="test")]
        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        params = ModelHyperparameters(reasoning_effort="high")

        kwargs = client._build_request_kwargs(messages, tools, params)

        assert kwargs["reasoning_effort"] == "high"

    def test_gpt5_keeps_reasoning_effort_without_tools(self):
        """GPT-5 without tools keeps reasoning_effort (Chat Completions fallback)."""
        client = LiteLLMClient(model="openai/gpt-5.2", provider="litellm_remote")
        messages = [Message(role="user", content="test")]
        params = ModelHyperparameters(reasoning_effort="high")

        kwargs = client._build_request_kwargs(messages, None, params)

        assert kwargs["reasoning_effort"] == "high"

    def test_gpt5_extras_cannot_reintroduce_reasoning_effort(self):
        """Extras merged before safety net can't reintroduce reasoning_effort."""
        client = LiteLLMClient(model="openai/gpt-5.2", provider="litellm_remote")
        messages = [Message(role="user", content="test")]
        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        params = ModelHyperparameters(
            reasoning_effort="high",
            extra={"openai": {"reasoning_effort": "medium"}},
        )

        kwargs = client._build_request_kwargs(messages, tools, params)

        assert "reasoning_effort" not in kwargs


class TestRetryExclusion:
    """Tests for _is_retryable_error excluding 400 errors."""

    @staticmethod
    def _make_status_error(status_code: int) -> Exception:
        """Create a fake APIStatusError with the given status code."""
        from openai import APIStatusError

        class FakeError(APIStatusError):
            def __init__(self, code: int):
                self.status_code = code

        return FakeError(status_code)

    def test_400_not_retryable(self):
        """400 BadRequestError should not be retried."""
        error = self._make_status_error(400)
        assert not LiteLLMClient._is_retryable_error(error)

    def test_401_not_retryable(self):
        """401 auth error should not be retried."""
        error = self._make_status_error(401)
        assert not LiteLLMClient._is_retryable_error(error)

    def test_403_not_retryable(self):
        """403 forbidden should not be retried."""
        error = self._make_status_error(403)
        assert not LiteLLMClient._is_retryable_error(error)

    def test_429_retryable(self):
        """429 rate limit should be retried."""
        error = self._make_status_error(429)
        assert LiteLLMClient._is_retryable_error(error)

    def test_500_retryable(self):
        """500 server error should be retried."""
        error = self._make_status_error(500)
        assert LiteLLMClient._is_retryable_error(error)


class TestExtractCachedTokens:
    """Tests for extract_cached_tokens helper."""

    def test_object_with_prompt_tokens_details(self) -> None:
        """Extract cached_tokens from SDK response usage object."""

        class FakeDetails:
            cached_tokens = 500

        class FakeUsage:
            prompt_tokens_details = FakeDetails()

        assert extract_cached_tokens(FakeUsage()) == 500

    def test_dict_with_prompt_tokens_details(self) -> None:
        """Extract cached_tokens from dict-style usage."""
        usage = {"prompt_tokens_details": {"cached_tokens": 300}}
        assert extract_cached_tokens(usage) == 300

    def test_no_prompt_tokens_details(self) -> None:
        """Return 0 when prompt_tokens_details is absent."""

        class FakeUsage:
            pass

        assert extract_cached_tokens(FakeUsage()) == 0

    def test_none_cached_tokens(self) -> None:
        """Return 0 when cached_tokens is None."""
        usage = {"prompt_tokens_details": {"cached_tokens": None}}
        assert extract_cached_tokens(usage) == 0

    def test_zero_cached_tokens(self) -> None:
        """Return 0 when cached_tokens is 0."""
        usage = {"prompt_tokens_details": {"cached_tokens": 0}}
        assert extract_cached_tokens(usage) == 0

    def test_empty_prompt_details(self) -> None:
        """Return 0 when prompt_tokens_details is empty dict."""
        usage: dict[str, object] = {"prompt_tokens_details": {}}
        assert extract_cached_tokens(usage) == 0
