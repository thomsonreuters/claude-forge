"""Tests for `forge claude start` bare launcher."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from forge.cli.main import main

# Mock target: invoke_claude is imported inside start_cmd
_INVOKE = "forge.session.claude.invoke.invoke_claude"


def _write_proxy_registry(
    *,
    forge_home: Path,
    proxy_id: str,
    template: str,
    base_url: str,
    port: int,
    status: str = "healthy",
    extra_proxies: dict | None = None,
) -> None:
    proxies = {
        proxy_id: {
            "proxy_id": proxy_id,
            "template": template,
            "base_url": base_url,
            "port": port,
            "status": status,
        }
    }
    if extra_proxies:
        proxies.update(extra_proxies)
    registry_path = forge_home / "proxies" / "index.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps({"version": 1, "proxies": proxies}),
        encoding="utf-8",
    )


def _setup_proxy_env(
    tmp_path, monkeypatch, *, proxy_id="proxy_1", template="litellm-openai", base_url="http://localhost:8085", port=8085
):
    """Set up proxy registry and mock healthcheck for proxy-mode tests."""
    forge_home = tmp_path / "forge_home"
    _write_proxy_registry(
        forge_home=forge_home,
        proxy_id=proxy_id,
        template=template,
        base_url=base_url,
        port=port,
    )

    from forge.cli import claude as claude_module

    monkeypatch.setattr(claude_module, "_healthcheck_proxy", lambda **_: None)
    return forge_home


# --- Validation ---


def test_requires_proxy_or_no_proxy():
    """forge claude start without --proxy or --no-proxy shows error."""
    runner = CliRunner()
    result = runner.invoke(main, ["claude", "start"])
    assert result.exit_code == 1
    assert "one of --proxy or --no-proxy is required" in result.output


def test_no_proxy_and_proxy_mutually_exclusive():
    """--no-proxy and --proxy cannot be used together."""
    runner = CliRunner()
    result = runner.invoke(main, ["claude", "start", "--no-proxy", "--proxy", "some-proxy"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_proxy_not_found(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)
    runner = CliRunner()
    result = runner.invoke(main, ["claude", "start", "--proxy", "missing"])
    assert result.exit_code == 1


def test_healthcheck_failure(tmp_path, monkeypatch):
    forge_home = tmp_path / "forge_home"
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_proxy_registry(
        forge_home=forge_home,
        proxy_id="proxy_1",
        template="litellm-openai",
        base_url="http://localhost:8085",
        port=8085,
    )
    monkeypatch.chdir(project_root)

    from forge.cli import claude as claude_module

    monkeypatch.setattr(claude_module, "_healthcheck_proxy", lambda **_: (_ for _ in ()).throw(ValueError("fail")))

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "start", "--proxy", "proxy_1"])
    assert result.exit_code == 1


# --- Bare launch: no session state ---


def test_proxy_launch_no_session_state(tmp_path, monkeypatch):
    """Bare launcher does not create a session or set FORGE_SESSION."""
    _setup_proxy_env(tmp_path, monkeypatch)

    captured = {}

    def fake_invoke(*, env_vars=None, unset_env_vars=None, **_kw):
        captured["env_vars"] = env_vars or {}
        captured["unset_env_vars"] = unset_env_vars or []
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "proxy_1"])

    assert result.exit_code == 0, result.output
    assert "FORGE_SESSION" not in captured["env_vars"]
    assert "FORGE_SESSION" in captured["unset_env_vars"]


def test_proxy_launch_sets_base_url_and_context_limit(tmp_path, monkeypatch):
    """Proxy mode sets ANTHROPIC_BASE_URL, CLAUDE_CODE_AUTO_COMPACT_WINDOW, ACTIVE_TEMPLATE."""
    _setup_proxy_env(tmp_path, monkeypatch)

    captured = {}

    def fake_invoke(*, env_vars=None, **_kw):
        captured["env_vars"] = env_vars or {}
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "proxy_1"])

    assert result.exit_code == 0, result.output
    assert captured["env_vars"]["ANTHROPIC_BASE_URL"] == "http://localhost:8085"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in captured["env_vars"]
    assert captured["env_vars"]["ACTIVE_TEMPLATE"] == "litellm-openai"


def test_direct_launch_scrubs_proxy_vars(tmp_path, monkeypatch):
    """Direct mode unsets ANTHROPIC_BASE_URL, ACTIVE_TEMPLATE but not CLAUDE_CODE_AUTO_COMPACT_WINDOW."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    captured = {}

    def fake_invoke(*, env_vars=None, unset_env_vars=None, **_kw):
        captured["env_vars"] = env_vars or {}
        captured["unset_env_vars"] = unset_env_vars or []
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--no-proxy"])

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_BASE_URL" not in captured["env_vars"]
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in captured["env_vars"]
    assert "ACTIVE_TEMPLATE" not in captured["env_vars"]
    assert "ANTHROPIC_BASE_URL" in captured["unset_env_vars"]
    assert "ACTIVE_TEMPLATE" in captured["unset_env_vars"]
    # CLAUDE_CODE_AUTO_COMPACT_WINDOW is a native CC env var -- Forge doesn't unset it
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in captured["unset_env_vars"]


def test_scrubs_session_identity_vars(tmp_path, monkeypatch):
    """Both proxy and direct mode scrub FORGE_SESSION, FORGE_FORK_NAME, FORGE_PARENT_SESSION."""
    _setup_proxy_env(tmp_path, monkeypatch)

    captured = {}

    def fake_invoke(*, unset_env_vars=None, **_kw):
        captured["unset_env_vars"] = unset_env_vars or []
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "proxy_1"])

    assert result.exit_code == 0, result.output
    assert "FORGE_SESSION" in captured["unset_env_vars"]
    assert "FORGE_FORK_NAME" in captured["unset_env_vars"]
    assert "FORGE_PARENT_SESSION" in captured["unset_env_vars"]


def test_extra_args_forwarded(tmp_path, monkeypatch):
    """-- --debug args are forwarded to invoke_claude as extra_args."""
    _setup_proxy_env(tmp_path, monkeypatch)

    captured = {}

    def fake_invoke(*, extra_args=None, **_kw):
        captured["extra_args"] = extra_args
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "proxy_1", "--", "--debug"])

    assert result.exit_code == 0, result.output
    assert captured["extra_args"] == ["--debug"]


def test_proxy_launch_by_template_injects_resolved_proxy_addendum(tmp_path, monkeypatch):
    """Bare launcher resolves template names to proxy IDs before addendum lookup."""
    _setup_proxy_env(tmp_path, monkeypatch)

    captured = {}

    def fake_resolve(proxy_id: str | None) -> str:
        captured["proxy_id"] = proxy_id
        return "Bare addendum"

    def fake_invoke(*, system_prompt_file=None, **_kw):
        captured["system_prompt_file"] = system_prompt_file
        captured["content"] = Path(system_prompt_file).read_text(encoding="utf-8")
        return 0

    with (
        patch("forge.cli.session_addendum.resolve_addendum_content_for_proxy", side_effect=fake_resolve),
        patch(_INVOKE, side_effect=fake_invoke),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "litellm-openai"])

    assert result.exit_code == 0, result.output
    assert captured["proxy_id"] == "proxy_1"
    assert captured["content"] == "Bare addendum"
    assert not Path(captured["system_prompt_file"]).exists()


# --- Output ---


def test_proxy_launch_prints_summary(tmp_path, monkeypatch):
    """Proxy launch shows proxy_id and template."""
    _setup_proxy_env(tmp_path, monkeypatch)

    with patch(_INVOKE, return_value=0):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "proxy_1"])

    assert result.exit_code == 0, result.output
    assert "proxy_1" in result.output
    assert "litellm-openai" in result.output


def test_direct_launch_prints_summary(tmp_path, monkeypatch):
    """Direct launch says 'direct'."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    with patch(_INVOKE, return_value=0):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--no-proxy"])

    assert result.exit_code == 0, result.output
    assert "direct" in result.output.lower()


def test_direct_launch_honors_default_direct_model(tmp_path, monkeypatch):
    """Direct mode passes default_direct_model through Claude Code env vars."""
    from forge.runtime_config import reset_runtime_config

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    # Write runtime config with a default_direct_model
    forge_home = tmp_path / "forge_home"
    forge_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FORGE_HOME", str(forge_home))
    (forge_home / "config.yaml").write_text("default_direct_model: claude-opus-4-6\n")
    reset_runtime_config()

    captured = {}

    def fake_invoke(*, model=None, env_vars=None, **_kw):
        captured["model"] = model
        captured["env_vars"] = env_vars or {}
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--no-proxy"])

    assert result.exit_code == 0, result.output
    assert captured["model"] is None
    assert captured["env_vars"]["ANTHROPIC_MODEL"] == "opus"
    assert captured["env_vars"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-6"


def test_direct_launch_no_model_when_unconfigured(tmp_path, monkeypatch):
    """Direct mode passes model=None when no default_direct_model configured."""
    from forge.runtime_config import reset_runtime_config

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    forge_home = tmp_path / "forge_home"
    forge_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FORGE_HOME", str(forge_home))
    reset_runtime_config()

    captured = {}

    def fake_invoke(*, model=None, **_kw):
        captured["model"] = model
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--no-proxy"])

    assert result.exit_code == 0, result.output
    assert captured["model"] is None


# --- Proxy resolution variants ---


def test_proxy_by_template_name(tmp_path, monkeypatch):
    """--proxy accepts a template name when exactly one active proxy uses it."""
    _setup_proxy_env(tmp_path, monkeypatch)

    captured = {}

    def fake_invoke(*, env_vars=None, **_kw):
        captured["env_vars"] = env_vars or {}
        return 0

    with patch(_INVOKE, side_effect=fake_invoke):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "start", "--proxy", "litellm-openai"])

    assert result.exit_code == 0, result.output
    assert captured["env_vars"]["ANTHROPIC_BASE_URL"] == "http://localhost:8085"


def test_proxy_template_inactive_only(tmp_path, monkeypatch):
    """Template match fails when all matching proxies are stopped."""
    forge_home = tmp_path / "forge_home"
    project_root = tmp_path / "project"
    project_root.mkdir()

    _write_proxy_registry(
        forge_home=forge_home,
        proxy_id="proxy_1",
        template="litellm-openai",
        base_url="http://localhost:8085",
        port=8085,
        status="stopped",
    )
    monkeypatch.chdir(project_root)

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "start", "--proxy", "litellm-openai"])
    assert result.exit_code == 1
    assert "none are active" in result.output


def test_proxy_template_ambiguous(tmp_path, monkeypatch):
    """Template match fails when multiple active proxies share the template."""
    forge_home = tmp_path / "forge_home"
    project_root = tmp_path / "project"
    project_root.mkdir()

    _write_proxy_registry(
        forge_home=forge_home,
        proxy_id="proxy_1",
        template="litellm-openai",
        base_url="http://localhost:8085",
        port=8085,
        status="healthy",
        extra_proxies={
            "proxy_2": {
                "proxy_id": "proxy_2",
                "template": "litellm-openai",
                "base_url": "http://localhost:8086",
                "port": 8086,
                "status": "healthy",
            }
        },
    )
    monkeypatch.chdir(project_root)

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "start", "--proxy", "litellm-openai"])
    assert result.exit_code == 1
    assert "ambiguous" in result.output
    assert "proxy_id" in result.output


# --- Other commands ---


def test_forge_upgrade_removed():
    runner = CliRunner()
    result = runner.invoke(main, ["upgrade", "--dry-run"])
    assert result.exit_code != 0
    assert "No such command" in result.output


# --- _build_bare_launch_env unit tests ---


class TestBuildBareLaunchEnv:
    """Direct tests for the env builder."""

    def test_proxy_mode(self):
        from forge.cli.claude import _build_bare_launch_env

        env, unset = _build_bare_launch_env(
            base_url="http://localhost:8085",
            template="litellm-openai",
            context_limit=200000,
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8085"
        assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
        assert env["ACTIVE_TEMPLATE"] == "litellm-openai"
        assert "FORGE_SESSION" in unset
        assert "FORGE_FORK_NAME" in unset
        assert "FORGE_PARENT_SESSION" in unset

    def test_direct_mode(self):
        from forge.cli.claude import _build_bare_launch_env

        env, unset = _build_bare_launch_env(
            base_url=None,
            template=None,
            context_limit=None,
        )
        assert env == {}
        assert "FORGE_SESSION" in unset
        assert "FORGE_FORK_NAME" in unset
        assert "FORGE_PARENT_SESSION" in unset
        assert "ANTHROPIC_BASE_URL" in unset
        assert "ACTIVE_TEMPLATE" in unset
        # CLAUDE_CODE_AUTO_COMPACT_WINDOW is native CC env var -- Forge doesn't unset it
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in unset

    def test_proxy_without_template(self):
        from forge.cli.claude import _build_bare_launch_env

        env, unset = _build_bare_launch_env(
            base_url="http://localhost:8085",
            template=None,
            context_limit=100000,
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8085"
        assert "ACTIVE_TEMPLATE" not in env
        assert "ACTIVE_TEMPLATE" in unset
