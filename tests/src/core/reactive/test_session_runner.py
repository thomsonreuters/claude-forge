"""Tests for forge.core.reactive.session_runner."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from forge.core.reactive.session_runner import SessionResult, run_claude_session


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Ensure ANTHROPIC_API_KEY is absent so --bare auto-detect is off by default."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


class TestSessionResult:
    def test_success_when_zero_returncode(self):
        r = SessionResult(stdout="ok", stderr="", returncode=0)
        assert r.success is True

    def test_not_success_when_nonzero(self):
        r = SessionResult(stdout="", stderr="err", returncode=1)
        assert r.success is False

    def test_not_success_when_timed_out(self):
        r = SessionResult(stdout="", stderr="", returncode=-1, timed_out=True)
        assert r.success is False

    def test_not_success_when_error(self):
        r = SessionResult(stdout="", stderr="", returncode=0, error="something broke")
        assert r.success is False


class TestRunClaudeSession:
    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_success_path(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="response text",
            stderr="",
            returncode=0,
        )
        result = run_claude_session("hello")
        assert result.success
        assert result.stdout == "response text"
        assert result.returncode == 0

        # Verify command (no ANTHROPIC_API_KEY in test env → no --bare)
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd == ["claude", "-p"]
        assert call_args.kwargs["input"] == "hello"
        assert call_args.kwargs["capture_output"] is True

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_resume_id_adds_flag(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", resume_id="abc-123")

        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "-p", "--resume", "abc-123"]

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_base_url_set_in_env(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", base_url="http://localhost:8085")

        env = mock_run.call_args.kwargs["env"]
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8085"

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_cwd_passed_through(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", cwd="/some/path")

        assert mock_run.call_args.kwargs["cwd"] == "/some/path"

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_nonzero_exit_code(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="error output", returncode=2)
        result = run_claude_session("prompt")

        assert not result.success
        assert result.returncode == 2
        assert result.stderr == "error output"
        assert result.error is None  # Non-zero exit is not an error field

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        result = run_claude_session("prompt", timeout_seconds=30)

        assert not result.success
        assert result.timed_out is True
        assert "30s" in result.error

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        result = run_claude_session("prompt")

        assert not result.success
        assert "not found" in result.error

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_generic_exception(self, mock_run):
        mock_run.side_effect = RuntimeError("unexpected")
        result = run_claude_session("prompt")

        assert not result.success
        assert "unexpected" in result.error

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_timeout_seconds_passed_through(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", timeout_seconds=120)

        assert mock_run.call_args.kwargs["timeout"] == 120

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_flag_when_api_key_present(self, mock_run):
        """Auto-detect: --bare added when ANTHROPIC_API_KEY is in env."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            run_claude_session("prompt")

        cmd = mock_run.call_args[0][0]
        assert "--bare" in cmd

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_flag_skipped_without_api_key(self, mock_run):
        """Auto-detect: --bare omitted when ANTHROPIC_API_KEY is absent."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt")

        cmd = mock_run.call_args[0][0]
        assert "--bare" not in cmd

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_auto_uses_hydrated_file_key(self, mock_run, monkeypatch):
        """Auto-detect checks the built env, so credential-file keys enable --bare."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._auth_ignore_env",
            lambda: False,
        )
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._get_file_secrets",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-from-file"},
        )
        monkeypatch.setattr(
            "forge.runtime_config.get_runtime_config",
            lambda: type("C", (), {"auth_ignore_env": False})(),
        )

        run_claude_session("prompt")

        cmd = mock_run.call_args[0][0]
        env = mock_run.call_args.kwargs["env"]
        assert "--bare" in cmd
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-from-file"

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_missing_key_returns_formatted_error_with_proxy_hint(self, mock_run, monkeypatch):
        """Explicit --bare without an API key fails before spawn with actionable guidance."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("FORGE_SUBPROCESS_PROXY", raising=False)
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._auth_ignore_env",
            lambda: False,
        )
        monkeypatch.setattr(
            "forge.core.auth.template_secrets._get_file_secrets",
            lambda: {},
        )
        monkeypatch.setattr(
            "forge.runtime_config.get_runtime_config",
            lambda: type("C", (), {"auth_ignore_env": False})(),
        )

        result = run_claude_session("prompt", bare=True)

        assert not result.success
        assert "ANTHROPIC_API_KEY" in (result.error or "")
        assert "forge auth login -c anthropic-api" in (result.error or "")
        assert "--subprocess-proxy" in (result.error or "")
        mock_run.assert_not_called()

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_explicit_true(self, mock_run):
        """Explicit bare=True forces --bare regardless of env."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            run_claude_session("prompt", bare=True)

        cmd = mock_run.call_args[0][0]
        assert "--bare" in cmd

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_explicit_false(self, mock_run):
        """Explicit bare=False suppresses --bare even with API key."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            run_claude_session("prompt", bare=False)

        cmd = mock_run.call_args[0][0]
        assert "--bare" not in cmd

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_bare_with_resume_id(self, mock_run):
        """--bare appears before --resume when both active."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            run_claude_session("prompt", resume_id="abc-123")

        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "-p", "--bare", "--resume", "abc-123"]

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_fork_session_flag_added_with_resume(self, mock_run):
        """--fork-session appears after --resume when both are set."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", resume_id="abc-123", fork_session=True)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "-p", "--resume", "abc-123", "--fork-session"]

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_fork_session_ignored_without_resume(self, mock_run):
        """--fork-session is not added when resume_id is None."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", fork_session=True)

        cmd = mock_run.call_args[0][0]
        assert "--fork-session" not in cmd

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_fork_session_not_added_when_false(self, mock_run):
        """--fork-session is absent when fork_session=False (the default)."""
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", resume_id="abc-123", fork_session=False)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "-p", "--resume", "abc-123"]

    @patch("forge.core.reactive.session_runner.subprocess.run")
    def test_extra_env_merged(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        run_claude_session("prompt", extra_env={"CUSTOM_VAR": "value"})

        env = mock_run.call_args.kwargs["env"]
        assert env["CUSTOM_VAR"] == "value"
