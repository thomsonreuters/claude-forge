"""Regression: proxy in registry but not actually running shows as unavailable.

Bug: forge workflow list-models showed all models as available even when their
proxy backends weren't running. lookup_proxy_base_url() only checked the
registry file, not whether the proxy was actually reachable. A stale "healthy"
entry or a "configured" entry that was never started both resolved successfully,
leading skills to pick models that couldn't serve requests.

Fix: check_proxy_reachable() resolves via registry AND does an HTTP health check.
check_model_availability() uses it so both list-models and preflight agree.

Affected: src/forge/core/reactive/proxy.py, src/forge/review/models.py,
          src/forge/review/engine.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge.core.reactive.proxy import check_proxy_reachable
from forge.proxy.proxies import ProxyEntry, ProxyRegistry
from forge.review.models import ModelSpec, check_model_availability

pytestmark = pytest.mark.regression


def _entry(
    proxy_id: str = "my-proxy",
    template: str = "litellm-openai",
    base_url: str = "http://localhost:8085",
    status: str = "healthy",
) -> ProxyEntry:
    return ProxyEntry(
        proxy_id=proxy_id,
        template=template,
        base_url=base_url,
        port=8085,
        status=status,
    )


def _mock_store(registry: ProxyRegistry) -> MagicMock:
    mock = MagicMock()
    mock.return_value.read.return_value = registry
    return mock


def _spec(name: str, proxy: str) -> ModelSpec:
    return ModelSpec(
        name=name,
        model_id=name,
        family="openai",
        provider_refs=(("openrouter", f"openai/{name}"),),
        description="Test",
        preferred_proxy=proxy,
    )


class TestStaleProxyNotReady:
    """Registry entry with stale 'healthy' status but dead process."""

    def test_stale_healthy_entry_is_not_reachable(self):
        entry = _entry(status="healthy")
        registry = ProxyRegistry(proxies={"my-proxy": entry})

        with (
            patch("forge.proxy.proxies.ProxyRegistryStore", _mock_store(registry)),
            patch("forge.proxy.proxy_orchestrator.check_proxy_health", return_value=False),
        ):
            reachable, reason, url = check_proxy_reachable("my-proxy")

        assert reachable is False
        assert "not responding" in reason
        assert url == "http://localhost:8085"

    def test_configured_entry_is_not_reachable(self):
        entry = _entry(status="configured")
        registry = ProxyRegistry(proxies={"my-proxy": entry})

        with (
            patch("forge.proxy.proxies.ProxyRegistryStore", _mock_store(registry)),
            patch("forge.proxy.proxy_orchestrator.check_proxy_health", return_value=False),
        ):
            reachable, reason, _url = check_proxy_reachable("my-proxy")

        assert reachable is False

    def test_model_unavailable_when_no_proxy_resolves(self):
        """check_model_availability reports unavailable when routing finds no proxy."""
        result = check_model_availability([_spec("gpt-5.5", "litellm-openai")])

        assert result[0].status == "unavailable"
