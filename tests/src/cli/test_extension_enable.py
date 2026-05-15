"""Tests for extension enable: scope/path resolution, anchor validation, Rule 4."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pytest

from forge.cli.extensions import (
    _create_claude_dir,
    _detect_git_project_root,
    _find_git_root,
    _resolve_project_root,
    _validate_anchor,
)
from forge.install.exceptions import NoClaudeDirectoryError
from forge.install.models import InstallScope


class TestFindGitRoot:
    """Tests for _find_git_root helper."""

    def test_finds_git_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert _find_git_root(tmp_path) == tmp_path

    def test_finds_git_in_parent(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        child = tmp_path / "src" / "deep"
        child.mkdir(parents=True)
        assert _find_git_root(child) == tmp_path

    def test_returns_none_outside_git(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nonexistent")
        assert _find_git_root(tmp_path) is None


class TestDetectGitProjectRoot:
    """Tests for _detect_git_project_root (Rule 4 detector)."""

    def test_detects_git_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src"
        subdir.mkdir()

        result = _detect_git_project_root(start=subdir)
        assert result == tmp_path.resolve()

    def test_returns_none_outside_git(self, tmp_path: Path) -> None:
        result = _detect_git_project_root(start=tmp_path)
        assert result is None

    def test_returns_none_at_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Does not return home directory even if it has .git."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".git").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _detect_git_project_root(start=fake_home)
        assert result is None


class TestCreateClaudeDir:
    """Tests for _create_claude_dir."""

    def test_creates_claude_dir(self, tmp_path: Path) -> None:
        _create_claude_dir(tmp_path)
        assert (tmp_path / ".claude").is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        _create_claude_dir(tmp_path)
        assert (tmp_path / ".claude").is_dir()


class TestResolveProjectRootAutoCreate:
    """Tests for _resolve_project_root with Rule 4 auto-create."""

    def test_user_scope_returns_none(self) -> None:
        assert _resolve_project_root(InstallScope.USER) is None

    def test_project_scope_detects_git_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--project in a git repo without .claude/ returns git root (no auto_create)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.chdir(repo)

        result = _resolve_project_root(InstallScope.PROJECT, auto_create=False)

        assert result == repo.resolve()
        # .claude/ NOT created when auto_create=False
        assert not (repo / ".claude").is_dir()

    def test_project_scope_auto_creates_claude(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--project with auto_create=True creates .claude/ at git root."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.chdir(repo)

        result = _resolve_project_root(InstallScope.PROJECT, auto_create=True)

        assert result == repo.resolve()
        assert (repo / ".claude").is_dir()

    def test_local_scope_auto_creates_claude(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--local with auto_create=True creates .claude/ at git root."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.chdir(repo)

        result = _resolve_project_root(InstallScope.LOCAL, auto_create=True)

        assert result == repo.resolve()
        assert (repo / ".claude").is_dir()

    def test_project_scope_raises_outside_git(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--project outside a git repo raises NoClaudeDirectoryError."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        no_git = tmp_path / "random-dir"
        no_git.mkdir()
        monkeypatch.chdir(no_git)

        with pytest.raises(NoClaudeDirectoryError):
            _resolve_project_root(InstallScope.PROJECT)

    def test_existing_claude_not_recreated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When .claude/ already exists, returns it without auto-create."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".claude").mkdir()
        monkeypatch.chdir(repo)

        result = _resolve_project_root(InstallScope.LOCAL)

        assert result == repo.resolve()


class TestEnableFailureCleanup:
    """Verify .forge/ is not created when enable fails."""

    def test_enable_failure_does_not_leave_forge_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed enable should not leave an orphaned .forge/ directory."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from forge.cli.extensions import enable_cmd

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".claude").mkdir()

        monkeypatch.chdir(repo)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()

        # Mock installer to return a plan with conflicts
        mock_plan = MagicMock()
        mock_plan.has_conflicts = True
        mock_plan.files = []
        mock_plan.settings_entries = []

        with (
            patch("forge.cli.extensions.Installer") as MockInstaller,
            patch("forge.install.version.check_minimum_version") as mock_ver,
        ):
            mock_instance = MockInstaller.return_value
            mock_instance.init.return_value = mock_plan
            mock_ver.return_value = MagicMock(ok=True)
            runner = CliRunner()
            result = runner.invoke(enable_cmd, ["--scope", "local"])

        assert result.exit_code != 0
        assert not (repo / ".forge").is_dir()


class TestEmptyModuleWarning:
    """Tests for the 0-file sanity warning (catches broken installs)."""

    def _make_plan(self, modules: list[str], file_paths: list[str]) -> Any:
        from unittest.mock import MagicMock

        plan = MagicMock()
        plan.modules = modules
        plan.files = [MagicMock(target_path=p, action="install") for p in file_paths]
        plan.settings = []
        return plan

    def test_warns_when_file_module_has_no_files_anywhere(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from forge.cli.extensions import _warn_if_modules_have_no_files
        from forge.install.tracking import TrackingStore

        plan = self._make_plan(modules=["skills", "hooks"], file_paths=[])
        tracking = MagicMock(spec=TrackingStore)
        tracking.get_installation.return_value = None  # no prior install

        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("forge.cli.extensions.console", Console(file=buf, width=200))
            _warn_if_modules_have_no_files(plan, InstallScope.USER, None, tracking)

        output = buf.getvalue()
        assert "Warning" in output
        assert "skills" in output

    def test_no_warn_when_files_in_plan(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from forge.cli.extensions import _warn_if_modules_have_no_files
        from forge.install.tracking import TrackingStore

        plan = self._make_plan(
            modules=["skills"],
            file_paths=["/some/path/.claude/skills/foo/SKILL.md"],
        )
        tracking = MagicMock(spec=TrackingStore)
        tracking.get_installation.return_value = None

        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("forge.cli.extensions.console", Console(file=buf, width=200))
            _warn_if_modules_have_no_files(plan, InstallScope.USER, None, tracking)

        assert "Warning" not in buf.getvalue()

    def test_no_warn_when_files_in_existing_install(self, tmp_path: Path) -> None:
        """Up-to-date install: 0 plan files but tracking has files → no warning."""
        from unittest.mock import MagicMock

        from forge.cli.extensions import _warn_if_modules_have_no_files
        from forge.install.tracking import TrackingStore

        plan = self._make_plan(modules=["skills"], file_paths=[])
        tracking = MagicMock(spec=TrackingStore)
        existing = MagicMock()
        existing.files = [MagicMock(target_path="/some/path/.claude/skills/foo/SKILL.md")]
        tracking.get_installation.return_value = existing

        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("forge.cli.extensions.console", Console(file=buf, width=200))
            _warn_if_modules_have_no_files(plan, InstallScope.USER, None, tracking)

        assert "Warning" not in buf.getvalue()

    def test_no_warn_for_intentionally_empty_modules(self, tmp_path: Path) -> None:
        """Allowlisted empty modules (agents, commands) should not warn."""
        from unittest.mock import MagicMock

        from forge.cli.extensions import _warn_if_modules_have_no_files
        from forge.install.tracking import TrackingStore

        plan = self._make_plan(modules=["agents", "commands", "skills"], file_paths=[])
        tracking = MagicMock(spec=TrackingStore)
        tracking.get_installation.return_value = None

        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("forge.cli.extensions.console", Console(file=buf, width=200))
            _warn_if_modules_have_no_files(plan, InstallScope.USER, None, tracking)

        output = buf.getvalue()
        # Should warn about skills (not allowlisted, 0 files), not agents/commands
        assert "Warning" in output
        assert "skills" in output
        assert "agents" not in output
        assert "commands" not in output

    def test_no_warn_for_settings_only_modules(self, tmp_path: Path) -> None:
        """Settings-only modules (hooks, permissions) should never trigger the warning."""
        from unittest.mock import MagicMock

        from forge.cli.extensions import _warn_if_modules_have_no_files
        from forge.install.tracking import TrackingStore

        plan = self._make_plan(modules=["hooks", "permissions", "status-line"], file_paths=[])
        tracking = MagicMock(spec=TrackingStore)
        tracking.get_installation.return_value = None

        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("forge.cli.extensions.console", Console(file=buf, width=200))
            _warn_if_modules_have_no_files(plan, InstallScope.USER, None, tracking)

        assert "Warning" not in buf.getvalue()


class TestValidateAnchor:
    """Tests for _validate_anchor (inside-.claude guard)."""

    def test_rejects_path_inside_claude_dir(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / "repo" / ".claude"
        claude_dir.mkdir(parents=True)
        with pytest.raises(click.UsageError, match="inside a .claude directory"):
            _validate_anchor(claude_dir)

    def test_rejects_nested_claude_path(self, tmp_path: Path) -> None:
        nested = tmp_path / "repo" / ".claude" / "sub" / "deep"
        nested.mkdir(parents=True)
        with pytest.raises(click.UsageError, match="inside a .claude directory"):
            _validate_anchor(nested)

    def test_accepts_normal_project_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _validate_anchor(repo)

    def test_accepts_path_containing_claude_in_name(self, tmp_path: Path) -> None:
        """A directory named 'claude-forge' should not be rejected."""
        repo = tmp_path / "claude-forge"
        repo.mkdir()
        _validate_anchor(repo)


class TestResolveProjectRootAnchor:
    """Tests for _resolve_project_root with explicit anchor."""

    def test_anchor_bypasses_walk_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anchor should return that path directly, not walk up."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        target = tmp_path / "target"
        target.mkdir()
        (target / ".claude").mkdir()

        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)

        result = _resolve_project_root(InstallScope.LOCAL, anchor=target)
        assert result == target.resolve()

    def test_anchor_auto_creates_claude(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()

        result = _resolve_project_root(InstallScope.LOCAL, anchor=target, auto_create=True)
        assert result == target.resolve()
        assert (target / ".claude").is_dir()

    def test_anchor_without_auto_create(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()

        result = _resolve_project_root(InstallScope.LOCAL, anchor=target, auto_create=False)
        assert result == target.resolve()
        assert not (target / ".claude").is_dir()

    def test_anchor_ignored_for_user_scope(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        assert _resolve_project_root(InstallScope.USER, anchor=target) is None

    def test_anchor_normalizes_path(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / "src").mkdir(parents=True)
        # Pass a non-canonical path with ..
        target = tmp_path / "repo" / "src" / ".." / "src"

        result = _resolve_project_root(InstallScope.LOCAL, anchor=target)
        assert result == (tmp_path / "repo" / "src").resolve()


class TestEnableWithPath:
    """Tests for enable_cmd with --scope and --path options."""

    def test_path_with_scope_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from forge.cli.extensions import enable_cmd

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".claude").mkdir()

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()

        mock_plan = MagicMock()
        mock_plan.has_conflicts = False
        mock_plan.files = []
        mock_plan.settings = []
        mock_plan.settings_entries = []
        mock_plan.modules = []
        mock_plan.profile = "standard"

        with (
            patch("forge.cli.extensions.Installer") as MockInstaller,
            patch("forge.install.version.check_minimum_version") as mock_ver,
        ):
            mock_instance = MockInstaller.return_value
            mock_instance.init.return_value = mock_plan
            mock_ver.return_value = MagicMock(ok=True)

            runner = CliRunner()
            result = runner.invoke(enable_cmd, ["--scope", "local", "--path", str(repo)])

        assert result.exit_code == 0
        MockInstaller.assert_called_once()
        call_kwargs = MockInstaller.call_args
        assert call_kwargs.kwargs["scope"] == InstallScope.LOCAL
        assert call_kwargs.kwargs["project_root"] == repo.resolve()

    def test_path_defaults_to_local_scope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from forge.cli.extensions import enable_cmd

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".claude").mkdir()

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()

        mock_plan = MagicMock()
        mock_plan.has_conflicts = False
        mock_plan.files = []
        mock_plan.settings = []
        mock_plan.settings_entries = []
        mock_plan.modules = []
        mock_plan.profile = "standard"

        with (
            patch("forge.cli.extensions.Installer") as MockInstaller,
            patch("forge.install.version.check_minimum_version") as mock_ver,
        ):
            mock_instance = MockInstaller.return_value
            mock_instance.init.return_value = mock_plan
            mock_ver.return_value = MagicMock(ok=True)

            runner = CliRunner()
            result = runner.invoke(enable_cmd, ["--path", str(repo)])

        assert result.exit_code == 0
        call_kwargs = MockInstaller.call_args
        assert call_kwargs.kwargs["scope"] == InstallScope.LOCAL

    def test_path_with_scope_user_errors(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from forge.cli.extensions import enable_cmd

        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("forge.install.version.check_minimum_version") as mock_ver:
            mock_ver.return_value = MagicMock(ok=True)
            runner = CliRunner()
            result = runner.invoke(enable_cmd, ["--scope", "user", "--path", str(repo)])

        assert result.exit_code != 0
        assert "not applicable" in result.output.lower()

    def test_dry_run_with_path_no_side_effects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from forge.cli.extensions import enable_cmd

        repo = tmp_path / "repo"
        repo.mkdir()

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()

        mock_plan = MagicMock()
        mock_plan.has_conflicts = False
        mock_plan.files = []
        mock_plan.settings = []
        mock_plan.settings_entries = []
        mock_plan.modules = []
        mock_plan.profile = "standard"

        with (
            patch("forge.cli.extensions.Installer") as MockInstaller,
            patch("forge.install.version.check_minimum_version") as mock_ver,
        ):
            mock_instance = MockInstaller.return_value
            mock_instance.plan.return_value = mock_plan
            mock_ver.return_value = MagicMock(ok=True)

            runner = CliRunner()
            result = runner.invoke(enable_cmd, ["--scope", "local", "--path", str(repo), "--dry-run"])

        assert result.exit_code == 0
        assert not (repo / ".claude").is_dir()
        assert not (repo / ".forge").is_dir()

    def test_path_inside_claude_dir_errors(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from forge.cli.extensions import enable_cmd

        claude_dir = tmp_path / "repo" / ".claude"
        claude_dir.mkdir(parents=True)

        with patch("forge.install.version.check_minimum_version") as mock_ver:
            mock_ver.return_value = MagicMock(ok=True)
            runner = CliRunner()
            result = runner.invoke(enable_cmd, ["--scope", "local", "--path", str(claude_dir)])

        assert result.exit_code != 0
        assert "inside a .claude directory" in result.output


class TestScopeAllConflict:
    """Tests for --all + --scope mutual exclusivity."""

    def test_disable_all_with_scope_errors(self) -> None:
        from click.testing import CliRunner

        from forge.cli.extensions import disable_cmd

        runner = CliRunner()
        result = runner.invoke(disable_cmd, ["--all", "--scope", "local"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_status_all_with_scope_errors(self) -> None:
        from click.testing import CliRunner

        from forge.cli.extensions import status_cmd

        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--all", "--scope", "local"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_status_all_with_path_errors(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from forge.cli.extensions import status_cmd

        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--all", "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_status_user_with_path_errors(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from forge.cli.extensions import status_cmd

        runner = CliRunner()
        result = runner.invoke(status_cmd, ["--scope", "user", "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "not applicable" in result.output.lower()
