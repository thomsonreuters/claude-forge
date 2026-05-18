"""Unit tests for proxy orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.proxy.proxies import ProxyRegistryStore
from forge.proxy.proxy_orchestrator import ProxyStartError, start_proxy


class _Proc:
    """Fake process object for orchestrator tests."""

    returncode = None

    def __init__(self, pid: int = 4242):
        self.pid = pid

    def poll(self):
        return None


# ---------------------------------------------------------------------------
# Reuse / adopt / spawn basics
# ---------------------------------------------------------------------------


def test_start_reuses_healthy_updates_last_seen(
    orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
) -> None:
    registry_path = orch_registry.registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text("""
{
  "version": 1,
  "proxies": {
    "proxy_1": {
      "proxy_id": "proxy_1",
      "template": "litellm-openai",
      "base_url": "http://localhost:8085",
      "port": 8085,
      "pid": 123,
      "created_at": "2025-12-20T00:00:00+00:00",
      "last_seen_at": "2025-12-20T00:01:00+00:00",
      "status": "healthy"
    }
  }
}
""".strip() + "\n")

    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2025-12-21T00:00:00+00:00")

    result = start_proxy(template="litellm-openai")

    assert result.source == "reuse"
    assert result.proxy.proxy_id == "proxy_1"
    assert result.proxy.last_seen_at == "2025-12-21T00:00:00+00:00"


def test_start_adopts_healthy_default_port(orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)

    def _health(
        *,
        base_url: str,
        expected_template: str,
        timeout_s: float,
        expected_proxy_id: str | None = None,
        require_unregistered: bool = False,
    ) -> bool:
        assert expected_proxy_id is None
        assert require_unregistered is True
        return base_url == "http://localhost:5555" and expected_template == "litellm-openai"

    orch_health(_health)
    monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_adopted")
    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2025-12-21T00:00:00+00:00")

    result = start_proxy(template="litellm-openai")

    assert result.source == "adopt"
    assert result.proxy.proxy_id == "proxy_adopted"
    assert result.proxy.pid is None

    registry = orch_registry.read()
    assert "proxy_adopted" in registry.proxies


def test_start_rejects_adopt_when_live_proxy_has_identity(
    tmp_path,
    orch_stubs,
    orch_registry,
    orchestrator,
    monkeypatch,
) -> None:
    """Healthy default-port proxies that already report an identity must not be adopted."""
    monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
    monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda port: port == 8085)
    monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 8086)
    monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_spawned")
    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-03-20T12:00:00+00:00")
    monkeypatch.setattr(
        orchestrator,
        "_spawn_proxy_process",
        lambda **_: (_Proc(pid=9999), tmp_path / "stderr.log"),
    )

    health_calls: list[tuple[str, str | None, bool]] = []

    def _health(
        *,
        base_url: str,
        expected_template: str,
        timeout_s: float,
        expected_proxy_id: str | None = None,
        require_unregistered: bool = False,
    ) -> bool:
        assert expected_template == "litellm-openai"
        health_calls.append((base_url, expected_proxy_id, require_unregistered))

        if base_url == "http://localhost:8085":
            # Simulate a healthy proxy already owned by another Forge home.
            return not require_unregistered

        return False

    monkeypatch.setattr(orchestrator, "check_proxy_health", _health)

    result = start_proxy(template="litellm-openai")

    assert result.source == "spawn"
    assert result.proxy.proxy_id == "proxy_spawned"
    assert result.proxy.port == 8086
    assert ("http://localhost:8085", None, True) in health_calls


def test_start_spawns_new_and_persists(
    tmp_path, orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
) -> None:
    monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)
    monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
    monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 7777)
    orch_health(False)

    monkeypatch.setattr(
        orchestrator,
        "_spawn_proxy_process",
        lambda **_: (_Proc(), tmp_path / "stderr.log"),
    )
    monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_spawned")
    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2025-12-21T00:00:00+00:00")

    result = start_proxy(template="litellm-openai")

    assert result.source == "spawn"
    assert result.proxy.proxy_id == "proxy_spawned"
    assert result.proxy.pid == 4242
    assert result.proxy.base_url == "http://localhost:7777"

    registry = orch_registry.read()
    assert "proxy_spawned" in registry.proxies


def test_start_timeout_terminates_process(tmp_path, orch_registry, orch_health, orchestrator, monkeypatch) -> None:
    # Does NOT use orch_stubs — needs custom _wait_until_healthy, _validate_template_exists, _ensure_template_credentials
    monkeypatch.setattr(orchestrator, "_validate_template_exists", lambda _: None)
    monkeypatch.setattr(orchestrator, "_ensure_template_credentials", lambda _: None)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.test.example.com")
    monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)
    monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
    monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 7777)
    orch_health(False)

    proc = _Proc()
    monkeypatch.setattr(
        orchestrator,
        "_spawn_proxy_process",
        lambda **_: (proc, tmp_path / "stderr.log"),
    )

    terminated: dict[str, bool] = {"called": False}

    def _terminate(p):
        assert p is proc
        terminated["called"] = True

    monkeypatch.setattr(orchestrator, "_terminate_process", _terminate)

    def _wait(**_):
        raise ProxyStartError("Timed out")

    monkeypatch.setattr(orchestrator, "_wait_until_healthy", _wait)

    with pytest.raises(ProxyStartError, match="Timed out"):
        start_proxy(template="litellm-openai", timeout_s=0.01)

    assert terminated["called"] is True


# ---------------------------------------------------------------------------
# Subprocess env var passing
# ---------------------------------------------------------------------------


class TestSpawnPassesProxyIdCliArg:
    """Tests that _spawn_proxy_process passes --proxy-id as CLI arg."""

    def test_spawn_passes_proxy_id_cli_arg(self, monkeypatch: pytest.MonkeyPatch, orchestrator) -> None:
        """Verify _spawn_proxy_process passes --proxy-id in subprocess command."""
        import subprocess

        captured_cmd: list[str] = []

        def mock_popen(cmd, stdout, stderr, env):
            captured_cmd.extend(cmd)
            return _Proc(pid=1234)

        monkeypatch.setattr(orchestrator, "_check_proxy_dependencies", lambda **_kw: None)
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        _, _ = orchestrator._spawn_proxy_process(
            template="litellm-openai",
            host="localhost",
            port=8085,
            proxy_id="proxy_abc123",
        )

        assert "--proxy-id" in captured_cmd
        assert "--log-level" not in captured_cmd
        idx = captured_cmd.index("--proxy-id")
        assert captured_cmd[idx + 1] == "proxy_abc123"

    def test_spawn_does_not_set_forge_proxy_id_env(self, monkeypatch: pytest.MonkeyPatch, orchestrator) -> None:
        """Verify _spawn_proxy_process does not inject FORGE_PROXY_ID into env."""
        import subprocess

        captured_env: dict[str, str] = {}

        def mock_popen(cmd, stdout, stderr, env):
            captured_env.update(env)
            return _Proc(pid=1234)

        monkeypatch.setattr(orchestrator, "_check_proxy_dependencies", lambda **_kw: None)
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.delenv("FORGE_PROXY_ID", raising=False)

        _, _ = orchestrator._spawn_proxy_process(
            template="litellm-openai",
            host="localhost",
            port=8085,
            proxy_id="proxy_abc123",
        )

        assert "FORGE_PROXY_ID" not in captured_env

    def test_spawn_preserves_existing_env_vars(self, monkeypatch: pytest.MonkeyPatch, orchestrator) -> None:
        """Verify _spawn_proxy_process doesn't drop existing env vars."""
        import subprocess

        monkeypatch.setenv("LITELLM_API_KEY", "secret_key_123")
        monkeypatch.setenv("HOME", "/home/user")

        captured_env: dict[str, str] = {}

        def mock_popen(cmd, stdout, stderr, env):
            captured_env.update(env)
            return _Proc(pid=1234)

        monkeypatch.setattr(orchestrator, "_check_proxy_dependencies", lambda **_kw: None)
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        _, _ = orchestrator._spawn_proxy_process(
            template="litellm-openai",
            host="localhost",
            port=8085,
            proxy_id="proxy_abc123",
        )

        assert captured_env["LITELLM_API_KEY"] == "secret_key_123"
        assert captured_env["HOME"] == "/home/user"


# ---------------------------------------------------------------------------
# Proxy ID generation timing
# ---------------------------------------------------------------------------


class TestStartGeneratesProxyIdBeforeSpawn:
    """Tests that start_proxy generates proxy_id before spawning."""

    def test_proxy_id_generated_before_spawn(
        self,
        tmp_path,
        orch_stubs,
        orch_registry,
        orch_health,
        orchestrator,
        monkeypatch,
    ) -> None:
        """Verify proxy_id is pre-generated so it can be passed to subprocess."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 7777)
        orch_health(False)

        spawned_proxy_id: str | None = None

        def capture_spawn(*, template, host, port, proxy_id, provider=""):
            nonlocal spawned_proxy_id
            spawned_proxy_id = proxy_id
            return (_Proc(), tmp_path / "stderr.log")

        monkeypatch.setattr(orchestrator, "_spawn_proxy_process", capture_spawn)
        monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_pregenerated")
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2025-12-21T00:00:00+00:00")

        result = start_proxy(template="litellm-openai")

        assert result.source == "spawn"
        assert result.proxy.proxy_id == "proxy_pregenerated"
        assert spawned_proxy_id == "proxy_pregenerated"


# ---------------------------------------------------------------------------
# Explicit proxy_id
# ---------------------------------------------------------------------------


class TestStartWithExplicitProxyId:
    """Tests for start_proxy() with explicit proxy_id parameter."""

    def _setup_spawn_mocks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        orchestrator,
        orch_registry,
        orch_health,
    ) -> ProxyRegistryStore:
        """Common mock setup for spawn-path tests."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 8085)
        orch_health(False)
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            orchestrator,
            "_spawn_proxy_process",
            lambda **_: (_Proc(pid=9999), tmp_path / "stderr.log"),
        )
        return orch_registry

    def test_reuses_that_specific_proxy(
        self, orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
    ) -> None:
        """When proxy_id is given and that proxy is healthy, reuse it."""
        registry_path = orch_registry.registry_path
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            '{"version": 1, "proxies": {'
            '"my-proxy": {"proxy_id": "my-proxy", "template": "litellm-openai",'
            '"base_url": "http://localhost:9999", "port": 9999, "pid": 100,'
            '"created_at": "2026-01-01T00:00:00+00:00", "last_seen_at": "2026-01-01T00:00:00+00:00",'
            '"status": "healthy"}}}\n'
        )

        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-01-02T00:00:00+00:00")

        result = start_proxy(template="litellm-openai", proxy_id="my-proxy")

        assert result.source == "reuse"
        assert result.proxy.proxy_id == "my-proxy"
        assert result.proxy.port == 9999

    def test_skips_other_template_proxies(
        self,
        tmp_path,
        orch_stubs,
        orch_registry,
        orch_health,
        orchestrator,
        monkeypatch,
    ) -> None:
        """When proxy_id is given, do NOT reuse other proxies for the same template."""
        registry_path = orch_registry.registry_path
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        # "proxy_a" is healthy for the same template, but user asked for "my-proxy"
        registry_path.write_text(
            '{"version": 1, "proxies": {'
            '"proxy_a": {"proxy_id": "proxy_a", "template": "litellm-openai",'
            '"base_url": "http://localhost:8085", "port": 8085, "pid": 100,'
            '"created_at": "2026-01-01T00:00:00+00:00", "last_seen_at": "2026-01-01T00:00:00+00:00",'
            '"status": "healthy"}}}\n'
        )

        self._setup_spawn_mocks(tmp_path, monkeypatch, orchestrator, orch_registry, orch_health)

        result = start_proxy(template="litellm-openai", proxy_id="my-proxy")

        # Should NOT reuse proxy_a — it spawns a new one with the requested ID
        assert result.source == "spawn"
        assert result.proxy.proxy_id == "my-proxy"


# ---------------------------------------------------------------------------
# Explicit port
# ---------------------------------------------------------------------------


class TestStartWithExplicitPort:
    """Tests for start_proxy() with explicit port parameter."""

    def test_uses_explicit_port(
        self,
        tmp_path,
        orch_stubs,
        orch_registry,
        orch_health,
        orchestrator,
        monkeypatch,
    ) -> None:
        """When port is given, spawn at that port (no scanning)."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        orch_health(False)
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-01-01T00:00:00+00:00")
        monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_auto")

        monkeypatch.setattr(
            orchestrator,
            "_spawn_proxy_process",
            lambda **_: (_Proc(pid=5555), tmp_path / "stderr.log"),
        )

        # _find_available_port should NOT be called — track it
        find_port_called = {"called": False}
        original_find = orchestrator._find_available_port

        def _track_find(**kwargs):
            find_port_called["called"] = True
            return original_find(**kwargs)

        monkeypatch.setattr(orchestrator, "_find_available_port", _track_find)

        result = start_proxy(template="litellm-openai", port=9999)

        assert result.source == "spawn"
        assert result.proxy.port == 9999
        assert result.proxy.base_url == "http://localhost:9999"
        assert not find_port_called["called"], "_find_available_port should not be called with explicit port"

    def test_explicit_port_in_use_raises(
        self, orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
    ) -> None:
        """When port is given and in use (not adoptable), raise ProxyStartError."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: True)
        orch_health(False)

        with pytest.raises(ProxyStartError, match="Port 9999 is already in use"):
            start_proxy(template="litellm-openai", port=9999)

    def test_explicit_port_adopts_healthy_process(
        self, orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
    ) -> None:
        """When port is given and a healthy process is there, adopt it."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
        monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_adopted")
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-01-01T00:00:00+00:00")

        result = start_proxy(template="litellm-openai", port=9999)

        assert result.source == "adopt"
        assert result.proxy.port == 9999
        assert result.proxy.base_url == "http://localhost:9999"


# ---------------------------------------------------------------------------
# Both proxy_id and port
# ---------------------------------------------------------------------------


class TestStartWithBothProxyIdAndPort:
    """Tests for start_proxy() with both proxy_id and port."""

    def test_spawns_with_both(
        self,
        tmp_path,
        orch_stubs,
        orch_registry,
        orch_health,
        orchestrator,
        monkeypatch,
    ) -> None:
        """When both proxy_id and port are given, spawn uses both."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        orch_health(False)
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            orchestrator,
            "_spawn_proxy_process",
            lambda **_: (_Proc(pid=7777), tmp_path / "stderr.log"),
        )

        result = start_proxy(template="litellm-openai", proxy_id="my-proxy", port=9999)

        assert result.source == "spawn"
        assert result.proxy.proxy_id == "my-proxy"
        assert result.proxy.port == 9999
        assert result.proxy.base_url == "http://localhost:9999"


# ---------------------------------------------------------------------------
# skip_proxy_file
# ---------------------------------------------------------------------------


class TestSkipProxyFile:
    """Tests for start_proxy() with skip_proxy_file=True."""

    def test_skip_proxy_file_does_not_create_config(
        self,
        tmp_path,
        orch_stubs,
        orch_registry,
        orch_health,
        orchestrator,
        monkeypatch,
    ) -> None:
        """When skip_proxy_file=True, create_proxy_file is not called."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        orch_health(False)
        monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_auto")
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            orchestrator,
            "_spawn_proxy_process",
            lambda **_: (_Proc(pid=1234), tmp_path / "stderr.log"),
        )

        create_called = {"called": False}

        def _track_create(**kwargs):
            create_called["called"] = True

        monkeypatch.setattr(orchestrator, "create_proxy_file", _track_create)

        result = start_proxy(template="litellm-openai", skip_proxy_file=True)

        assert result.source == "spawn"
        assert not create_called["called"], "create_proxy_file should not be called with skip_proxy_file=True"


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------


def test_check_proxy_dependencies_raises_on_missing(monkeypatch: pytest.MonkeyPatch, orchestrator) -> None:
    """Verify that _check_proxy_dependencies raises helpful error when deps missing."""
    import builtins

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError(f"No module named '{name}'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    with pytest.raises(ProxyStartError) as exc_info:
        orchestrator._check_proxy_dependencies()

    error_msg = str(exc_info.value)
    assert "uvicorn" in error_msg
    assert "proxy dependencies" in error_msg.lower() or "uv sync" in error_msg
    assert "--no-start" in error_msg


# ---------------------------------------------------------------------------
# Credential preflight
# ---------------------------------------------------------------------------


class TestTemplateCredentialPreflight:
    """Tests for template credential checks before proxy spawn."""

    def test_skips_connection_value_vars(self, monkeypatch: pytest.MonkeyPatch, orchestrator) -> None:
        """LITELLM_BASE_URL is a connection value, so --base-url/proxy config may satisfy it."""
        monkeypatch.setenv("LITELLM_API_KEY", "sk-litellm-test")
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._get_file_secrets",
            lambda: {},
        )
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._auth_ignore_env",
            lambda: False,
        )

        orchestrator._ensure_template_credentials("litellm-anthropic")

    def test_start_with_upstream_base_url_does_not_require_litellm_base_url(
        self,
        tmp_path: Path,
        orch_registry,
        orch_health,
        monkeypatch: pytest.MonkeyPatch,
        orchestrator,
    ) -> None:
        """Remote LiteLLM --base-url satisfies the connection value without LITELLM_BASE_URL."""
        monkeypatch.setenv("LITELLM_API_KEY", "sk-litellm-test")
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._get_file_secrets",
            lambda: {},
        )
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._auth_ignore_env",
            lambda: False,
        )
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8092)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 8092)
        monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_remote")
        monkeypatch.setattr(orchestrator, "_wait_until_healthy", lambda **_: None)
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-05-13T00:00:00+00:00")
        orch_health(False)
        monkeypatch.setattr(
            orchestrator,
            "_spawn_proxy_process",
            lambda **_: (_Proc(pid=9999), tmp_path / "stderr.log"),
        )

        result = start_proxy(
            template="litellm-anthropic",
            upstream_base_url="https://litellm.example.com",
        )

        assert result.source == "spawn"
        assert result.proxy.proxy_id == "proxy_remote"


# ---------------------------------------------------------------------------
# Backend config staleness
# ---------------------------------------------------------------------------


class TestBackendConfigStaleness:
    """Tests for backend config outdated detection."""

    def test_outdated_detected_when_digests_differ(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """is_backend_config_outdated returns True when configs differ."""
        from forge.backend.creation import is_backend_config_outdated

        forge_home = tmp_path / "forge"
        backend_dir = forge_home / "backends" / "litellm"
        backend_dir.mkdir(parents=True)
        (backend_dir / "config.yaml").write_text("old: config\n")

        defaults_dir = tmp_path / "defaults" / "backends"
        defaults_dir.mkdir(parents=True)
        (defaults_dir / "litellm.yaml").write_text("new: config with more models\n")

        monkeypatch.setattr("forge.backend.creation.get_forge_home", lambda: forge_home)
        monkeypatch.setattr("forge.backend.creation.get_defaults_dir", lambda: tmp_path / "defaults")

        assert is_backend_config_outdated("litellm") is True

    def test_not_outdated_when_digests_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """is_backend_config_outdated returns False when configs are identical."""
        from forge.backend.creation import is_backend_config_outdated

        content = "same: config\n"

        forge_home = tmp_path / "forge"
        backend_dir = forge_home / "backends" / "litellm"
        backend_dir.mkdir(parents=True)
        (backend_dir / "config.yaml").write_text(content)

        defaults_dir = tmp_path / "defaults" / "backends"
        defaults_dir.mkdir(parents=True)
        (defaults_dir / "litellm.yaml").write_text(content)

        monkeypatch.setattr("forge.backend.creation.get_forge_home", lambda: forge_home)
        monkeypatch.setattr("forge.backend.creation.get_defaults_dir", lambda: tmp_path / "defaults")

        assert is_backend_config_outdated("litellm") is False

    def test_not_outdated_when_no_installed_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """is_backend_config_outdated returns False when no config exists yet."""
        from forge.backend.creation import is_backend_config_outdated

        forge_home = tmp_path / "forge"
        monkeypatch.setattr("forge.backend.creation.get_forge_home", lambda: forge_home)

        assert is_backend_config_outdated("litellm") is False


# ---------------------------------------------------------------------------
# Adoption under lock (TOCTOU fix)
# ---------------------------------------------------------------------------


class TestAdoptionUnderLock:
    """Tests for atomic adopt-under-lock behavior (M21 TOCTOU fix)."""

    def test_adopt_under_lock_prevents_duplicate(
        self, orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
    ) -> None:
        """Second adopt attempt sees first's entry and skips."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-02-09T00:00:00+00:00")

        call_count = {"n": 0}

        def _id_gen(existing=None) -> str:
            call_count["n"] += 1
            return f"proxy_{call_count['n']}"

        monkeypatch.setattr(orchestrator, "_new_proxy_id", _id_gen)

        # First call: adopts
        result1 = start_proxy(template="litellm-openai")
        assert result1.source == "adopt"
        assert result1.proxy.proxy_id == "proxy_1"

        # Second call: should reuse (now registered), not create a duplicate
        result2 = start_proxy(template="litellm-openai")
        assert result2.source == "reuse"
        assert result2.proxy.proxy_id == "proxy_1"

        # Verify only one entry in registry
        registry = orch_registry.read()
        assert len(registry.proxies) == 1

    def test_adopt_health_check_fails_no_entry(
        self,
        tmp_path,
        orch_stubs,
        orch_registry,
        orch_health,
        orchestrator,
        monkeypatch,
    ) -> None:
        """When health check fails, no entry is created in the registry."""
        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)
        monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda _: False)
        monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 7777)
        orch_health(False)
        monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_should_not_exist")
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-02-09T00:00:00+00:00")

        monkeypatch.setattr(
            orchestrator,
            "_spawn_proxy_process",
            lambda **_: (_Proc(pid=9999), tmp_path / "stderr.log"),
        )

        result = start_proxy(template="litellm-openai")

        assert result.source == "spawn"

        registry = orch_registry.read()
        assert len(registry.proxies) == 1
        entry = list(registry.proxies.values())[0]
        assert entry.pid == 9999

    def test_adopt_already_registered_skips(
        self, orch_stubs, orch_registry, orch_health, orchestrator, monkeypatch
    ) -> None:
        """When a proxy for the template+URL is already registered, adopt is skipped."""
        registry_path = orch_registry.registry_path
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            '{"version": 1, "proxies": {"existing": '
            '{"proxy_id": "existing", "template": "litellm-openai", '
            '"base_url": "http://localhost:5555", "port": 5555, "pid": 111, '
            '"created_at": "2026-01-01T00:00:00+00:00", '
            '"last_seen_at": "2026-01-01T00:00:00+00:00", "status": "healthy"}}}\n'
        )

        monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 5555)
        monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-02-09T00:00:00+00:00")

        result = start_proxy(template="litellm-openai")

        assert result.source == "reuse"
        assert result.proxy.proxy_id == "existing"

        registry = orch_registry.read()
        assert len(registry.proxies) == 1


# ---------------------------------------------------------------------------
# smoke_test_proxy
# ---------------------------------------------------------------------------


class TestSmokeTestProxy:
    def test_success_returns_text(self, monkeypatch):
        from forge.proxy import proxy_orchestrator

        class _FakeResp:
            status_code = 200
            text = '{"content":[{"text":"Hi there!"}]}'

            def json(self):
                return {"content": [{"text": "Hi there!"}]}

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, **kw):
                return _FakeResp()

        monkeypatch.setattr(proxy_orchestrator.httpx, "Client", lambda **kw: _FakeClient())

        ok, detail = proxy_orchestrator.smoke_test_proxy(base_url="http://localhost:9999")
        assert ok is True
        assert "Hi there!" in detail

    def test_http_error_retries_then_fails(self, monkeypatch):
        from forge.proxy import proxy_orchestrator

        call_count = 0

        class _FakeResp:
            status_code = 500
            text = "Internal Server Error"

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, **kw):
                nonlocal call_count
                call_count += 1
                return _FakeResp()

        monkeypatch.setattr(proxy_orchestrator.httpx, "Client", lambda **kw: _FakeClient())
        monkeypatch.setattr(proxy_orchestrator.time, "sleep", lambda _: None)

        ok, detail = proxy_orchestrator.smoke_test_proxy(base_url="http://localhost:9999")
        assert ok is False
        assert "500" in detail
        assert call_count == 2  # initial + 1 retry

    def test_retry_succeeds_on_second_attempt(self, monkeypatch):
        from forge.proxy import proxy_orchestrator

        attempt = 0

        class _FailResp:
            status_code = 500
            text = "fail"

        class _OkResp:
            status_code = 200
            text = '{"content":[{"text":"ok"}]}'

            def json(self):
                return {"content": [{"text": "ok"}]}

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, **kw):
                nonlocal attempt
                attempt += 1
                return _FailResp() if attempt == 1 else _OkResp()

        monkeypatch.setattr(proxy_orchestrator.httpx, "Client", lambda **kw: _FakeClient())
        monkeypatch.setattr(proxy_orchestrator.time, "sleep", lambda _: None)

        ok, detail = proxy_orchestrator.smoke_test_proxy(base_url="http://localhost:9999")
        assert ok is True
        assert "ok" in detail

    def test_timeout_returns_error(self, monkeypatch):
        import httpx as _httpx

        from forge.proxy import proxy_orchestrator

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, **kw):
                raise _httpx.ReadTimeout("timed out")

        monkeypatch.setattr(proxy_orchestrator.httpx, "Client", lambda **kw: _FakeClient())
        monkeypatch.setattr(proxy_orchestrator.time, "sleep", lambda _: None)

        ok, detail = proxy_orchestrator.smoke_test_proxy(base_url="http://localhost:9999", timeout_s=1.0)
        assert ok is False
        assert "timed out" in detail.lower()
