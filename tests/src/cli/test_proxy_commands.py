"""Tests for CLI proxy commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from forge.cli.main import main
from forge.cli.proxy import _ProxyInfo
from forge.proxy.proxies import ProxyEntry, ProxyRegistry, ProxyRegistryStore
from forge.session.identity import make_scoped_key


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Remote templates need a base URL (no longer hardcoded)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.test.example.com")

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()

    monkeypatch.chdir(project)
    return project


@pytest.fixture(autouse=True)
def safe_port_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default CLI proxy tests to "no listener found" for kill-by-port.

    These tests isolate FORGE_HOME, but force-delete/force-stop paths still use
    lsof against the real machine. Defaulting discovery to None keeps the suite
    hermetic; tests that need port-based kills override this explicitly.
    """
    monkeypatch.setattr("forge.cli.proxy.find_pid_by_port", lambda port: None)


def test_proxy_list_empty_shows_message(runner: CliRunner, temp_env: Path) -> None:
    result = runner.invoke(main, ["proxy", "list"])

    assert result.exit_code == 0
    assert "No proxies found" in result.output


def test_proxy_list_shows_entries(runner: CliRunner, temp_env: Path) -> None:
    forge_home = Path(os.environ["FORGE_HOME"])
    proxies_index = forge_home / "proxies" / "index.json"
    proxies_index.parent.mkdir(parents=True)

    proxies_index.write_text(
        json.dumps(
            {
                "version": 1,
                "proxies": {
                    "proxy_1": {
                        "proxy_id": "proxy_1",
                        "template": "litellm-openai",
                        "base_url": "http://localhost:8085",
                        "port": 8085,
                        "pid": None,
                        "status": "healthy",
                        "last_seen_at": "2025-12-20T00:01:00+00:00",
                    }
                },
            },
            indent=2,
        )
    )

    result = runner.invoke(main, ["proxy", "list"])

    assert result.exit_code == 0
    assert "proxy_1" in result.output
    assert "litellm-openai" in result.output
    assert "http://localhost:8085" in result.output


def test_proxy_create_reuses_healthy_registered_proxy(
    runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create (default) reuses existing healthy proxy instead of creating new one."""
    store = ProxyRegistryStore()
    registry = ProxyRegistry(
        proxies={
            "proxy_1": ProxyEntry(
                proxy_id="proxy_1",
                template="litellm-openai",
                base_url="http://localhost:8085",
                port=8085,
                pid=None,
                status="healthy",
                last_seen_at="2025-12-20T00:01:00+00:00",
                created_at="2025-12-20T00:00:00+00:00",
            )
        }
    )
    store.write(registry)

    import forge.proxy.proxy_orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "check_proxy_health", lambda **_: True)

    result = runner.invoke(main, ["proxy", "create", "litellm-openai"])

    assert result.exit_code == 0
    assert "Reusing existing" in result.output
    assert "proxy_1" in result.output
    assert "http://localhost:8085" in result.output


def test_proxy_create_adopts_healthy_default_port_when_not_registered(
    runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create (default) adopts existing proxy on default port if healthy."""
    import forge.proxy.proxy_orchestrator as orchestrator

    # litellm-openai template has default_port=8085. create_cmd resolves this
    # from load_config() and passes it to start_proxy(port=8085).
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
        assert expected_template == "litellm-openai"
        return base_url == "http://localhost:8085"

    monkeypatch.setattr(orchestrator, "check_proxy_health", _health)
    monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_adopted")
    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2025-12-21T00:00:00+00:00")

    result = runner.invoke(main, ["proxy", "create", "litellm-openai"])

    assert result.exit_code == 0
    assert "Found existing process on port 8085" in result.output
    assert "http://localhost:8085" in result.output
    assert "not started by Forge" in result.output

    store = ProxyRegistryStore()
    registry = store.read()
    # create_cmd without --name → orchestrator generates proxy_id
    assert "proxy_adopted" in registry.proxies
    entry = registry.proxies["proxy_adopted"]
    assert entry.pid is None
    assert entry.base_url == "http://localhost:8085"


def test_proxy_create_json_output(runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create --json outputs JSON with proxy details."""
    import forge.proxy.proxy_orchestrator as orchestrator

    # litellm-openai template has default_port=8085
    monkeypatch.setattr(orchestrator, "check_proxy_health", lambda **_: True)
    monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "proxy_adopted")
    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2025-12-21T00:00:00+00:00")

    result = runner.invoke(main, ["proxy", "create", "litellm-openai", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    # create_cmd without --name → orchestrator generates proxy_id
    assert data["proxy_id"] == "proxy_adopted"
    assert data["template"] == "litellm-openai"
    assert data["base_url"] == "http://localhost:8085"
    assert data["port"] == 8085


# --------------------------------------------------------------------------
# Show / Edit / Set / Delete / Create / Validate tests
# --------------------------------------------------------------------------


def _create_proxy_file(temp_env: Path, proxy_id: str, content: str) -> Path:
    """Create a proxy.yaml file for testing.

    Uses FORGE_HOME from the isolate_forge_home fixture (set by conftest.py).
    """
    forge_home = Path(os.environ["FORGE_HOME"])
    proxy_dir = forge_home / "proxies" / proxy_id
    proxy_dir.mkdir(parents=True, exist_ok=True)
    proxy_file = proxy_dir / "proxy.yaml"
    proxy_file.write_text(content)
    return proxy_file


def _create_proxy_registry_from_entries(entries: dict[str, ProxyEntry]) -> None:
    """Create a proxy registry using ProxyRegistryStore.

    Uses FORGE_HOME from the isolate_forge_home fixture (set by conftest.py).
    This mirrors production behavior exactly.
    """
    store = ProxyRegistryStore()
    registry = ProxyRegistry(proxies=entries)
    store.write(registry)


def _create_proxy_registry_raw(content: str) -> Path:
    """Create a proxy registry with raw JSON content (for testing corruption).

    Returns the path to the created registry file.
    """
    forge_home = Path(os.environ["FORGE_HOME"])
    proxies_dir = forge_home / "proxies"
    proxies_dir.mkdir(parents=True, exist_ok=True)
    registry_path = proxies_dir / "index.json"
    registry_path.write_text(content)
    return registry_path


def _write_session_index_entry(session_name: str, forge_root: Path) -> None:
    """Write a single current-shape v1 session index entry."""
    forge_home = Path(os.environ["FORGE_HOME"])
    sessions_dir = forge_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    root = str(forge_root)
    scoped_key = make_scoped_key(session_name, root)
    (sessions_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    scoped_key: {
                        "worktree_path": root,
                        "project_root": root,
                        "last_accessed_at": "2026-01-01T00:00:00Z",
                        "forge_root": root,
                        "checkout_root": root,
                        "relative_path": ".",
                    }
                },
            }
        )
    )


class TestProxyShow:
    """Tests for `forge proxy show`."""

    def test_show_displays_proxy_yaml(self, runner: CliRunner, temp_env: Path) -> None:
        """Show command displays proxy.yaml content."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
"""
        _create_proxy_file(temp_env, "my-proxy", proxy_yaml)

        result = runner.invoke(main, ["proxy", "show", "my-proxy", "--raw"])

        assert result.exit_code == 0
        assert "litellm-openai" in result.output
        assert "gpt-4o" in result.output

    def test_show_not_found_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Show command errors when proxy not found."""
        result = runner.invoke(main, ["proxy", "show", "nonexistent"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestProxyValidate:
    """Tests for `forge proxy validate`."""

    def test_validate_success(self, runner: CliRunner, temp_env: Path) -> None:
        """Validate command succeeds for valid proxy."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
"""
        _create_proxy_file(temp_env, "valid-proxy", proxy_yaml)

        result = runner.invoke(main, ["proxy", "validate", "valid-proxy"])

        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_validate_invalid_yaml(self, runner: CliRunner, temp_env: Path) -> None:
        """Validate command fails for invalid YAML."""
        _create_proxy_file(temp_env, "bad-proxy", "not: valid: yaml: [")

        result = runner.invoke(main, ["proxy", "validate", "bad-proxy"])

        assert result.exit_code != 0
        assert "failed" in result.output.lower()

    def test_validate_missing_required_field(self, runner: CliRunner, temp_env: Path) -> None:
        """Validate command fails when required field missing."""
        # Missing 'provider' field
        proxy_yaml = """\
template: litellm-openai
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "incomplete-proxy", proxy_yaml)

        result = runner.invoke(main, ["proxy", "validate", "incomplete-proxy"])

        assert result.exit_code != 0

    def test_validate_not_found_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Validate command errors when proxy not found."""
        result = runner.invoke(main, ["proxy", "validate", "nonexistent"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestProxySet:
    """Tests for `forge proxy set`."""

    def test_set_simple_field(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command updates simple field."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
default_tier: sonnet
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
"""
        proxy_file = _create_proxy_file(temp_env, "set-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "set-test", "default_tier=opus"])

        assert result.exit_code == 0
        content = proxy_file.read_text()
        assert "default_tier: opus" in content

    def test_set_nested_field_with_dot_notation(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command updates nested field with dot notation."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
tier_overrides:
  opus:
    reasoning_effort: medium
"""
        proxy_file = _create_proxy_file(temp_env, "nested-test", proxy_yaml)

        result = runner.invoke(
            main,
            [
                "proxy",
                "set",
                "nested-test",
                "tier_overrides.opus.reasoning_effort=high",
            ],
        )

        assert result.exit_code == 0
        content = proxy_file.read_text()
        assert "reasoning_effort: high" in content

    def test_set_rejects_claude_47_unsupported_static_temperature(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command validates unsupported 4.7 tier overrides before writing."""
        proxy_yaml = """\
template: litellm-anthropic
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: claude-haiku-4-5-20251001
  sonnet: claude-sonnet-4-6
  opus: claude-opus-4-7
"""
        proxy_file = _create_proxy_file(temp_env, "opus-47-test", proxy_yaml)

        result = runner.invoke(
            main,
            ["proxy", "set", "opus-47-test", "tier_overrides.opus.temperature=0.7"],
        )

        assert result.exit_code != 0
        assert "not supported" in result.output
        assert "temperature: 0.7" not in proxy_file.read_text()

    def test_set_cost_cap_coerces_to_float(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command writes cost caps as numeric YAML values."""
        from ruamel.yaml import YAML

        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
"""
        proxy_file = _create_proxy_file(temp_env, "cost-cap-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "cost-cap-test", "costs.caps.per_day=20.00"])

        assert result.exit_code == 0
        yaml = YAML()
        with open(proxy_file) as f:
            data = yaml.load(f)
        assert data["costs"]["caps"]["per_day"] == 20.0

    def test_set_cost_cap_can_reset_to_none(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command accepts none/null for optional cost caps."""
        from ruamel.yaml import YAML

        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
costs:
  caps:
    per_day: 20.0
"""
        proxy_file = _create_proxy_file(temp_env, "cost-cap-reset-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "cost-cap-reset-test", "costs.caps.per_day=none"])

        assert result.exit_code == 0
        yaml = YAML()
        with open(proxy_file) as f:
            data = yaml.load(f)
        assert data["costs"]["caps"]["per_day"] is None

    def test_set_invalid_format_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command errors on invalid format."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "format-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "format-test", "no_equals_sign"])

        assert result.exit_code != 0
        assert "expected format" in result.output.lower() or "key=value" in result.output.lower()

    def test_set_not_found_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command errors when proxy not found."""
        result = runner.invoke(main, ["proxy", "set", "nonexistent", "key=value"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_set_port_type_coercion_failure(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command errors when port value cannot be coerced to int."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "coerce-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "coerce-test", "port=not-a-number"])

        assert result.exit_code != 0
        # Should fail with conversion error
        assert "invalid" in result.output.lower() or "error" in result.output.lower()

    def test_set_temperature_type_coercion_failure(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command errors when temperature value cannot be coerced to float."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "temp-coerce-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "temp-coerce-test", "temperature=cold"])

        assert result.exit_code != 0

    def test_set_semantic_yaml_verification(self, runner: CliRunner, temp_env: Path) -> None:
        """Set command produces semantically correct YAML (not just substring match)."""
        from ruamel.yaml import YAML

        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
default_tier: sonnet
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
"""
        proxy_file = _create_proxy_file(temp_env, "semantic-test", proxy_yaml)

        result = runner.invoke(main, ["proxy", "set", "semantic-test", "default_tier=opus"])

        assert result.exit_code == 0
        # Parse YAML semantically instead of substring matching
        yaml = YAML()
        with open(proxy_file) as f:
            data = yaml.load(f)
        assert data["default_tier"] == "opus"
        # Verify other fields preserved
        assert data["template"] == "litellm-openai"
        assert data["port"] == 8085


class TestProxyDelete:
    """Tests for `forge proxy delete`."""

    def test_delete_removes_proxy_and_registry(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete removes proxy file and registry entry."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        proxy_file = _create_proxy_file(temp_env, "delete-me", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "delete-me": ProxyEntry(
                    proxy_id="delete-me",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        result = runner.invoke(main, ["proxy", "delete", "delete-me", "--force"])

        assert result.exit_code == 0
        assert not proxy_file.exists()
        # Verify registry updated
        store = ProxyRegistryStore()
        registry = store.read()
        assert "delete-me" not in registry.proxies

    def test_delete_prompts_without_force(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete prompts for confirmation when --force not provided."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        proxy_file = _create_proxy_file(temp_env, "prompt-test", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "prompt-test": ProxyEntry(
                    proxy_id="prompt-test",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # Invoke without --force and with 'n' input to decline
        result = runner.invoke(main, ["proxy", "delete", "prompt-test"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        # Proxy should still exist
        assert proxy_file.exists()

    def test_delete_confirms_and_deletes(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete proceeds when user confirms."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        proxy_file = _create_proxy_file(temp_env, "confirm-test", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "confirm-test": ProxyEntry(
                    proxy_id="confirm-test",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # Invoke without --force and with 'y' input to confirm
        result = runner.invoke(main, ["proxy", "delete", "confirm-test"], input="y\n")

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert not proxy_file.exists()

    def test_delete_not_found_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete errors when proxy not in registry."""
        result = runner.invoke(main, ["proxy", "delete", "nonexistent", "--force"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_delete_aborts_before_config_removal_when_registry_update_fails(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete must not remove config files if the registry transaction fails."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        proxy_file = _create_proxy_file(temp_env, "registry-fail", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "registry-fail": ProxyEntry(
                    proxy_id="registry-fail",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="configured",
                )
            }
        )

        def fail_update(self: ProxyRegistryStore, *, timeout_s: float, mutate: object) -> None:
            raise RuntimeError("lock unavailable")

        monkeypatch.setattr(ProxyRegistryStore, "update", fail_update)

        result = runner.invoke(main, ["proxy", "delete", "registry-fail", "--yes"])

        assert result.exit_code != 0
        assert "could not update registry" in result.output.lower()
        assert proxy_file.exists()

    def test_delete_restores_registry_entry_when_directory_delete_fails(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If config deletion fails after registry removal, the registry entry is restored."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        proxy_file = _create_proxy_file(temp_env, "dir-fail", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "dir-fail": ProxyEntry(
                    proxy_id="dir-fail",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="configured",
                )
            }
        )

        def fail_rmtree(path: Path) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr("forge.cli.proxy.shutil.rmtree", fail_rmtree)

        result = runner.invoke(main, ["proxy", "delete", "dir-fail", "--yes"])

        assert result.exit_code != 0
        assert "could not delete proxy directory" in result.output.lower()
        assert proxy_file.exists()

        registry = ProxyRegistryStore().read()
        assert "dir-fail" in registry.proxies

    def test_delete_no_warning_for_intent_only_sessions(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete does NOT warn for sessions that only share base_url via intent.

        Sessions reference template+port, not a specific proxy_id. Multiple proxy
        entries can share a port, so intent-level base_url matching would produce
        false positives.
        """
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "warn-test", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "warn-test": ProxyEntry(
                    proxy_id="warn-test",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # Session has intent.proxy with same base_url but no confirmed.started_with_proxy
        _write_session_index_entry("my-session", temp_env)
        manifest_dir = temp_env / ".forge" / "sessions" / "my-session"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "forge.session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "my-session",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_accessed_at": "2026-01-01T00:00:00Z",
                    "intent": {
                        "proxy": {
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                        }
                    },
                    "overrides": {},
                    "confirmed": {},
                }
            )
        )

        result = runner.invoke(main, ["proxy", "delete", "warn-test"], input="y\n")

        assert result.exit_code == 0
        assert "Warning" not in result.output
        assert "Deleted" in result.output

    def test_delete_force_skips_session_warning(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete with --force skips session warning."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "force-test", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "force-test": ProxyEntry(
                    proxy_id="force-test",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # Create session referencing proxy
        _write_session_index_entry("my-session", temp_env)
        manifest_dir = temp_env / ".forge" / "sessions" / "my-session"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "forge.session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "my-session",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_accessed_at": "2026-01-01T00:00:00Z",
                    "intent": {
                        "proxy": {
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                        }
                    },
                    "overrides": {},
                    "confirmed": {},
                }
            )
        )

        result = runner.invoke(main, ["proxy", "delete", "force-test", "--force"])

        assert result.exit_code == 0
        assert "Warning" not in result.output
        assert "Deleted" in result.output

    def test_delete_handles_missing_session_index(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete proceeds cleanly when no session index exists (fail-open)."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "no-index-test", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "no-index-test": ProxyEntry(
                    proxy_id="no-index-test",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # No session index file at all
        result = runner.invoke(main, ["proxy", "delete", "no-index-test", "--force"])

        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_delete_warns_on_confirmed_started_with_proxy(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete warns when session has confirmed.started_with_proxy matching proxy_id."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "confirmed-test", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "confirmed-test": ProxyEntry(
                    proxy_id="confirmed-test",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # Session has no intent.proxy but has confirmed.started_with_proxy
        _write_session_index_entry("confirmed-session", temp_env)
        manifest_dir = temp_env / ".forge" / "sessions" / "confirmed-session"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "forge.session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "confirmed-session",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_accessed_at": "2026-01-01T00:00:00Z",
                    "intent": {},
                    "overrides": {},
                    "confirmed": {
                        "started_with_proxy": {
                            "base_url": "http://localhost:8085",
                            "proxy_id": "confirmed-test",
                            "template": "litellm-openai",
                            "port": 8085,
                        }
                    },
                }
            )
        )

        result = runner.invoke(main, ["proxy", "delete", "confirmed-test"], input="y\n")

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "confirmed-session" in result.output

    def test_delete_skips_pid_kill_when_other_entries_share_base_url(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete keeps server alive when other proxy entries share the same base_url.

        Regression: QA-019. Multiple proxy entries from the same template share a
        port. Deleting one alias should not kill the shared server process.
        """
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "alias-a", proxy_yaml)
        _create_proxy_file(temp_env, "alias-b", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "alias-a": ProxyEntry(
                    proxy_id="alias-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
                "alias-b": ProxyEntry(
                    proxy_id="alias-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
            }
        )

        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "delete", "alias-a", "--force"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert killed_pids == [], "Should NOT kill server when other entries share the port"
        assert "kept alive" in result.output.lower()
        assert "Keeping shared server references:" in result.output
        assert "alias-b" in result.output

    def test_delete_confirmation_lists_related_shared_port_proxies(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete confirmation lists other proxy entries on the same port.

        Regression: QA-020. Users need to see which aliases will continue to own the
        shared server before deleting one entry.
        """
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "alias-a", proxy_yaml)
        _create_proxy_file(temp_env, "alias-b", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "alias-a": ProxyEntry(
                    proxy_id="alias-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="configured",
                ),
                "alias-b": ProxyEntry(
                    proxy_id="alias-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="configured",
                ),
            }
        )

        result = runner.invoke(main, ["proxy", "delete", "alias-a"], input="n\n")

        assert result.exit_code == 0
        assert "Related proxies on the same port" in result.output
        assert "alias-b" in result.output
        assert "Cancelled" in result.output

    def test_delete_last_alias_lists_related_sessions_on_port(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete confirmation lists sessions affected when removing the last alias.

        Regression: QA-020. The warning should enumerate the related sessions, not
        just say a warning exists.
        """
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "alias-b", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "alias-b": ProxyEntry(
                    proxy_id="alias-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        _write_session_index_entry("orphan-session", temp_env)
        manifest_dir = temp_env / ".forge" / "sessions" / "orphan-session"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "forge.session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "orphan-session",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_accessed_at": "2026-01-01T00:00:00Z",
                    "intent": {},
                    "overrides": {},
                    "confirmed": {
                        "started_with_proxy": {
                            "base_url": "http://localhost:8085",
                            "proxy_id": "alias-a",
                            "template": "litellm-openai",
                            "port": 8085,
                        }
                    },
                }
            )
        )

        result = runner.invoke(main, ["proxy", "delete", "alias-b"], input="n\n")

        assert result.exit_code == 0
        assert "Related sessions on http://localhost:8085:" in result.output
        assert "orphan-session" in result.output
        assert "Cancelled" in result.output
        assert "Delete sessions first" in result.output

    def test_delete_kills_pid_when_last_entry_for_base_url(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete kills server when this is the last registry entry for this base_url."""
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        _create_proxy_file(temp_env, "only-entry", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "only-entry": ProxyEntry(
                    proxy_id="only-entry",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
            }
        )

        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "delete", "only-entry", "--force"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert killed_pids == [12345], "Should kill server when last entry for this port"

    def test_delete_last_alias_warns_sessions_by_base_url(self, runner: CliRunner, temp_env: Path) -> None:
        """Deleting the last alias for a port warns sessions bound to ANY alias on that port.

        Regression: QA-019 review finding. Session started with alias-a; alias-a was
        deleted earlier while alias-b kept the server alive. Now deleting alias-b (last
        entry) should warn about the session bound to alias-a, since the server will die.
        """
        proxy_yaml = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""
        # Only alias-b remains (alias-a was deleted earlier)
        _create_proxy_file(temp_env, "alias-b", proxy_yaml)
        _create_proxy_registry_from_entries(
            {
                "alias-b": ProxyEntry(
                    proxy_id="alias-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        # Session was started through alias-a (now deleted) but still uses port 8085
        _write_session_index_entry("orphan-session", temp_env)
        manifest_dir = temp_env / ".forge" / "sessions" / "orphan-session"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "forge.session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "orphan-session",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_accessed_at": "2026-01-01T00:00:00Z",
                    "intent": {},
                    "overrides": {},
                    "confirmed": {
                        "started_with_proxy": {
                            "base_url": "http://localhost:8085",
                            "proxy_id": "alias-a",
                            "template": "litellm-openai",
                            "port": 8085,
                        }
                    },
                }
            )
        )

        result = runner.invoke(main, ["proxy", "delete", "alias-b"], input="y\n")

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert (
            "orphan-session" in result.output
        ), "Should warn about sessions on the same port when deleting the last alias"
        assert "Deleted" in result.output


class TestProxyDeleteMulti:
    """Tests for multi-proxy delete and --all."""

    _PROXY_YAML = """\
template: litellm-openai
provider: litellm
proxy_endpoint: http://localhost:{port}
port: {port}
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-4o-mini
"""

    def _setup_proxies(self, temp_env: Path, *names: str, base_port: int = 9001) -> None:
        entries: dict[str, ProxyEntry] = {}
        for i, name in enumerate(names):
            port = base_port + i
            _create_proxy_file(temp_env, name, self._PROXY_YAML.format(port=port))
            entries[name] = ProxyEntry(
                proxy_id=name,
                template="litellm-openai",
                base_url=f"http://localhost:{port}",
                port=port,
                pid=None,
                status="healthy",
            )
        _create_proxy_registry_from_entries(entries)

    def test_delete_multiple_proxies(self, runner: CliRunner, temp_env: Path) -> None:
        """Should delete multiple proxies in one command."""
        self._setup_proxies(temp_env, "p1", "p2", "p3")

        result = runner.invoke(main, ["proxy", "delete", "p1", "p2", "p3", "--force"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert "p1" in result.output
        assert "p2" in result.output
        assert "p3" in result.output
        assert "3 deleted" in result.output

        store = ProxyRegistryStore()
        registry = store.read()
        assert len(registry.proxies) == 0

    def test_delete_all_proxies(self, runner: CliRunner, temp_env: Path) -> None:
        """--all should delete every proxy."""
        self._setup_proxies(temp_env, "all-1", "all-2")

        result = runner.invoke(main, ["proxy", "delete", "--all", "--force"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert "2 deleted" in result.output

        store = ProxyRegistryStore()
        registry = store.read()
        assert len(registry.proxies) == 0

    def test_delete_all_with_ids_errors(self, runner: CliRunner, temp_env: Path) -> None:
        """--all with explicit IDs should error."""
        result = runner.invoke(main, ["proxy", "delete", "--all", "some-id", "--force"])

        assert result.exit_code == 1
        assert "Cannot combine --all" in result.output

    def test_delete_no_args_errors(self, runner: CliRunner, temp_env: Path) -> None:
        """No IDs and no --all should error."""
        result = runner.invoke(main, ["proxy", "delete"])

        assert result.exit_code == 1
        assert "Provide proxy ID(s) or use --all" in result.output

    def test_delete_all_empty_is_noop(self, runner: CliRunner, temp_env: Path) -> None:
        """--all with no proxies should show a message and succeed."""
        result = runner.invoke(main, ["proxy", "delete", "--all", "--force"])

        assert result.exit_code == 0
        assert "No proxies to delete" in result.output

    def test_delete_partial_failure(self, runner: CliRunner, temp_env: Path) -> None:
        """Should continue deleting after a failure and report summary."""
        self._setup_proxies(temp_env, "real-proxy")

        result = runner.invoke(main, ["proxy", "delete", "real-proxy", "ghost", "--force"])

        assert result.exit_code == 1
        assert "Deleted" in result.output
        assert "real-proxy" in result.output
        assert "not found" in result.output
        assert "1 deleted" in result.output
        assert "1 failed" in result.output

    def test_delete_all_prompts_without_force(self, runner: CliRunner, temp_env: Path) -> None:
        """--all without --force should prompt with proxy list."""
        self._setup_proxies(temp_env, "pr-1", "pr-2")

        result = runner.invoke(main, ["proxy", "delete", "--all"], input="n\n")

        assert "all 2 proxy(ies)" in result.output
        assert "Cancelled" in result.output


class TestProxyCreateNoStart:
    """Tests for `forge proxy create --no-start` (create config only).

    These tests use the real template files from src/forge/config/defaults/templates/
    since the create command validates template existence.
    """

    def test_create_creates_proxy_file_and_registers(self, runner: CliRunner, temp_env: Path) -> None:
        """Create --no-start makes a new proxy file AND registers it in index.json."""
        # Use a real template that exists in the codebase
        result = runner.invoke(
            main,
            ["proxy", "create", "litellm-openai", "--name", "my-proxy", "--no-start"],
        )

        assert result.exit_code == 0, result.output

        # Verify file created (uses FORGE_HOME from isolate_forge_home fixture)
        forge_home = Path(os.environ["FORGE_HOME"])
        proxy_file = forge_home / "proxies" / "my-proxy" / "proxy.yaml"
        assert proxy_file.exists()
        content = proxy_file.read_text()
        assert "litellm-openai" in content

        # CRITICAL: Verify proxy is registered so it appears in `forge proxy list`
        store = ProxyRegistryStore()
        registry = store.read()
        assert "my-proxy" in registry.proxies, "Proxy must be registered in index.json"
        entry = registry.proxies["my-proxy"]
        assert entry.template == "litellm-openai"
        assert entry.status == "configured"
        assert entry.pid is None  # Not started

    def test_create_already_exists_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Create --no-start errors when proxy already exists."""
        # Create existing proxy
        _create_proxy_file(temp_env, "existing", "template: something\n")

        result = runner.invoke(
            main,
            ["proxy", "create", "litellm-openai", "--name", "existing", "--no-start"],
        )

        assert result.exit_code != 0
        assert "exists" in result.output.lower()

    def test_create_invalid_template_error(self, runner: CliRunner, temp_env: Path) -> None:
        """Create errors for unknown template."""
        result = runner.invoke(
            main,
            [
                "proxy",
                "create",
                "nonexistent-template",
                "--name",
                "new-proxy",
                "--no-start",
            ],
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_create_with_custom_port(self, runner: CliRunner, temp_env: Path) -> None:
        """Create --no-start uses custom port when specified."""
        result = runner.invoke(
            main,
            [
                "proxy",
                "create",
                "litellm-openai",
                "--name",
                "custom-port",
                "--port",
                "9999",
                "--no-start",
            ],
        )

        assert result.exit_code == 0, result.output

        # Verify port in file (uses FORGE_HOME from isolate_forge_home fixture)
        forge_home = Path(os.environ["FORGE_HOME"])
        proxy_file = forge_home / "proxies" / "custom-port" / "proxy.yaml"
        content = proxy_file.read_text()
        assert "9999" in content

    def test_create_rolls_back_proxy_directory_when_registry_update_fails(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Create --no-start must not leave an unregistered proxy directory behind."""

        def fail_update(self: ProxyRegistryStore, *, timeout_s: float, mutate: object) -> None:
            raise RuntimeError("lock unavailable")

        monkeypatch.setattr(ProxyRegistryStore, "update", fail_update)

        result = runner.invoke(
            main,
            ["proxy", "create", "litellm-openai", "--name", "rollback-proxy", "--no-start"],
        )

        assert result.exit_code != 0
        assert "could not register proxy" in result.output.lower()

        forge_home = Path(os.environ["FORGE_HOME"])
        proxy_dir = forge_home / "proxies" / "rollback-proxy"
        assert not proxy_dir.exists()


class TestProxyRegistryCorruption:
    """Tests for corrupted proxy registry handling.

    These tests verify that CLI commands handle corrupt registry files gracefully.
    The ProxyRegistryStore.read() method raises ProxyRegistryCorruptedError for:
    - Invalid JSON
    - Missing version field
    - Wrong version number
    """

    def test_list_with_invalid_json_registry(self, runner: CliRunner, temp_env: Path) -> None:
        """List command handles invalid JSON gracefully."""
        _create_proxy_registry_raw("not valid json {{{")

        result = runner.invoke(main, ["proxy", "list"])

        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_list_with_missing_version_registry(self, runner: CliRunner, temp_env: Path) -> None:
        """List command handles missing version field."""
        _create_proxy_registry_raw(json.dumps({"proxies": {}}))

        result = runner.invoke(main, ["proxy", "list"])

        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_list_with_wrong_version_registry(self, runner: CliRunner, temp_env: Path) -> None:
        """List command handles wrong version number."""
        _create_proxy_registry_raw(json.dumps({"version": 999, "proxies": {}}))

        result = runner.invoke(main, ["proxy", "list"])

        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_create_with_invalid_json_registry(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Create command (default start) handles invalid JSON registry."""
        _create_proxy_registry_raw("invalid json")

        result = runner.invoke(main, ["proxy", "create", "litellm-openai"])

        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_delete_with_invalid_json_registry(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete command handles invalid JSON registry."""
        _create_proxy_registry_raw("invalid json")

        result = runner.invoke(main, ["proxy", "delete", "some-proxy", "--force"])

        assert result.exit_code != 0
        assert "error" in result.output.lower()


# --------------------------------------------------------------------------
# Metrics tests
# --------------------------------------------------------------------------

_SAMPLE_METRICS = {
    "started_at": "2026-03-23T00:00:00+00:00",
    "uptime_seconds": 3600.0,
    "total_requests": 42,
    "total_streaming": 30,
    "total_failures": 2,
    "tokens": {
        "input": 100000,
        "output": 25000,
        "cached": 60000,
        "failed_input": 500,
        "failed_output": 100,
    },
    "cache_hit_rate": 60.0,
    "by_tier": {
        "sonnet": {
            "requests": 35,
            "input_tokens": 80000,
            "output_tokens": 20000,
            "cached_tokens": 50000,
            "avg_latency_ms": 842.0,
        },
        "opus": {
            "requests": 7,
            "input_tokens": 20000,
            "output_tokens": 5000,
            "cached_tokens": 10000,
            "avg_latency_ms": 2100.0,
        },
    },
    "by_model": {
        "openai/gpt-5.5": {"requests": 35, "input_tokens": 80000, "output_tokens": 20000, "cached_tokens": 50000},
        "openai/o3": {"requests": 7, "input_tokens": 20000, "output_tokens": 5000, "cached_tokens": 10000},
    },
    "failures_by_type": {"tool_call_error": 2},
    "last_request_at": "2026-03-23T01:00:00+00:00",
}


class TestProxyMetrics:
    """Tests for `forge proxy metrics`."""

    def _setup_registry_with_proxy(self, proxy_id: str = "test-proxy", port: int = 8085) -> None:
        _create_proxy_registry_from_entries(
            {
                proxy_id: ProxyEntry(
                    proxy_id=proxy_id,
                    template="litellm-openai",
                    base_url=f"http://localhost:{port}",
                    port=port,
                    pid=12345,
                    status="healthy",
                )
            }
        )

    def test_metrics_displays_for_single_proxy(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup_registry_with_proxy()
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info",
            lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="litellm-openai"),
        )

        result = runner.invoke(main, ["proxy", "metrics", "test-proxy"])

        assert result.exit_code == 0
        assert "test-proxy" in result.output
        assert "42" in result.output  # total_requests
        assert "sonnet" in result.output
        assert "opus" in result.output

    def test_metrics_json_output(self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._setup_registry_with_proxy()
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info",
            lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="litellm-openai"),
        )

        result = runner.invoke(main, ["proxy", "metrics", "test-proxy", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_requests"] == 42
        assert data["tokens"]["cached"] == 60000

    def test_metrics_all_json_is_valid_single_object(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--all --json must emit a single valid JSON object."""
        _create_proxy_registry_from_entries(
            {
                "proxy-a": ProxyEntry(proxy_id="proxy-a", template="t", base_url="http://localhost:8085", port=8085),
                "proxy-b": ProxyEntry(proxy_id="proxy-b", template="t", base_url="http://localhost:8086", port=8086),
            }
        )

        # proxy-a reachable, proxy-b not
        def _mock_fetch(base_url: str):
            if "8085" in base_url:
                return _ProxyInfo(metrics=_SAMPLE_METRICS, template="t")
            return None

        monkeypatch.setattr("forge.cli.proxy._fetch_proxy_info", _mock_fetch)

        result = runner.invoke(main, ["proxy", "metrics", "--all", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)  # Must parse as single JSON
        assert "proxy-a" in data
        assert data["proxy-a"]["total_requests"] == 42
        assert data["proxy-b"] is None  # unreachable

    def test_metrics_proxy_not_found(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "metrics", "nonexistent"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_metrics_proxy_unreachable(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup_registry_with_proxy()
        monkeypatch.setattr("forge.cli.proxy._fetch_proxy_info", lambda _: None)

        result = runner.invoke(main, ["proxy", "metrics", "test-proxy"])

        assert result.exit_code != 0
        assert "not reachable" in result.output.lower()

    def test_metrics_no_proxies_registered(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "metrics"])

        assert result.exit_code == 0
        assert "no proxies" in result.output.lower()

    def test_metrics_default_single_proxy(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No proxy_id argument defaults to the single registered proxy."""
        self._setup_registry_with_proxy()
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info",
            lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="litellm-openai"),
        )

        result = runner.invoke(main, ["proxy", "metrics"])

        assert result.exit_code == 0
        assert "test-proxy" in result.output

    def test_metrics_multiple_proxies_requires_id(self, runner: CliRunner, temp_env: Path) -> None:
        """Multiple proxies without proxy_id or --all is an error."""
        _create_proxy_registry_from_entries(
            {
                "p1": ProxyEntry(proxy_id="p1", template="t", base_url="http://localhost:8085", port=8085),
                "p2": ProxyEntry(proxy_id="p2", template="t", base_url="http://localhost:8086", port=8086),
            }
        )

        result = runner.invoke(main, ["proxy", "metrics"])

        assert result.exit_code != 0
        assert "--all" in result.output

    def test_metrics_corrupted_registry(self, runner: CliRunner, temp_env: Path) -> None:
        _create_proxy_registry_raw("not valid json")

        result = runner.invoke(main, ["proxy", "metrics", "some-proxy"])

        assert result.exit_code != 0
        assert "corrupted" in result.output.lower()

    def test_metrics_shows_latency_per_tier(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup_registry_with_proxy()
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info",
            lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="litellm-openai"),
        )

        result = runner.invoke(main, ["proxy", "metrics", "test-proxy"])

        assert result.exit_code == 0
        assert "842ms" in result.output
        assert "2,100ms" in result.output

    def test_metrics_shows_failure_types(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup_registry_with_proxy()
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info",
            lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="litellm-openai"),
        )

        result = runner.invoke(main, ["proxy", "metrics", "test-proxy"])

        assert result.exit_code == 0
        assert "tool_call_error" in result.output

    def test_metrics_shows_template_in_identity(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup_registry_with_proxy()
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info",
            lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="litellm-openai"),
        )

        result = runner.invoke(main, ["proxy", "metrics", "test-proxy"])

        assert result.exit_code == 0
        assert "litellm-openai" in result.output

    def test_metrics_all_shows_separators(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--all with multiple proxies shows separator lines between them."""
        _create_proxy_registry_from_entries(
            {
                "proxy-a": ProxyEntry(proxy_id="proxy-a", template="t", base_url="http://localhost:8085", port=8085),
                "proxy-b": ProxyEntry(proxy_id="proxy-b", template="t", base_url="http://localhost:8086", port=8086),
            }
        )
        monkeypatch.setattr(
            "forge.cli.proxy._fetch_proxy_info", lambda _: _ProxyInfo(metrics=_SAMPLE_METRICS, template="t")
        )

        result = runner.invoke(main, ["proxy", "metrics", "--all"])

        assert result.exit_code == 0
        assert "proxy-a" in result.output
        assert "proxy-b" in result.output
        assert "----" in result.output  # separator between proxies


# -----------------------------------------------------------------------------
# Adopted proxy lifecycle (kill-by-port, health guard, --no-kill)
# -----------------------------------------------------------------------------


class TestDeleteAdoptedProxy:
    """Tests for adopted proxy (pid=None) delete behavior.

    Smart-pointer policy: Forge never kills adopted proxies (not started by Forge).
    Delete only removes the registry entry.
    """

    def test_delete_adopted_leaves_process_alone(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete adopted proxy without --force leaves process alive."""
        _create_proxy_registry_from_entries(
            {
                "adopted": ProxyEntry(
                    proxy_id="adopted",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )
        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: killed_pids.append(pid))
        monkeypatch.setattr("forge.cli.proxy.click.confirm", lambda *a, **kw: True)

        result = runner.invoke(main, ["proxy", "delete", "adopted"])

        assert result.exit_code == 0
        assert killed_pids == []  # NOT killed — adopted proxy
        assert "not started by Forge" in result.output

    def test_delete_adopted_removes_from_registry(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete adopted proxy still removes it from the registry."""
        _create_proxy_registry_from_entries(
            {
                "adopted": ProxyEntry(
                    proxy_id="adopted",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )
        # --force + adopted → kill_adopted=True → must mock process discovery
        # to avoid killing a real proxy on port 8085
        monkeypatch.setattr("forge.cli.proxy.find_pid_by_port", lambda port: None)

        result = runner.invoke(main, ["proxy", "delete", "adopted", "--force"])

        assert result.exit_code == 0
        store = ProxyRegistryStore()
        registry = store.read()
        assert "adopted" not in registry.proxies

    def test_delete_adopted_skips_foreign_proxy_identity(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force delete must not kill a different healthy proxy on the same port.

        This protects local developer proxies while tests run with an isolated
        FORGE_HOME. Port discovery may find a real listener, but the health
        check must also match the expected proxy_id before we signal it.
        """
        _create_proxy_registry_from_entries(
            {
                "adopted": ProxyEntry(
                    proxy_id="adopted",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.find_pid_by_port", lambda port: 4242)

        def _health(
            *,
            base_url: str,
            expected_template: str,
            timeout_s: float,
            expected_proxy_id: str | None = None,
            require_unregistered: bool = False,
        ) -> bool:
            assert base_url == "http://localhost:8085"
            assert expected_template == "litellm-openai"
            assert expected_proxy_id == "adopted"
            assert require_unregistered is False
            return False

        monkeypatch.setattr("forge.cli.proxy.check_proxy_health", _health)
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "delete", "adopted", "--yes", "--kill-adopted"])

        assert result.exit_code == 0
        assert killed_pids == []
        assert "doesn't match proxy 'adopted'" in result.output

    def test_delete_no_kill_flag_skips_termination(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-kill removes registry entry without killing."""
        _create_proxy_registry_from_entries(
            {
                "proxy-a": ProxyEntry(
                    proxy_id="proxy-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                )
            }
        )
        monkeypatch.setattr("forge.cli.proxy.is_pid_alive", lambda pid: True)
        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "delete", "proxy-a", "--force", "--no-kill"])

        assert result.exit_code == 0
        assert killed_pids == []
        store = ProxyRegistryStore()
        registry = store.read()
        assert "proxy-a" not in registry.proxies

    def test_delete_adopted_shared_port_keeps_alive(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adopted proxy sharing port with another proxy: don't kill."""
        _create_proxy_registry_from_entries(
            {
                "adopted-a": ProxyEntry(
                    proxy_id="adopted-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                ),
                "adopted-b": ProxyEntry(
                    proxy_id="adopted-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                ),
            }
        )
        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "delete", "adopted-a", "--force"])

        assert result.exit_code == 0
        assert killed_pids == []
        assert "Server kept alive" in result.output


class TestStopAdoptedProxy:
    """Tests for stopping adopted proxies and shared-port policy.

    Smart-pointer policy: stop never kills adopted proxies.
    """

    def test_stop_adopted_leaves_process_alone(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop on adopted proxy does NOT kill — process not started by Forge."""
        _create_proxy_registry_from_entries(
            {
                "adopted": ProxyEntry(
                    proxy_id="adopted",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )
        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "stop", "adopted"])

        assert result.exit_code == 0
        assert killed_pids == []
        assert "not started by Forge" in result.output
        assert "Detached" in result.output

        # Adopted proxy removed from registry (not falsely marked "stopped")
        store = ProxyRegistryStore()
        registry = store.read()
        assert "adopted" not in registry.proxies

    def test_stop_managed_dead_pid_clears_registry(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_proxy_registry_from_entries(
            {
                "managed-dead": ProxyEntry(
                    proxy_id="managed-dead",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=4242,
                    status="healthy",
                )
            }
        )
        monkeypatch.setattr("forge.cli.proxy.is_pid_alive", lambda pid: False)

        result = runner.invoke(main, ["proxy", "stop", "managed-dead"])

        assert result.exit_code == 0
        assert "is not running" in result.output
        assert "Cleared" in result.output

        store = ProxyRegistryStore()
        registry = store.read()
        assert registry.proxies["managed-dead"].status == "stopped"
        assert registry.proxies["managed-dead"].pid is None

    def test_stop_shared_port_refuses_without_force(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_proxy_registry_from_entries(
            {
                "proxy-a": ProxyEntry(
                    proxy_id="proxy-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
                "proxy-b": ProxyEntry(
                    proxy_id="proxy-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
            }
        )

        result = runner.invoke(main, ["proxy", "stop", "proxy-a"])

        assert result.exit_code != 0
        assert "Cannot stop" in result.output
        assert "proxy-b" in result.output

    def test_stop_shared_port_with_force_proceeds(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_proxy_registry_from_entries(
            {
                "proxy-a": ProxyEntry(
                    proxy_id="proxy-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
                "proxy-b": ProxyEntry(
                    proxy_id="proxy-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
            }
        )
        monkeypatch.setattr("forge.cli.proxy.is_pid_alive", lambda pid: True)
        killed_pids: list[int] = []
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: killed_pids.append(pid))

        result = runner.invoke(main, ["proxy", "stop", "proxy-a", "--force"])

        assert result.exit_code == 0
        assert 12345 in killed_pids


class TestProxyListSource:
    """Tests for SOURCE column in proxy list."""

    def test_list_shows_adopted_source(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_proxy_registry_from_entries(
            {
                "adopted-proxy": ProxyEntry(
                    proxy_id="adopted-proxy",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                )
            }
        )

        result = runner.invoke(main, ["proxy", "list"])

        assert result.exit_code == 0
        assert "SOURCE" in result.output
        assert "adopted" in result.output

    def test_list_shows_managed_source(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_proxy_registry_from_entries(
            {
                "managed-proxy": ProxyEntry(
                    proxy_id="managed-proxy",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=os.getpid(),
                    status="healthy",
                )
            }
        )

        result = runner.invoke(main, ["proxy", "list"])

        assert result.exit_code == 0
        assert "managed" in result.output


class TestStopForceUpdatesSiblings:
    """Issue 1: stop --force must update all sibling aliases on shared port."""

    def test_stop_force_marks_siblings_stopped(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_proxy_registry_from_entries(
            {
                "alias-a": ProxyEntry(
                    proxy_id="alias-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                ),
                "alias-b": ProxyEntry(
                    proxy_id="alias-b",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=None,
                    status="healthy",
                ),
            }
        )
        monkeypatch.setattr("forge.cli.proxy.find_pid_by_port", lambda port: 42)
        monkeypatch.setattr("forge.cli.proxy.check_proxy_health", lambda **_: True)
        monkeypatch.setattr("forge.cli.proxy.os.kill", lambda pid, sig: None)

        result = runner.invoke(main, ["proxy", "stop", "alias-a", "--force", "--kill-adopted"])

        assert result.exit_code == 0
        store = ProxyRegistryStore()
        registry = store.read()
        assert registry.proxies["alias-a"].status == "stopped"
        assert registry.proxies["alias-b"].status == "stopped"
        assert "Also marked as stopped" in result.output
        assert "alias-b" in result.output


class TestSharedPortByPortNotUrl:
    """Issue 2: shared-port policy must compare by port, not base_url string."""

    def test_stop_detects_shared_port_across_host_spellings(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """localhost:8085 and 127.0.0.1:8085 should be treated as shared."""
        _create_proxy_registry_from_entries(
            {
                "proxy-a": ProxyEntry(
                    proxy_id="proxy-a",
                    template="litellm-openai",
                    base_url="http://localhost:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
                "proxy-b": ProxyEntry(
                    proxy_id="proxy-b",
                    template="litellm-openai",
                    base_url="http://127.0.0.1:8085",
                    port=8085,
                    pid=12345,
                    status="healthy",
                ),
            }
        )

        result = runner.invoke(main, ["proxy", "stop", "proxy-a"])

        assert result.exit_code != 0
        assert "Cannot stop" in result.output
        assert "proxy-b" in result.output


# =============================================================================
# Template subgroup tests
# =============================================================================


class TestProxyTemplate:
    """Tests for forge proxy template subgroup."""

    def test_template_list_shows_shipped(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "list"])
        assert result.exit_code == 0
        assert "litellm-openai" in result.output
        assert "built-in" in result.output

    def test_template_list_shows_user_template(self, runner: CliRunner, temp_env: Path) -> None:
        forge_home = Path(os.environ["FORGE_HOME"])
        tpl_dir = forge_home / "templates"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "litellm-openai.yaml").write_text("# user override\nproxy:\n  default_port: 9999\n")

        result = runner.invoke(main, ["proxy", "template", "list"])
        assert result.exit_code == 0
        assert "customized" in result.output

    def test_template_show_displays_yaml(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "show", "litellm-openai"])
        assert result.exit_code == 0
        assert "litellm-openai" in result.output
        assert "built-in" in result.output

    def test_template_show_not_found(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "show", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_template_show_raw(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "show", "litellm-openai", "--raw"])
        assert result.exit_code == 0
        assert "proxy:" in result.output

    def test_template_edit_copies_shipped(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edit creates user copy from shipped template."""
        monkeypatch.setenv("EDITOR", "true")

        result = runner.invoke(main, ["proxy", "template", "edit", "litellm-openai"])
        assert result.exit_code == 0

        forge_home = Path(os.environ["FORGE_HOME"])
        user_path = forge_home / "templates" / "litellm-openai.yaml"
        assert user_path.is_file()
        assert "Updated" in result.output

    def test_template_edit_invalid_yaml(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edit rejects invalid YAML and does not leave a user copy behind."""
        import tempfile

        script = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False)
        script.write('#!/bin/sh\necho "{{{{not yaml" > "$1"\n')
        script.close()
        os.chmod(script.name, 0o755)
        monkeypatch.setenv("EDITOR", script.name)

        result = runner.invoke(main, ["proxy", "template", "edit", "litellm-openai"])
        assert result.exit_code != 0
        assert "Invalid YAML" in result.output or "must be a YAML mapping" in result.output

        # No user copy should be left behind on failure
        forge_home = Path(os.environ["FORGE_HOME"])
        user_path = forge_home / "templates" / "litellm-openai.yaml"
        assert not user_path.is_file()

    def test_template_edit_bad_editor_no_leftover(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failed first edit (bad editor) does not leave a user copy behind."""
        monkeypatch.setenv("EDITOR", "definitely-not-an-editor")

        result = runner.invoke(main, ["proxy", "template", "edit", "litellm-openai"])
        assert result.exit_code != 0

        forge_home = Path(os.environ["FORGE_HOME"])
        user_path = forge_home / "templates" / "litellm-openai.yaml"
        assert not user_path.is_file()

    def test_template_edit_bad_name(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "edit", "../etc/passwd"])
        assert result.exit_code != 0
        assert "Invalid template name" in result.output

    def test_template_edit_nonexistent_shipped(self, runner: CliRunner, temp_env: Path) -> None:
        """Edit requires a shipped template to seed from."""
        result = runner.invoke(main, ["proxy", "template", "edit", "nonexistent-xyz"])
        assert result.exit_code != 0
        assert "No built-in template" in result.output

    def test_template_reset_removes_user_copy(self, runner: CliRunner, temp_env: Path) -> None:
        forge_home = Path(os.environ["FORGE_HOME"])
        tpl_dir = forge_home / "templates"
        tpl_dir.mkdir(parents=True)
        user_path = tpl_dir / "litellm-openai.yaml"
        user_path.write_text("# user override\n")

        result = runner.invoke(main, ["proxy", "template", "reset", "litellm-openai", "--force"])
        assert result.exit_code == 0
        assert "Reset" in result.output
        assert not user_path.is_file()

    def test_template_reset_noop_when_no_override(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "reset", "litellm-openai"])
        assert result.exit_code == 0
        assert "Already using built-in defaults" in result.output

    def test_bare_template_shows_help(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template"])
        assert result.exit_code == 2
        assert "Manage proxy templates" in result.output

    def test_proxy_list_empty_shows_template_tip(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "list"])
        assert result.exit_code == 0
        assert "No proxies found" in result.output
        assert "forge proxy template list" in result.output

    def test_template_show_rejects_path_traversal(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "show", "../outside"])
        assert result.exit_code != 0
        assert "Invalid template name" in result.output

    def test_template_reset_rejects_path_traversal(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "template", "reset", "../outside", "--force"])
        assert result.exit_code != 0
        assert "Invalid template name" in result.output

    def test_proxy_create_rejects_invalid_template_name(self, runner: CliRunner, temp_env: Path) -> None:
        """create with invalid template name shows clean error, not traceback."""
        result = runner.invoke(main, ["proxy", "create", "../outside", "--no-start"])
        assert result.exit_code != 0
        assert "Invalid template name" in result.output
        assert "Traceback" not in result.output

    def test_template_list_skips_invalid_user_filenames(self, runner: CliRunner, temp_env: Path) -> None:
        """Invalid filenames like .hidden.yaml in user templates dir are silently skipped."""
        forge_home = Path(os.environ["FORGE_HOME"])
        tpl_dir = forge_home / "templates"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / ".hidden.yaml").write_text("proxy: {}\n")
        (tpl_dir / "valid-name.yaml").write_text("proxy: {}\n")

        result = runner.invoke(main, ["proxy", "template", "list"])
        assert result.exit_code == 0
        assert ".hidden" not in result.output
        assert "valid-name" in result.output

    def test_proxy_show_rejects_template_flag(self, runner: CliRunner, temp_env: Path) -> None:
        """--template flag was removed from proxy show."""
        result = runner.invoke(main, ["proxy", "show", "foo", "--template"])
        assert result.exit_code != 0


class TestProxyCreateMissingUrl:
    """Regression: missing LITELLM_BASE_URL should give actionable error, not traceback."""

    @pytest.fixture(autouse=True)
    def _no_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        monkeypatch.delenv("LITELLM_LOCAL_BASE_URL", raising=False)
        # Also block credential-file fallback so the URL is truly missing
        monkeypatch.setattr(
            "forge.proxy.proxy_orchestrator.resolve_env_or_credential",
            lambda key: None,
        )

    def test_no_traceback_on_missing_base_url(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "create", "litellm-openai", "--no-start"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "upstream URL" in result.output

    def test_error_suggests_auth_login(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["proxy", "create", "litellm-openai", "--no-start"])
        assert "forge auth login" in result.output
