"""Proxy remote LiteLLM integration tests.

These tests run the Forge proxy as a subprocess and validate full request flow.

They are marked remote_litellm and require LITELLM_API_KEY plus
LITELLM_BASE_URL. Missing or unreachable infrastructure fails loudly.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.remote_litellm]


@pytest.mark.parametrize(
    ("template", "fixture_name", "expected_resolved_model"),
    [
        ("litellm-openai", "proxy_server_remote_openai", "openai/gpt-5.4-mini"),
        (
            "litellm-gemini",
            "proxy_server_remote_gemini",
            "vertex_ai/gemini-2.5-flash",
        ),
    ],
)
class TestProxyWithRemoteLiteLLM:
    def test_health_endpoint(
        self,
        request: pytest.FixtureRequest,
        template: str,
        fixture_name: str,
        expected_resolved_model: str,
    ) -> None:
        proxy_server: str = request.getfixturevalue(fixture_name)

        with httpx.Client() as client:
            resp = client.get(f"{proxy_server}/")
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_proxy"] is True
            assert data["template"] == template

    def test_simple_completion(
        self,
        request: pytest.FixtureRequest,
        template: str,
        fixture_name: str,
        expected_resolved_model: str,
    ) -> None:
        proxy_server: str = request.getfixturevalue(fixture_name)

        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{proxy_server}/v1/messages",
                json={
                    "model": "claude-3-5-haiku-20241022",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Say hello"}],
                },
                headers={"x-api-key": "test"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("X-Resolved-Tier") == "haiku"
        assert resp.headers.get("X-Resolved-Model") == expected_resolved_model

        data = resp.json()
        assert data["type"] == "message"
        assert "content" in data

    def test_streaming_completion(
        self,
        request: pytest.FixtureRequest,
        template: str,
        fixture_name: str,
        expected_resolved_model: str,
    ) -> None:
        proxy_server: str = request.getfixturevalue(fixture_name)

        with httpx.Client(timeout=60) as client:
            with client.stream(
                "POST",
                f"{proxy_server}/v1/messages",
                json={
                    "model": "claude-3-5-haiku-20241022",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Count 1 2 3"}],
                    "stream": True,
                },
                headers={"x-api-key": "test"},
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers.get("X-Resolved-Tier") == "haiku"
                assert resp.headers.get("X-Resolved-Model") == expected_resolved_model

                events = []
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        events.append(line)

                assert len(events) > 0
