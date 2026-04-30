"""Tests for forge logs command."""

from __future__ import annotations

import logging
import os
import time

import pytest
from click.testing import CliRunner

from forge.cli.logs import (
    _extract_pid,
    _file_age_days,
    _is_log_file,
    _is_older_than,
    _oldest_file_age_days,
    _remove_files,
    auto_clean_old_logs,
)
from forge.cli.main import main


@pytest.fixture(autouse=True)
def _isolate_forge_logger():
    """Prevent debug logging state from leaking across test modules."""
    forge_logger = logging.getLogger("forge")
    original_handlers = forge_logger.handlers[:]
    original_level = forge_logger.level
    original_propagate = forge_logger.propagate
    yield
    for h in forge_logger.handlers:
        if h not in original_handlers:
            h.close()
    forge_logger.handlers = original_handlers
    forge_logger.level = original_level
    forge_logger.propagate = original_propagate


# ---------------------------------------------------------------------------
# Basic forge logs display
# ---------------------------------------------------------------------------


def test_logs_shows_directory_and_level(tmp_path, monkeypatch):
    """forge logs shows the log directory path and level."""
    monkeypatch.delenv("FORGE_DEBUG", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert result.exit_code == 0
    assert "Log directory:" in result.output
    assert "Log level:" in result.output
    assert "off" in result.output


def test_logs_shows_level_debug_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_DEBUG", "1")
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert "debug" in result.output


def test_logs_command_does_not_create_self_log_file(tmp_path, monkeypatch):
    """`forge logs` should inspect logs without generating a fresh logs.*.log file."""
    monkeypatch.setenv("FORGE_DEBUG", "1")
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert result.exit_code == 0
    logs_dir = tmp_path / "forge_home" / "logs" / "cli"
    assert not logs_dir.exists() or not list(logs_dir.glob("logs.*.log"))


def test_logs_shows_tip_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_DEBUG", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert "forge config set log_level=debug" in result.output
    assert "FORGE_DEBUG=1" in result.output


def test_logs_shows_retention_unlimited(tmp_path, monkeypatch):
    """Default retention shows 'unlimited'."""
    monkeypatch.delenv("FORGE_DEBUG", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert "unlimited" in result.output


def test_logs_shows_retention_days_when_set(tmp_path, monkeypatch):
    """When log_retention_days is set, show it in the display."""
    from forge.core.paths import get_forge_home
    from forge.runtime_config import reset_runtime_config

    config_path = get_forge_home() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("log_retention_days: 14\n")
    reset_runtime_config()

    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert "14 days" in result.output
    assert "auto-cleanup" in result.output


def test_logs_shows_file_counts(tmp_path, monkeypatch):
    """forge logs shows file counts per subdirectory."""
    from forge.core.paths import get_forge_home

    logs_dir = get_forge_home() / "logs" / "hooks"
    logs_dir.mkdir(parents=True)
    (logs_dir / "policy-check.99999999.log").write_text("x" * 1024)

    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert result.exit_code == 0
    assert "hooks/" in result.output
    assert "1 files" in result.output


def test_logs_shows_total_summary(tmp_path, monkeypatch):
    """forge logs shows total file count and size."""
    from forge.core.paths import get_forge_home

    hooks_dir = get_forge_home() / "logs" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "a.log").write_text("data")
    cli_dir = get_forge_home() / "logs" / "cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "b.log").write_text("data")

    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert "Total:" in result.output
    assert "2 files" in result.output


# ---------------------------------------------------------------------------
# forge logs --clean
# ---------------------------------------------------------------------------


def test_logs_clean_no_logs(tmp_path, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean"])
    assert result.exit_code == 0


def test_logs_clean_removes_files(tmp_path, monkeypatch):
    """forge logs --clean removes log files."""
    from forge.core.paths import get_forge_home

    logs_dir = get_forge_home() / "logs" / "hooks"
    logs_dir.mkdir(parents=True)
    (logs_dir / "policy-check.99999998.log").write_text("test")
    (logs_dir / "session-start.99999999.log").write_text("test")

    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean"])
    assert result.exit_code == 0
    assert "Removed 2 log files" in result.output
    assert not list(logs_dir.glob("*.log"))


def test_logs_clean_reports_active_files(tmp_path, monkeypatch):
    """forge logs --clean reports files belonging to running processes."""
    from forge.core.paths import get_forge_home

    logs_dir = get_forge_home() / "logs" / "proxy"
    logs_dir.mkdir(parents=True)

    # Use current PID so the file is detected as active
    pid = os.getpid()
    (logs_dir / f"proxy.{pid}.log").write_text("active proxy log")
    (logs_dir / "proxy.99999999.log").write_text("dead proxy log")

    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean"])
    assert result.exit_code == 0
    assert "running process" in result.output
    assert (logs_dir / f"proxy.{pid}.log").exists()
    assert not (logs_dir / "proxy.99999999.log").exists()


# ---------------------------------------------------------------------------
# forge logs --clean --older-than
# ---------------------------------------------------------------------------


def test_older_than_requires_clean(tmp_path, monkeypatch):
    """--older-than without --clean is an error."""
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--older-than", "7"])
    assert result.exit_code != 0
    assert "--older-than requires --clean" in result.output


def test_older_than_rejects_zero(tmp_path, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean", "--older-than", "0"])
    assert result.exit_code != 0
    assert "--older-than must be >= 1" in result.output


def test_older_than_rejects_negative(tmp_path, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean", "--older-than", "-5"])
    # Click rejects negative int for type=int before our validator
    assert result.exit_code != 0


def test_clean_older_than_removes_old_files_only(tmp_path, monkeypatch):
    """--older-than only removes files older than N days."""
    from forge.core.paths import get_forge_home

    logs_dir = get_forge_home() / "logs" / "cli"
    logs_dir.mkdir(parents=True)

    old_file = logs_dir / "old.log"
    old_file.write_text("old data")
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    new_file = logs_dir / "new.log"
    new_file.write_text("new data")

    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean", "--older-than", "7"])
    assert result.exit_code == 0
    assert "Removed 1 log file" in result.output
    assert "older than 7 days" in result.output
    assert not old_file.exists()
    assert new_file.exists()


def test_clean_older_than_no_matches(tmp_path, monkeypatch):
    """--older-than with no old files shows appropriate message."""
    from forge.core.paths import get_forge_home

    logs_dir = get_forge_home() / "logs" / "cli"
    logs_dir.mkdir(parents=True)
    (logs_dir / "recent.log").write_text("data")

    runner = CliRunner()
    result = runner.invoke(main, ["logs", "--clean", "--older-than", "7"])
    assert result.exit_code == 0
    assert "No log files older than 7 days" in result.output


# ---------------------------------------------------------------------------
# Helper functions (unit tests)
# ---------------------------------------------------------------------------


class TestExtractPid:
    def test_standard_log(self):
        assert _extract_pid("proxy.12345.log") == 12345

    def test_jsonl_with_datestamp(self):
        assert _extract_pid("20260327_proxy.12345.jsonl") == 12345

    def test_rotated_log(self):
        assert _extract_pid("proxy.12345.log.1") == 12345

    def test_rotated_log_high_suffix(self):
        assert _extract_pid("proxy.12345.log.5") == 12345

    def test_no_pid(self):
        assert _extract_pid("debug.log") is None

    def test_non_numeric_pid(self):
        assert _extract_pid("proxy.abc.log") is None

    def test_cli_component(self):
        assert _extract_pid("session-start.67890.log") == 67890


class TestIsLogFile:
    def test_log_extension(self, tmp_path):
        assert _is_log_file(tmp_path / "proxy.123.log")

    def test_jsonl_extension(self, tmp_path):
        assert _is_log_file(tmp_path / "20260327_proxy.123.jsonl")

    def test_rotated_log(self, tmp_path):
        assert _is_log_file(tmp_path / "proxy.123.log.1")

    def test_rotated_log_high(self, tmp_path):
        assert _is_log_file(tmp_path / "proxy.123.log.5")

    def test_rejects_txt(self, tmp_path):
        assert not _is_log_file(tmp_path / "notes.txt")

    def test_rejects_json(self, tmp_path):
        assert not _is_log_file(tmp_path / "metadata.json")

    def test_rejects_yaml(self, tmp_path):
        assert not _is_log_file(tmp_path / "config.yaml")


class TestFileAgeDays:
    def test_recent_file(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("data")
        age = _file_age_days(f)
        assert age < 0.01

    def test_old_file(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("data")
        old_mtime = time.time() - (5 * 86400)
        os.utime(f, (old_mtime, old_mtime))
        age = _file_age_days(f)
        assert 4.9 < age < 5.1


class TestIsOlderThan:
    def test_recent_file_not_older(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("data")
        assert not _is_older_than(f, 7)

    def test_old_file_is_older(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("data")
        old_mtime = time.time() - (10 * 86400)
        os.utime(f, (old_mtime, old_mtime))
        assert _is_older_than(f, 7)

    def test_missing_file_returns_false(self, tmp_path):
        assert not _is_older_than(tmp_path / "nonexistent.log", 7)


class TestOldestFileAgeDays:
    def test_empty_dir_returns_none(self, tmp_path):
        assert _oldest_file_age_days(tmp_path / "nonexistent") is None

    def test_finds_oldest_in_subdirs(self, tmp_path):
        logs_root = tmp_path / "logs"
        subdir = logs_root / "cli"
        subdir.mkdir(parents=True)

        new = subdir / "new.log"
        new.write_text("data")

        old = subdir / "old.log"
        old.write_text("data")
        old_mtime = time.time() - (5 * 86400)
        os.utime(old, (old_mtime, old_mtime))

        age = _oldest_file_age_days(logs_root)
        assert age is not None
        assert 4.9 < age < 5.1


class TestRemoveFiles:
    def test_remove_all(self, tmp_path):
        subdir = tmp_path / "cli"
        subdir.mkdir()
        (subdir / "a.log").write_text("data")
        (subdir / "b.log").write_text("data")

        removed, failed, skipped = _remove_files(tmp_path, older_than_days=None)
        assert removed == 2
        assert failed == 0
        assert skipped == 0

    def test_remove_older_than(self, tmp_path):
        subdir = tmp_path / "cli"
        subdir.mkdir()

        old = subdir / "old.log"
        old.write_text("data")
        old_mtime = time.time() - (10 * 86400)
        os.utime(old, (old_mtime, old_mtime))

        new = subdir / "new.log"
        new.write_text("data")

        removed, failed, skipped = _remove_files(tmp_path, older_than_days=7)
        assert removed == 1
        assert failed == 0
        assert not old.exists()
        assert new.exists()

    def test_nonexistent_dir_returns_zeros(self, tmp_path):
        removed, failed, skipped = _remove_files(tmp_path / "nope")
        assert removed == 0
        assert failed == 0
        assert skipped == 0

    def test_skips_active_process_files(self, tmp_path):
        """P1 fix: files belonging to a running process are not deleted."""
        subdir = tmp_path / "proxy"
        subdir.mkdir()

        # Use current PID (guaranteed alive)
        pid = os.getpid()
        active_file = subdir / f"proxy.{pid}.log"
        active_file.write_text("active log data")

        # Dead PID (99999999 is almost certainly not running)
        dead_file = subdir / "proxy.99999999.log"
        dead_file.write_text("dead log data")

        removed, failed, skipped = _remove_files(tmp_path, older_than_days=None)
        assert removed == 1
        assert skipped == 1
        assert active_file.exists()
        assert not dead_file.exists()

    def test_skips_active_rotated_log(self, tmp_path):
        """P1 fix: rotated logs (.log.1) for active processes are also skipped."""
        subdir = tmp_path / "proxy"
        subdir.mkdir()

        pid = os.getpid()
        rotated = subdir / f"proxy.{pid}.log.1"
        rotated.write_text("rotated log data")

        removed, failed, skipped = _remove_files(tmp_path, older_than_days=None)
        assert removed == 0
        assert skipped == 1
        assert rotated.exists()

    def test_preserves_non_log_files(self, tmp_path):
        """Review fix: non-log files in log subdirectories are not deleted."""
        subdir = tmp_path / "tool_events"
        subdir.mkdir()
        (subdir / "20260327_proxy.123.jsonl").write_text("log data")
        (subdir / "metadata.json").write_text("important metadata")
        (subdir / "notes.txt").write_text("notes")

        removed, failed, skipped = _remove_files(tmp_path, older_than_days=None)
        assert removed == 1
        assert (subdir / "metadata.json").exists()
        assert (subdir / "notes.txt").exists()


# ---------------------------------------------------------------------------
# Auto-cleanup
# ---------------------------------------------------------------------------


def test_auto_clean_skips_when_disabled(tmp_path, monkeypatch):
    """auto_clean_old_logs is a no-op when log_retention_days is 0."""
    from forge.core.paths import get_forge_home
    from forge.runtime_config import reset_runtime_config

    logs_dir = get_forge_home() / "logs" / "cli"
    logs_dir.mkdir(parents=True)
    (logs_dir / "old.log").write_text("data")

    reset_runtime_config()
    auto_clean_old_logs()
    assert (logs_dir / "old.log").exists()


def test_auto_clean_removes_old_files(tmp_path, monkeypatch):
    """auto_clean_old_logs removes files older than log_retention_days."""
    from forge.core.paths import get_forge_home
    from forge.runtime_config import reset_runtime_config

    config_path = get_forge_home() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("log_retention_days: 7\n")
    reset_runtime_config()

    logs_dir = get_forge_home() / "logs" / "cli"
    logs_dir.mkdir(parents=True)

    old_file = logs_dir / "old.log"
    old_file.write_text("data")
    old_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    new_file = logs_dir / "new.log"
    new_file.write_text("data")

    auto_clean_old_logs()
    assert not old_file.exists()
    assert new_file.exists()


def test_auto_clean_preserves_active_proxy_logs(tmp_path, monkeypatch):
    """P1 fix: auto-cleanup skips logs belonging to running processes."""
    from forge.core.paths import get_forge_home
    from forge.runtime_config import reset_runtime_config

    config_path = get_forge_home() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("log_retention_days: 7\n")
    reset_runtime_config()

    logs_dir = get_forge_home() / "logs" / "proxy"
    logs_dir.mkdir(parents=True)

    # Old file with current PID (simulates a long-running proxy)
    pid = os.getpid()
    active_file = logs_dir / f"proxy.{pid}.log"
    active_file.write_text("active proxy log")
    old_mtime = time.time() - (30 * 86400)
    os.utime(active_file, (old_mtime, old_mtime))

    auto_clean_old_logs()
    assert active_file.exists()


def test_auto_clean_is_best_effort(tmp_path, monkeypatch):
    """auto_clean_old_logs swallows exceptions."""
    monkeypatch.setattr("forge.cli.logs.get_forge_home", lambda: None)  # type: ignore[arg-type]
    # Should not raise
    auto_clean_old_logs()
