"""Regression: sidecar subprocess proxy must fail closed when configured but unresolvable.

Bug: When FORGE_SUBPROCESS_PROXY was set but FORGE_SUBPROCESS_BASE_URL was missing
inside a sidecar, resolve_subprocess_routing() silently fell through to later chain
steps (preferred_proxy, route_scan, session_proxy). This could route requests through
an unrelated proxy or direct to Anthropic, bypassing the user's explicit intent.

Root cause: _resolve_injected_subprocess_proxy() returned None when the base URL env
var was missing, and the caller logged debug and continued instead of raising.
"""

from __future__ import annotations

import pytest

from forge.core.reactive.routing import ProxyRoutingError, resolve_subprocess_routing

pytestmark = pytest.mark.regression


def test_sidecar_with_subprocess_proxy_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured subprocess proxy in sidecar must raise, not silently fall through."""
    monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "my-proxy")
    monkeypatch.setenv("FORGE_SIDECAR", "1")
    monkeypatch.delenv("FORGE_SUBPROCESS_BASE_URL", raising=False)
    monkeypatch.delenv("FORGE_SUBPROCESS_PROXY_ID", raising=False)
    monkeypatch.delenv("FORGE_SUBPROCESS_TEMPLATE", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    with pytest.raises(ProxyRoutingError, match="not resolvable inside sidecar"):
        resolve_subprocess_routing()


def test_sidecar_without_subprocess_proxy_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """No subprocess proxy in sidecar should fall through to later chain steps normally."""
    monkeypatch.setenv("FORGE_SIDECAR", "1")
    monkeypatch.delenv("FORGE_SUBPROCESS_PROXY", raising=False)
    monkeypatch.delenv("FORGE_SUBPROCESS_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    result = resolve_subprocess_routing()
    assert result.source == "unresolved"


def test_sidecar_with_injected_metadata_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sidecar with properly injected metadata should resolve successfully."""
    monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "my-proxy")
    monkeypatch.setenv("FORGE_SIDECAR", "1")
    monkeypatch.setenv("FORGE_SUBPROCESS_BASE_URL", "http://host:8085")
    monkeypatch.setenv("FORGE_SUBPROCESS_PROXY_ID", "my-proxy")

    result = resolve_subprocess_routing()
    assert result.source == "subprocess_proxy"
    assert result.base_url == "http://host:8085"
