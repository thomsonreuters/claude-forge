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
        """Tool use works through OpenRouter (OpenAI-compatible tools)."""
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
        response = await client.complete(
            [Message(role="user", content="What's the weather in Paris? Use the tool.")],
            tools=tools,
            hyperparams=ModelHyperparameters(max_tokens=200),
        )
        assert response.tool_calls is not None
        assert len(response.tool_calls) >= 1
        assert response.tool_calls[0].name == "get_weather"
        assert "paris" in response.tool_calls[0].arguments.get("city", "").lower()


@pytest.mark.asyncio
class TestOpenRouterStream:
    """Real streaming tests against OpenRouter API."""

    async def test_stream_yields_text(self):
        """Streaming returns text_delta events."""
        _require_openrouter_key()
        client = get_client("anthropic/claude-haiku-4.5", provider="openrouter")
        chunks = []

        async for event in client.stream(
            [Message(role="user", content="Count from 1 to 3, one per line")],
            hyperparams=ModelHyperparameters(max_tokens=50),
        ):
            if event.type == "text_delta":
                chunks.append(event.text or "")
            elif event.type == "response_end":
                pass

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
