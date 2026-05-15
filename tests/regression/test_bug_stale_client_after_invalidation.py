"""Regression: LLM clients must reset cached HTTP client after credential invalidation.

Bug: After authentication failure, credentials.invalidate() was called but the
cached AsyncOpenAI client (self._client) retained the old credentials. Subsequent
requests reused the stale client, bypassing credential re-resolution.

Root cause: _get_client() short-circuits when self._client is not None, so
invalidating credentials without clearing the client has no effect.

Affected files: openrouter.py (2 paths), litellm.py (3 paths).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from forge.core.llm.clients.litellm import LiteLLMClient
from forge.core.llm.clients.openrouter import OpenRouterClient
from forge.core.llm.types import Message, ModelHyperparameters

pytestmark = pytest.mark.regression


def _make_openrouter_client() -> OpenRouterClient:
    return OpenRouterClient(
        model="anthropic/claude-sonnet-4.6",
        provider="openrouter",
        default_hyperparams=ModelHyperparameters(max_tokens=4096),
    )


def _make_litellm_client(model: str = "openai/gpt-4o") -> LiteLLMClient:
    return LiteLLMClient(
        model=model,
        provider="litellm_remote",
        default_hyperparams=ModelHyperparameters(max_tokens=4096),
    )


def _inject_mock_client(client: OpenRouterClient | LiteLLMClient) -> AsyncMock:
    """Inject a mock AsyncOpenAI client and credentials manager.

    Returns the mock_openai so tests can assert close() was called.
    """
    mock_openai = AsyncMock()
    mock_openai.close = AsyncMock()
    client._client = mock_openai

    mock_creds = AsyncMock()
    mock_creds.invalidate = AsyncMock()
    client._credentials = mock_creds

    return mock_openai


# ── OpenRouterClient: 2 invalidation paths ──────────────────────


@pytest.mark.asyncio
async def test_bug_openrouter_client_reset_on_auth_failure_complete() -> None:
    """OpenRouterClient.complete() auth path (openrouter.py line ~131)."""
    client = _make_openrouter_client()
    mock_openai = _inject_mock_client(client)
    mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("unauthorized"))

    with pytest.raises(Exception, match="unauthorized"):
        await client.complete([Message(role="user", content="test")])

    assert client._client is None
    client._credentials.invalidate.assert_awaited_once()  # type: ignore[attr-defined]  # AsyncMock
    mock_openai.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug_openrouter_client_reset_on_auth_failure_stream() -> None:
    """OpenRouterClient.stream() auth path (openrouter.py line ~218)."""
    client = _make_openrouter_client()
    mock_openai = _inject_mock_client(client)
    mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("authentication failed"))

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    assert client._client is None
    client._credentials.invalidate.assert_awaited_once()  # type: ignore[attr-defined]  # AsyncMock
    mock_openai.close.assert_awaited_once()
    assert any(e.type == "error" for e in events)


# ── LiteLLMClient: 3 invalidation paths ─────────────────────────


@pytest.mark.asyncio
async def test_bug_litellm_client_reset_on_auth_failure_complete() -> None:
    """LiteLLMClient.complete() auth path (litellm.py line ~465)."""
    client = _make_litellm_client()
    mock_openai = _inject_mock_client(client)
    mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("unauthorized"))

    with pytest.raises(Exception, match="unauthorized"):
        await client.complete([Message(role="user", content="test")])

    assert client._client is None
    client._credentials.invalidate.assert_awaited_once()  # type: ignore[attr-defined]  # AsyncMock
    mock_openai.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug_litellm_client_reset_on_auth_failure_stream_chat_completions() -> None:
    """LiteLLMClient.stream() Chat Completions path (litellm.py line ~611).

    Non-GPT-5 models use the Chat Completions streaming path.
    """
    client = _make_litellm_client("openai/gpt-4o")
    mock_openai = _inject_mock_client(client)
    mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("authentication error"))

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    assert client._client is None
    client._credentials.invalidate.assert_awaited_once()  # type: ignore[attr-defined]  # AsyncMock
    mock_openai.close.assert_awaited_once()
    assert any(e.type == "error" for e in events)


@pytest.mark.asyncio
async def test_bug_litellm_client_reset_on_auth_failure_stream_responses_api() -> None:
    """LiteLLMClient.stream() GPT-5 Responses API fallback path (litellm.py line ~547).

    GPT-5 models fall back to non-streaming Responses API in the stream() method.
    """
    client = _make_litellm_client("openai/gpt-5")
    mock_openai = _inject_mock_client(client)
    mock_openai.responses.create = AsyncMock(side_effect=Exception("unauthorized request"))

    events = []
    async for event in client.stream([Message(role="user", content="test")]):
        events.append(event)

    assert client._client is None
    client._credentials.invalidate.assert_awaited_once()  # type: ignore[attr-defined]  # AsyncMock
    mock_openai.close.assert_awaited_once()
    assert any(e.type == "error" for e in events)


# ── Negative case: non-auth errors should NOT reset ──────────────


@pytest.mark.asyncio
async def test_bug_openrouter_client_not_reset_on_non_auth_error() -> None:
    """Non-auth errors must not clear the client."""
    client = _make_openrouter_client()
    mock_openai = _inject_mock_client(client)
    mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("rate limit exceeded"))

    with pytest.raises(Exception, match="rate limit"):
        await client.complete([Message(role="user", content="test")])

    assert client._client is mock_openai
    client._credentials.invalidate.assert_not_awaited()  # type: ignore[attr-defined]  # AsyncMock


@pytest.mark.asyncio
async def test_bug_litellm_client_not_reset_on_non_auth_error() -> None:
    """Non-auth errors must not clear the LiteLLM client."""
    client = _make_litellm_client()
    mock_openai = _inject_mock_client(client)
    mock_openai.chat.completions.create = AsyncMock(side_effect=Exception("rate limit exceeded"))

    with pytest.raises(Exception, match="rate limit"):
        await client.complete([Message(role="user", content="test")])

    assert client._client is mock_openai
    client._credentials.invalidate.assert_not_awaited()  # type: ignore[attr-defined]  # AsyncMock
