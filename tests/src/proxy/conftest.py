"""Pytest configuration for proxy tests.

Provides fixtures that ensure test isolation for config state
and reduce monkeypatch boilerplate for orchestrator/identity/routing tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import forge.proxy.proxy_orchestrator as _orchestrator_module
from forge.proxy.proxies import ProxyRegistryStore

# ---------------------------------------------------------------------------
# Config isolation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_config_singleton():
    """Reset the config singleton before and after each test.

    This ensures test isolation when tests modify the global config state,
    especially when using monkeypatch.setattr on config proxy objects.

    The config module uses a _ConfigProxy that delegates to get_config().
    When tests monkeypatch attributes directly on the proxy, the proxy's
    __dict__ gets modified. This fixture ensures a clean state.
    """
    import forge.config as config_module

    # Reset _config singleton before test
    config_module._config = None

    # Also clean up any attributes that might have been set on the proxy
    # by tests using monkeypatch.setattr(server.config, "proxy", ...)
    proxy_obj = config_module.config
    for attr in ["proxy", "session"]:
        if attr in proxy_obj.__dict__:
            del proxy_obj.__dict__[attr]

    yield

    # Reset after test
    config_module._config = None

    # Clean up proxy attributes again
    for attr in ["proxy", "session"]:
        if attr in proxy_obj.__dict__:
            del proxy_obj.__dict__[attr]


# ---------------------------------------------------------------------------
# Orchestrator fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator():
    """Provide the orchestrator module for monkeypatching."""
    return _orchestrator_module


@pytest.fixture
def orch_stubs(monkeypatch: pytest.MonkeyPatch, orchestrator) -> None:
    """Stub orchestrator functions that never vary across tests.

    Patches: _validate_template_exists, _wait_until_healthy.
    Sets LITELLM_BASE_URL for remote templates that have no hardcoded URL.
    """
    monkeypatch.setattr(orchestrator, "_validate_template_exists", lambda _: None)
    monkeypatch.setattr(orchestrator, "_ensure_template_credentials", lambda _: None)
    monkeypatch.setattr(orchestrator, "_wait_until_healthy", lambda **_: None)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.test.example.com")


@pytest.fixture
def orch_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator) -> ProxyRegistryStore:
    """ProxyRegistryStore backed by tmp_path, wired into orchestrator.

    Returns the store so tests can pre-populate registry content.
    """
    registry_path = tmp_path / "proxies" / "index.json"
    store = ProxyRegistryStore(registry_path)
    monkeypatch.setattr(orchestrator, "ProxyRegistryStore", lambda *a, **kw: store)
    return store


@pytest.fixture
def orch_health(monkeypatch: pytest.MonkeyPatch, orchestrator):
    """Configurable check_proxy_health — defaults to healthy (True).

    Call the returned function to override:
        orch_health(False)         # always unhealthy
        orch_health(custom_fn)     # custom callable
    """

    def configure(healthy=True):
        if callable(healthy) and not isinstance(healthy, bool):
            monkeypatch.setattr(orchestrator, "check_proxy_health", healthy)
        else:
            monkeypatch.setattr(orchestrator, "check_proxy_health", lambda **_: healthy)

    configure(True)
    return configure


# ---------------------------------------------------------------------------
# Identity / startup fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch get_proxy_registry_path to return a tmp_path-based path.

    Returns the registry_path so tests can write JSON to it.
    Used by: test_proxy_identity.py, test_proxy_startup.py.
    """
    from forge.proxy import proxies

    registry_path = tmp_path / "proxies" / "index.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(proxies, "get_proxy_registry_path", lambda: registry_path)
    return registry_path


# ---------------------------------------------------------------------------
# Server routing fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_stubs(monkeypatch: pytest.MonkeyPatch):
    """Stub common server no-ops for routing invariant tests.

    Patches reload, logging, and tool-failure checking.
    Returns the server module for additional per-test patching.
    """
    from forge.proxy import server

    monkeypatch.setattr(server, "reload", lambda: None)
    monkeypatch.setattr(server, "log_request_response", lambda *a, **kw: None)
    monkeypatch.setattr(server, "log_request_beautifully", lambda *a, **kw: None)
    monkeypatch.setattr(server, "log_tool_event", lambda *a, **kw: None)
    monkeypatch.setattr(server, "log_tool_failure", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_check_client_tool_failures", lambda *a, **kw: None)
    return server
