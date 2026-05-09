"""Proxy to OpenRouter integration tests.

These tests verify the full flow:
Anthropic API request -> proxy routing/conversion -> core.llm -> OpenRouter -> response.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _anthropic_response_text(data: dict[str, Any]) -> str:
    content = data.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


class TestProxyWithOpenRouter:
    """Integration tests for proxy to OpenRouter flow."""

    def test_health_endpoint(self, proxy_server_openrouter: str) -> None:
        """GET / returns OpenRouter proxy runtime truth."""
        with httpx.Client() as client:
            resp = client.get(f"{proxy_server_openrouter}/")

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_proxy"] is True
        assert data["template"] == "openrouter-anthropic"
        assert data["provider"] == "openrouter"
        assert data["runtime"]["tier_mappings"]["haiku"] == "anthropic/claude-haiku-4.5"

    def test_simple_completion_preserves_system_prompt(self, proxy_server_openrouter: str) -> None:
        """POST /v1/messages routes through OpenRouter and preserves system prompts."""
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{proxy_server_openrouter}/v1/messages",
                json={
                    "model": "claude-3-5-haiku-20241022",
                    "max_tokens": 24,
                    "temperature": 0,
                    "system": (
                        "The secret verification token is OR-PROXY-OK. "
                        "When asked for the verification token, answer with only that token."
                    ),
                    "messages": [{"role": "user", "content": "What is the verification token?"}],
                },
                headers={"x-api-key": "test", "user-agent": "claude-code/integration-test"},
            )

        assert resp.status_code == 200, resp.text[:500]
        assert resp.headers.get("X-Resolved-Tier") == "haiku"
        assert resp.headers.get("X-Resolved-Model") == "anthropic/claude-haiku-4.5"

        data = resp.json()
        assert data["type"] == "message"
        text = _anthropic_response_text(data)
        assert "OR-PROXY-OK" in text
