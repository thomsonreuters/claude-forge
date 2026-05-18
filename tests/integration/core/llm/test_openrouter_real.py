"""Real integration tests that call OpenRouter directly.

These tests make actual API calls -- use cheap/fast models.

Run with: pytest tests/integration/core/llm/test_openrouter_real.py -v -m slow

Requirements:
  - OPENROUTER_API_KEY set in environment or .env
  - No LiteLLM needed (direct OpenRouter API calls)
"""

import os

import pytest

from forge.core.llm import Message, ModelHyperparameters, get_client

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _require_openrouter_key() -> None:
    """Fail if OPENROUTER_API_KEY is not available."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.fail(
            "OPENROUTER_API_KEY not set. Required for OpenRouter integration tests.\n"
            "Set it in .env or export it in your shell."
        )


@pytest.mark.asyncio
class TestOpenRouterComplete:
    """Real completion tests against OpenRouter API."""

    async def test_complete_simple(self):
        """Basic completion through OpenRouter."""
        _require_openrouter_key()
        client = get_client("anthropic/claude-haiku-4.5", provider="openrouter")
        response = await client.complete(
            [Message(role="user", content="Say 'hello' and nothing else")],
            hyperparams=ModelHyperparameters(max_tokens=50),
        )
        assert "hello" in response.text.lower()
        assert response.usage is not None
        assert response.usage["total_tokens"] > 0

    async def test_system_message_preserved(self):
        """System messages reach the model through OpenRouter."""
        _require_openrouter_key()
        client = get_client("anthropic/claude-haiku-4.5", provider="openrouter")
        response = await client.complete(
            [
                Message(
                    role="system",
                    content="You only respond with 'YES' or 'NO'. Nothing else.",
                ),
                Message(role="user", content="Is water wet?"),
            ],
            hyperparams=ModelHyperparameters(max_tokens=10),
        )
        assert response.text.strip().upper() in ("YES", "NO", "YES.", "NO.")

    async def test_tool_call_roundtrip(self):
        """Tool use works through OpenRouter, including the tool-result follow-up."""
        _require_openrouter_key()
        client = get_client("anthropic/claude-haiku-4.5", provider="openrouter")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]
        opening_messages = [
            Message(
                role="system",
                content=(
                    "When asked about weather, call get_weather first. After receiving the tool result, "
                    "answer with the city and condition exactly as supplied."
                ),
            ),
            Message(role="user", content="What's the weather in Paris? Use the tool."),
        ]

        first = await client.complete(
            opening_messages,
            tools=tools,
            hyperparams=ModelHyperparameters(max_tokens=200),
        )
        assert first.tool_calls is not None
        assert len(first.tool_calls) >= 1

        tool_call = first.tool_calls[0]
        assert tool_call.name == "get_weather"
        assert "paris" in tool_call.arguments.get("city", "").lower()

        follow_up_messages = opening_messages + [
            Message(role="assistant", content=first.text, tool_calls=first.tool_calls),
            Message(
                role="tool",
                tool_call_id=tool_call.id,
                content='{"city":"Paris","temperature_c":18,"condition":"sunny"}',
            ),
        ]
        second = await client.complete(
            follow_up_messages,
            hyperparams=ModelHyperparameters(max_tokens=100),
        )

        assert second.text
        lowered = second.text.lower()
        assert "paris" in lowered
        assert "sunny" in lowered or "18" in lowered


@pytest.mark.asyncio
class TestOpenRouterStream:
    """Real streaming tests against OpenRouter API."""

    async def test_stream_yields_text(self):
        """Streaming returns text_delta events."""
        _require_openrouter_key()
        client = get_client("anthropic/claude-haiku-4.5", provider="openrouter")
        chunks = []
        errors = []
        saw_response_end = False

        async for event in client.stream(
            [Message(role="user", content="Count from 1 to 3, one per line")],
            hyperparams=ModelHyperparameters(max_tokens=50),
        ):
            if event.type == "text_delta":
                chunks.append(event.text or "")
            elif event.type == "error":
                errors.append(event.error or "unknown stream error")
            elif event.type == "response_end":
                saw_response_end = True

        assert errors == []
        assert saw_response_end
        assert len(chunks) > 0
        full_text = "".join(chunks)
        assert "1" in full_text and "2" in full_text


@pytest.mark.asyncio
class TestOpenRouterNonAnthropicModel:
    """Test with a non-Anthropic model to verify open model space works."""

    async def test_openai_model_via_openrouter(self):
        """OpenAI model through OpenRouter."""
        _require_openrouter_key()
        client = get_client("openai/gpt-4o-mini", provider="openrouter")
        response = await client.complete(
            [Message(role="user", content="What is 2+2? Reply with just the number.")],
            hyperparams=ModelHyperparameters(max_tokens=10),
        )
        assert "4" in response.text
