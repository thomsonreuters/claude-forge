"""Regression: HTTP error responses must not expose internal/provider details.

Bug: Auth errors returned f"Authentication failed: {e}" which could leak
provider-specific exception messages. Generic errors returned the full
exception string. Token counting and streaming errors had the same issue.

Root cause: Four error handlers in server.py and converters.py forwarded raw
exception strings to clients instead of sanitized messages.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.proxy.cost_tracker import CapResult
from forge.proxy.server import _cap_result_message

pytestmark = pytest.mark.regression


def test_bug_cap_message_format() -> None:
    result = CapResult(exceeded=True, cap_type="daily", current_micros=5_500_000, limit_micros=5_000_000)
    msg = _cap_result_message(result)
    assert "daily" in msg
    assert "$5.50" in msg
    assert "$5.00" in msg


def test_bug_auth_error_no_provider_detail() -> None:
    """Auth error response must contain request_id, not the exception message."""
    from fastapi.testclient import TestClient

    from forge.proxy.server import app

    secret_detail = "InvalidAPIKey: sk-secret-key-xxx is not valid for org-internal-12345"

    with patch("forge.proxy.server._ensure_runtime_state"):
        with patch("forge.proxy.server.config") as mock_config:
            mock_config.proxy.default_tier = "sonnet"
            mock_config.proxy.preferred_provider = "litellm"
            mock_config.proxy.get_provider.return_value = SimpleNamespace(
                tiers=SimpleNamespace(haiku="m1", sonnet="m2", opus="m3"),
                tier_overrides={},
                model_alternatives={},
                error_hints=False,
            )
            mock_config.proxy.get_model_for_tier.return_value = "test-model"

            factory_mock = MagicMock()
            factory_mock.detect_provider_for_model.return_value = SimpleNamespace(value="litellm_remote")

            from forge.core.llm.errors import AuthenticationError

            factory_mock.get_client = AsyncMock(side_effect=AuthenticationError("litellm_remote", secret_detail))

            with patch("forge.proxy.server.client_factory", factory_mock):
                with patch("forge.proxy.server.cost_tracker", None):
                    with patch("forge.proxy.server.log_request_beautifully"):
                        with patch("forge.proxy.server.log_request_response", new_callable=AsyncMock):
                            with patch("forge.proxy.server._check_client_tool_failures", new_callable=AsyncMock):
                                client = TestClient(app)
                                response = client.post(
                                    "/v1/messages",
                                    json={
                                        "model": "claude-sonnet-4-6",
                                        "messages": [{"role": "user", "content": "hi"}],
                                        "max_tokens": 10,
                                    },
                                )

    assert response.status_code == 401
    body = response.json()
    assert re.search(r"\[req_[a-f0-9]{12}\]", body["detail"]["message"])
    assert secret_detail not in body["detail"]["message"]
    assert "sk-secret" not in json.dumps(body)


def test_bug_internal_error_no_exception_detail() -> None:
    """Generic 500 error must not expose exception internals."""
    from fastapi.testclient import TestClient

    from forge.proxy.server import app

    secret_detail = "ConnectionRefusedError: [Errno 111] internal-host.corp:443"

    with patch("forge.proxy.server._ensure_runtime_state"):
        with patch("forge.proxy.server.config") as mock_config:
            mock_config.proxy.default_tier = "sonnet"
            mock_config.proxy.preferred_provider = "litellm"
            mock_config.proxy.get_provider.return_value = SimpleNamespace(
                tiers=SimpleNamespace(haiku="m1", sonnet="m2", opus="m3"),
                tier_overrides={},
                model_alternatives={},
                error_hints=False,
            )
            mock_config.proxy.get_model_for_tier.return_value = "test-model"

            factory_mock = MagicMock()
            factory_mock.detect_provider_for_model.return_value = SimpleNamespace(value="litellm_remote")
            factory_mock.get_client = AsyncMock(side_effect=RuntimeError(secret_detail))

            with patch("forge.proxy.server.client_factory", factory_mock):
                with patch("forge.proxy.server.cost_tracker", None):
                    with patch("forge.proxy.server.log_request_beautifully"):
                        with patch("forge.proxy.server.log_request_response", new_callable=AsyncMock):
                            with patch("forge.proxy.server._check_client_tool_failures", new_callable=AsyncMock):
                                client = TestClient(app)
                                response = client.post(
                                    "/v1/messages",
                                    json={
                                        "model": "claude-sonnet-4-6",
                                        "messages": [{"role": "user", "content": "hi"}],
                                        "max_tokens": 10,
                                    },
                                )

    assert response.status_code == 500
    body = response.json()
    assert re.search(r"\[req_[a-f0-9]{12}\]", body["detail"]["message"])
    assert secret_detail not in json.dumps(body)
    assert "internal-host.corp" not in json.dumps(body)


def test_bug_token_count_error_no_exception_detail() -> None:
    """Token counting error must not expose exception internals."""
    from fastapi.testclient import TestClient

    from forge.proxy.server import app

    secret_detail = "VertexAI auth failed for project secret-gcp-project-id"

    with patch("forge.proxy.server._ensure_runtime_state"):
        with patch("forge.proxy.server.config") as mock_config:
            mock_config.proxy.default_tier = "sonnet"
            mock_config.proxy.preferred_provider = "litellm"
            mock_config.proxy.get_provider.return_value = SimpleNamespace(
                tiers=SimpleNamespace(haiku="m1", sonnet="m2", opus="m3"),
                tier_overrides={},
                model_alternatives={},
            )
            mock_config.proxy.get_model_for_tier.return_value = "test-model"

            factory_mock = MagicMock()
            factory_mock.detect_provider_for_model.return_value = SimpleNamespace(value="litellm_remote")
            factory_mock.get_client = AsyncMock(side_effect=RuntimeError(secret_detail))

            with patch("forge.proxy.server.client_factory", factory_mock):
                client = TestClient(app)
                response = client.post(
                    "/v1/messages/count_tokens",
                    json={"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]},
                )

    assert response.status_code == 500
    body = response.json()
    assert secret_detail not in json.dumps(body)
    assert "secret-gcp-project" not in json.dumps(body)
    assert re.search(r"\[tok_[a-f0-9]{12}\]", body["detail"]["message"])


def test_bug_streaming_error_no_provider_detail() -> None:
    """Streaming error events must not expose upstream provider details."""
    from fastapi.testclient import TestClient

    from forge.proxy.base_client import ProxyStreamError
    from forge.proxy.server import app

    secret_detail = "upstream provider api.internal-llm.io returned 502"

    async def mock_stream(*args, **kwargs):
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise ProxyStreamError(secret_detail, error_type="api_error", status_code=502)

    with patch("forge.proxy.server._ensure_runtime_state"):
        with patch("forge.proxy.server.config") as mock_config:
            mock_config.proxy.default_tier = "sonnet"
            mock_config.proxy.preferred_provider = "litellm"
            mock_config.proxy.get_provider.return_value = SimpleNamespace(
                tiers=SimpleNamespace(haiku="m1", sonnet="m2", opus="m3"),
                tier_overrides={},
                model_alternatives={},
                error_hints=False,
            )
            mock_config.proxy.get_model_for_tier.return_value = "test-model"

            mock_client = AsyncMock()
            mock_client.create_streaming_completion = mock_stream

            factory_mock = MagicMock()
            factory_mock.detect_provider_for_model.return_value = SimpleNamespace(value="litellm_remote")
            factory_mock.get_client = AsyncMock(return_value=mock_client)

            with patch("forge.proxy.server.client_factory", factory_mock):
                with patch("forge.proxy.server.cost_tracker", None):
                    with patch("forge.proxy.server.proxy_metrics") as mock_metrics:
                        mock_metrics.total_cost_micros = 0
                        with patch("forge.proxy.server.log_request_beautifully"):
                            with patch("forge.proxy.server.log_request_response", new_callable=AsyncMock):
                                with patch("forge.proxy.server._check_client_tool_failures", new_callable=AsyncMock):
                                    client = TestClient(app)
                                    response = client.post(
                                        "/v1/messages",
                                        json={
                                            "model": "claude-sonnet-4-6",
                                            "messages": [{"role": "user", "content": "hi"}],
                                            "max_tokens": 10,
                                            "stream": True,
                                        },
                                    )

    body = response.text
    assert secret_detail not in body
    assert "api.internal-llm" not in body
    assert re.search(r"Streaming request failed \[req_[a-f0-9]{12}\]", body)
