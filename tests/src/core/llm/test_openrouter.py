"""Tests for OpenRouter client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.core.llm.clients.openrouter import _UNSUPPORTED_PARAMS, OpenRouterClient
from forge.core.llm.types import CompletionResponse, Message, ModelHyperparameters


@pytest.fixture
def client():
    """Create an OpenRouter client with mocked credentials."""
    return OpenRouterClient(
        model="anthropic/claude-sonnet-4.6",
        provider="openrouter",
        default_hyperparams=ModelHyperparameters(max_tokens=4096),
    )


class TestOpenRouterClientInit:
    """Tests for client construction."""

    def test_model_property(self, client):
        assert client.model == "anthropic/claude-sonnet-4.6"

    def test_strips_reasoning_effort(self):
        kwargs = {
            "model": "test",
            "messages": [],
            "max_tokens": 100,
            "reasoning_effort": "high",
            "verbosity": "medium",
            "temperature": 0.7,
        }
        result = OpenRouterClient._strip_unsupported_params(kwargs)
        assert "reasoning_effort" not in result
        assert "verbosity" not in result
        assert result["temperature"] == 0.7

    def test_unsupported_params_set(self):
        assert "reasoning_effort" in _UNSUPPORTED_PARAMS
        assert "verbosity" in _UNSUPPORTED_PARAMS


class TestOpenRouterClientComplete:
    """Tests for non-streaming completion."""

    @pytest.mark.asyncio
    async def test_calls_chat_completions(self, client):
        """Verify OpenRouter uses chat.completions, not responses API."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Hello", tool_calls=None))]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_response.usage.prompt_tokens_details = None
        mock_response.error = None
        mock_response.model_dump = MagicMock(return_value={})

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        mock_creds = {
            "api_key": "sk-or-test",
            "base_url": "https://openrouter.ai/api/v1",
            "extra_headers": {"X-OpenRouter-Title": "Claude Forge"},
        }
        with patch.object(client, "_credentials") as mock_cm:
            mock_cm.get_credentials = AsyncMock(return_value=mock_creds)
            client._client = mock_client

            result = await client.complete(
                messages=[Message(role="user", content="Hello")],
            )

        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "anthropic/claude-sonnet-4.6"
        assert "reasoning_effort" not in call_kwargs
        assert isinstance(result, CompletionResponse)
        assert result.text == "Hello"

    @pytest.mark.asyncio
    async def test_headers_set_on_client_creation(self, client):
        """Verify OpenRouter-specific headers are passed to AsyncOpenAI."""
        mock_creds = {
            "api_key": "sk-or-test",
            "base_url": "https://openrouter.ai/api/v1",
            "extra_headers": {
                "HTTP-Referer": "https://github.com/thomsonreuters/claude-forge",
                "X-OpenRouter-Title": "Claude Forge",
            },
        }
        with (
            patch.object(client, "_credentials") as mock_cm,
            patch("forge.core.llm.clients.openrouter.AsyncOpenAI") as mock_openai_cls,
        ):
            mock_cm.get_credentials = AsyncMock(return_value=mock_creds)
            client._client = None

            await client._get_client()

            mock_openai_cls.assert_called_once()
            call_kwargs = mock_openai_cls.call_args[1]
            assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"
            assert call_kwargs["api_key"] == "sk-or-test"
            assert "X-OpenRouter-Title" in call_kwargs["default_headers"]


class TestOpenRouterClientStream:
    """Tests for streaming completion."""

    @pytest.mark.asyncio
    async def test_stream_yields_events(self, client):
        """Verify streaming yields text_delta and response_end events."""
        mock_chunk1 = MagicMock()
        mock_chunk1.usage = None
        mock_chunk1.choices = [MagicMock(delta=MagicMock(content="Hi", tool_calls=None))]

        mock_chunk2 = MagicMock()
        mock_chunk2.usage = MagicMock(prompt_tokens=10, completion_tokens=2, total_tokens=12)
        mock_chunk2.usage.prompt_tokens_details = None
        mock_chunk2.choices = []

        async def mock_stream():
            yield mock_chunk1
            yield mock_chunk2

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

        mock_creds = {
            "api_key": "sk-or-test",
            "base_url": "https://openrouter.ai/api/v1",
            "extra_headers": {},
        }
        with patch.object(client, "_credentials") as mock_cm:
            mock_cm.get_credentials = AsyncMock(return_value=mock_creds)
            client._client = mock_client

            events = []
            async for event in client.stream(
                messages=[Message(role="user", content="Hello")],
            ):
                events.append(event)

        types = [e.type for e in events]
        assert "text_delta" in types
        assert "response_end" in types
