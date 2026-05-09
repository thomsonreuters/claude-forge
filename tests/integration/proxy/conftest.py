"""Test fixtures for proxy integration tests.

These fixtures manage proxy server lifecycle and session state for integration tests.
Session state is passed to the proxy subprocess via FORGE_HOME environment variable.

Uses shared utilities from tests/fixtures/proxy for:
- Ephemeral port allocation (random OS-assigned ports)
- Process lifecycle management (wait_for_port, kill_process)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

import httpx
import pytest
from dotenv import load_dotenv

from tests.fixtures.proxy import allocate_ephemeral_port, kill_process, wait_for_port

# Load .env secrets (API keys) so fixtures can check them
# Use explicit path since pytest may run from different directories
_repo_root = Path(__file__).parent.parent.parent.parent
load_dotenv(_repo_root / ".env", override=False)

# File-level mark for ALL tests in integration/proxy/
pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class RegisteredProxyServer:
    """A started proxy with a registry-backed proxy identity."""

    proxy_id: str
    template: str
    base_url: str
    port: int
    forge_home: Path


def _check_port(port: int) -> bool:
    """Check if port is currently in use.

    Args:
        port: Port number to check.

    Returns:
        True if port is accepting connections.
    """
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


def _start_proxy_subprocess(
    *,
    template: str,
    port: int,
    forge_home: Path,
    env: dict[str, str],
    cwd: Path,
    proxy_id: str | None = None,
) -> subprocess.Popen:  # noqa: ARG001
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "forge.proxy.server",
        "--template",
        template,
        "--port",
        str(port),
    ]
    if proxy_id:
        cmd.extend(["--proxy-id", proxy_id])

    proc = subprocess.Popen(
        cmd,
        # Use DEVNULL for stdout to avoid pipe buffer deadlock
        # Keep stderr PIPE only for error capture on startup failure
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(cwd),
    )

    if not wait_for_port(port, timeout=10):
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        pytest.fail(f"Proxy failed to start on port {port}. Stderr: {stderr}")

    return proc


@contextmanager
def _with_forge_home(forge_home: Path) -> Generator[None, None, None]:
    """Temporarily point Forge helpers at a test FORGE_HOME."""
    old_forge_home = os.environ.get("FORGE_HOME")
    os.environ["FORGE_HOME"] = str(forge_home)
    try:
        yield
    finally:
        if old_forge_home is not None:
            os.environ["FORGE_HOME"] = old_forge_home
        else:
            os.environ.pop("FORGE_HOME", None)


def _register_proxy_for_test(
    *,
    proxy_id: str,
    template: str,
    port: int,
    forge_home: Path,
) -> None:
    """Create proxy.yaml and registry entries for strict proxy startup."""
    from forge.proxy.proxies import ProxyEntry, ProxyRegistryStore
    from forge.proxy.proxy_orchestrator import create_proxy_file

    base_url = f"http://localhost:{port}"
    with _with_forge_home(forge_home):
        create_proxy_file(
            proxy_id=proxy_id,
            template=template,
            base_url=base_url,
            port=port,
        )
        store = ProxyRegistryStore(registry_path=forge_home / "proxies" / "index.json")
        registry = store.read()
        registry.proxies[proxy_id] = ProxyEntry(
            proxy_id=proxy_id,
            template=template,
            base_url=base_url,
            port=port,
            pid=None,
            status="starting",
        )
        store.write(registry)


def _start_registered_proxy(
    *,
    proxy_id: str,
    template: str,
    module_forge_home: Path,
    tmp_path_factory,
    required_env_var: str,
    unreachable_fail_reason: str,
) -> Generator[RegisteredProxyServer, None, None]:
    if not os.environ.get(required_env_var):
        pytest.fail(f"{required_env_var} not set (required for {template} proxy tests)")

    port = allocate_ephemeral_port()
    base_url = f"http://localhost:{port}"
    _register_proxy_for_test(
        proxy_id=proxy_id,
        template=template,
        port=port,
        forge_home=module_forge_home,
    )

    env = os.environ.copy()
    env["FORGE_HOME"] = str(module_forge_home)

    cwd = tmp_path_factory.mktemp("forge_proxy_cwd_")
    proc = _start_proxy_subprocess(
        template=template,
        port=port,
        forge_home=module_forge_home,
        env=env,
        cwd=cwd,
        proxy_id=proxy_id,
    )

    _preflight_proxy(
        proxy_base_url=base_url,
        request_model="claude-3-5-haiku-20241022",
        max_tokens=8,
        unreachable_fail_reason=unreachable_fail_reason,
        template=template,
    )

    try:
        with _with_forge_home(module_forge_home):
            from forge.proxy.proxies import ProxyRegistryStore

            store = ProxyRegistryStore(registry_path=module_forge_home / "proxies" / "index.json")
            registry = store.read()
            entry = registry.proxies[proxy_id]
            entry.pid = proc.pid
            entry.status = "healthy"
            store.write(registry)

        yield RegisteredProxyServer(
            proxy_id=proxy_id,
            template=template,
            base_url=base_url,
            port=port,
            forge_home=module_forge_home,
        )
    finally:
        kill_process(proc.pid)


def _preflight_proxy(
    *,
    proxy_base_url: str,
    request_model: str,
    max_tokens: int,
    unreachable_fail_reason: str,
    template: str,
) -> None:
    """Validate proxy can successfully serve a small request.

    Fail loudly when the upstream is unreachable - integration tests should
    not silently skip when dependencies are missing.

    NOTE: All connectivity failures now result in test failures rather than
    skips. This ensures integration tests surface environmental issues immediately.
    """

    payload = {
        "model": request_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "ping"}],
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{proxy_base_url}/v1/messages",
                json=payload,
                headers={"x-api-key": "test"},
            )
    except httpx.RequestError as e:
        pytest.fail(f"{unreachable_fail_reason}: {e}")

    if resp.status_code in (502, 503, 504):
        pytest.fail(f"{unreachable_fail_reason}: status={resp.status_code}")

    if resp.status_code in (401, 403):
        pytest.fail(f"Auth failure during proxy preflight: status={resp.status_code}")

    if resp.status_code == 500:
        # Remote LiteLLM sometimes returns a generic 500 with a low-signal
        # message when unreachable from this environment.
        if template.startswith("litellm-"):
            pytest.fail(f"{unreachable_fail_reason}: status=500, body={resp.text[:200]}")

    if resp.status_code != 200:
        pytest.fail(f"Proxy preflight failed: status={resp.status_code}, body={resp.text[:500]}")


@pytest.fixture(scope="module")
def local_litellm() -> Generator[str, None, None]:
    """Start local LiteLLM if GEMINI_API_KEY set, otherwise check if running.

    Uses port 4001 for test isolation (dev uses port 4000).

    This fixture handles three scenarios:
    1. LiteLLM already running on port 4001 → use it
    2. GEMINI_API_KEY set → start LiteLLM using our script
    3. Neither → FAIL tests (dependencies must be available)

    Yields:
        Base URL for local LiteLLM server.
    """
    # Test port (4001) isolates from dev instance (4000)
    test_port = 4001
    base_url = f"http://localhost:{test_port}"

    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    already_running = _check_port(test_port)

    if already_running:
        yield base_url
        return

    if not has_key:
        pytest.fail(f"GEMINI_API_KEY not set and local LiteLLM not running on port {test_port}")

    # Ensure backend config exists
    subprocess.run(["uv", "run", "forge", "backend", "create", "litellm"], check=False)

    # Start LiteLLM on test port using forge backend CLI
    result = subprocess.run(
        ["uv", "run", "forge", "backend", "start", "litellm", "--port", str(test_port)],
        check=False,
    )

    if result.returncode != 0:
        pytest.fail(f"Failed to start local LiteLLM on port {test_port}")

    if not wait_for_port(test_port, timeout=30):
        pytest.fail(f"Local LiteLLM failed to start on port {test_port}")

    yield base_url

    # Cleanup - stop the test instance
    subprocess.run(
        ["uv", "run", "forge", "backend", "stop", "litellm", "--port", str(test_port)],
        check=False,
    )


@pytest.fixture(scope="module")
def module_forge_home() -> Generator[Path, None, None]:
    """Create a module-scoped temp directory for FORGE_HOME.

    This directory is shared by proxy and tests, enabling session manipulation.

    Yields:
        Path to temp forge home directory.
    """
    with tempfile.TemporaryDirectory(prefix="forge_test_") as tmpdir:
        forge_home = Path(tmpdir)
        yield forge_home


@pytest.fixture(scope="module")
def proxy_server(local_litellm: str, module_forge_home: Path, tmp_path_factory) -> Generator[str, None, None]:
    """Start proxy server for testing.

    Runs the proxy subprocess from an isolated temp directory so repo-local
    artifacts (.env, .claude/) do not affect the test.

    Args:
        local_litellm: Dependency ensuring LiteLLM is running.
        module_forge_home: Shared forge home directory.

    Yields:
        Base URL for proxy server.
    """
    port = allocate_ephemeral_port()

    # Create an isolated working directory (acts like a temp repo root)
    cwd = tmp_path_factory.mktemp("forge_proxy_cwd_")

    # Build environment with required variables
    env = os.environ.copy()
    env["FORGE_HOME"] = str(module_forge_home)  # Session config location
    env["LITELLM_LOCAL_BASE_URL"] = local_litellm  # Override .env dev URL with test URL

    proc = _start_proxy_subprocess(
        template="litellm-gemini-test",
        port=port,
        forge_home=module_forge_home,
        env=env,
        cwd=cwd,
    )

    proxy_base_url = f"http://localhost:{port}"

    # Local LiteLLM is already gated by local_litellm fixture.
    _preflight_proxy(
        proxy_base_url=proxy_base_url,
        request_model="claude-3-5-haiku-20241022",
        max_tokens=8,
        unreachable_fail_reason="Local LiteLLM proxy unreachable",
        template="litellm-gemini-test",
    )

    yield proxy_base_url

    # Robust cleanup
    kill_process(proc.pid)


@pytest.fixture
def temp_session(module_forge_home: Path, tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    """Create temporary session with overrides for routing tests.

    Creates actual filesystem state that the proxy subprocess can read:
    - Temp worktree with .forge/sessions/<name>/forge.session.json

    Args:
        module_forge_home: Shared forge home directory (proxy uses this).
        tmp_path: pytest temp path for worktree.

    Yields:
        Dict with worktree path, manifest, and forge_home.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    forge_dir = worktree / ".forge"
    forge_dir.mkdir()

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "name": "test-session",
        "created_at": now,
        "last_accessed_at": now,
        "intent": {},
        "overrides": {"policy": {"fail_mode": "open"}},
        "confirmed": {},
    }
    session_dir = forge_dir / "sessions" / "test-session"
    session_dir.mkdir(parents=True)
    (session_dir / "forge.session.json").write_text(json.dumps(manifest))

    yield {
        "worktree": str(worktree),
        "manifest": manifest,
        "forge_home": module_forge_home,
    }


@pytest.fixture
def no_active_session() -> Generator[None, None, None]:
    """Marker fixture for tests that run without a Forge session context.

    Under the 1:1 model there is no global active-session pointer; this
    fixture is retained only as a semantic marker for test readability.
    """
    yield


@pytest.fixture(scope="module")
def proxy_server_remote_openai(module_forge_home: Path, tmp_path_factory) -> Generator[str, None, None]:
    if not os.environ.get("LITELLM_API_KEY"):
        pytest.fail("LITELLM_API_KEY not set (required for remote LiteLLM tests)")

    port = allocate_ephemeral_port()
    env = os.environ.copy()
    env["FORGE_HOME"] = str(module_forge_home)

    cwd = tmp_path_factory.mktemp("forge_proxy_cwd_")

    proc = _start_proxy_subprocess(
        template="litellm-openai",
        port=port,
        forge_home=module_forge_home,
        env=env,
        cwd=cwd,
    )
    proxy_base_url = f"http://localhost:{port}"

    _preflight_proxy(
        proxy_base_url=proxy_base_url,
        request_model="claude-3-5-haiku-20241022",
        max_tokens=8,
        unreachable_fail_reason="Remote LiteLLM (OpenAI) unreachable from this environment",
        template="litellm-openai",
    )

    yield proxy_base_url
    kill_process(proc.pid)


@pytest.fixture(scope="module")
def proxy_server_remote_gemini(module_forge_home: Path, tmp_path_factory) -> Generator[str, None, None]:
    if not os.environ.get("LITELLM_API_KEY"):
        pytest.fail("LITELLM_API_KEY not set (required for remote LiteLLM tests)")

    port = allocate_ephemeral_port()
    env = os.environ.copy()
    env["FORGE_HOME"] = str(module_forge_home)

    cwd = tmp_path_factory.mktemp("forge_proxy_cwd_")

    proc = _start_proxy_subprocess(
        template="litellm-gemini",
        port=port,
        forge_home=module_forge_home,
        env=env,
        cwd=cwd,
    )
    proxy_base_url = f"http://localhost:{port}"

    _preflight_proxy(
        proxy_base_url=proxy_base_url,
        request_model="claude-3-5-haiku-20241022",
        max_tokens=8,
        unreachable_fail_reason="Remote LiteLLM (Gemini) unreachable from this environment",
        template="litellm-gemini",
    )

    yield proxy_base_url
    kill_process(proc.pid)


@pytest.fixture(scope="module")
def proxy_server_openrouter(module_forge_home: Path, tmp_path_factory) -> Generator[str, None, None]:
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.fail("OPENROUTER_API_KEY not set (required for OpenRouter proxy tests)")

    port = allocate_ephemeral_port()
    env = os.environ.copy()
    env["FORGE_HOME"] = str(module_forge_home)

    cwd = tmp_path_factory.mktemp("forge_proxy_cwd_")

    proc = _start_proxy_subprocess(
        template="openrouter-anthropic",
        port=port,
        forge_home=module_forge_home,
        env=env,
        cwd=cwd,
    )
    proxy_base_url = f"http://localhost:{port}"

    _preflight_proxy(
        proxy_base_url=proxy_base_url,
        request_model="claude-3-5-haiku-20241022",
        max_tokens=8,
        unreachable_fail_reason="OpenRouter proxy unreachable",
        template="openrouter-anthropic",
    )

    yield proxy_base_url
    kill_process(proc.pid)


@pytest.fixture(scope="module")
def registered_proxy_server_openrouter(
    module_forge_home: Path, tmp_path_factory
) -> Generator[RegisteredProxyServer, None, None]:
    yield from _start_registered_proxy(
        proxy_id="openrouter-paid-e2e",
        template="openrouter-anthropic",
        module_forge_home=module_forge_home,
        tmp_path_factory=tmp_path_factory,
        required_env_var="OPENROUTER_API_KEY",
        unreachable_fail_reason="OpenRouter proxy unreachable",
    )


@pytest.fixture(scope="module")
def registered_proxy_server_local_gemini(
    local_litellm: str,  # noqa: ARG001 — ensures LiteLLM is running on test port
    module_forge_home: Path,
    tmp_path_factory,
) -> Generator[RegisteredProxyServer, None, None]:
    yield from _start_registered_proxy(
        proxy_id="litellm-gemini-local-e2e",
        template="litellm-gemini-test",
        module_forge_home=module_forge_home,
        tmp_path_factory=tmp_path_factory,
        required_env_var="GEMINI_API_KEY",
        unreachable_fail_reason="Local LiteLLM (Gemini) unreachable",
    )
