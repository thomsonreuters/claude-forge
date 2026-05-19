"""Tests for ``forge session handoff show`` (D2 review surface)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_session_with_reports(
    tmp_path: Path, session_name: str = "my-session", report_count: int = 0
) -> tuple[Path, list[Path]]:
    """Create a session in the index and seed N handoff review files."""
    from forge.session import IndexStore

    forge_root = tmp_path / "project"
    (forge_root / ".forge" / "sessions" / session_name).mkdir(parents=True)
    (forge_root / ".forge" / "sessions" / session_name / "forge.session.json").write_text("{}")

    index = IndexStore()
    index.add_session(
        name=session_name,
        worktree_path=str(forge_root),
        project_root=str(tmp_path),
        forge_root=str(forge_root),
        checkout_root=str(forge_root),
        relative_path=".",
        is_incognito=False,
        is_fork=False,
        parent_session=None,
    )

    review = forge_root / ".forge" / "artifacts" / session_name / "handoff"
    review.mkdir(parents=True, exist_ok=True)
    reports: list[Path] = []
    for i in range(report_count):
        # Different timestamps so sorting is well-defined
        f = review / f"review-2026010{i}-120000.md"
        f.write_text(f"# Handoff Agent Report\n\nrun {i}\n", encoding="utf-8")
        reports.append(f)
    return forge_root, reports


class TestShowCommand:
    def test_no_reports_emits_friendly_message(self, runner: CliRunner, tmp_path: Path) -> None:
        forge_root, _ = _seed_session_with_reports(tmp_path, "s1", report_count=0)
        with patch("forge.cli.session.SessionManager"), patch("forge.cli.session_handoff.SessionManager"):
            # _resolve_session_forge_root expects manager state matching the seeded session
            from forge.session import SessionManager

            real_manager = SessionManager()
            with patch("forge.cli.session_handoff.SessionManager", return_value=real_manager):
                with patch("forge.cli.session_handoff._cwd_forge_root", return_value=forge_root):
                    result = runner.invoke(main, ["session", "handoff", "show", "s1"])
        assert result.exit_code == 0, result.output
        assert "No handoff reports found" in result.output

    def test_shows_latest_by_default(self, runner: CliRunner, tmp_path: Path) -> None:
        forge_root, reports = _seed_session_with_reports(tmp_path, "s1", report_count=3)

        from forge.session import SessionManager

        real_manager = SessionManager()
        with patch("forge.cli.session_handoff.SessionManager", return_value=real_manager):
            with patch("forge.cli.session_handoff._cwd_forge_root", return_value=forge_root):
                result = runner.invoke(main, ["session", "handoff", "show", "s1"])

        assert result.exit_code == 0, result.output
        # Latest is the highest-numbered (run 2)
        assert "run 2" in result.output
        # Path may be wrapped by Rich's word-wrap; normalize whitespace before checking
        normalized = "".join(result.output.split())
        assert reports[-1].name.replace("-", "") in normalized.replace("-", "")

    def test_all_flag_lists_each_report(self, runner: CliRunner, tmp_path: Path) -> None:
        forge_root, reports = _seed_session_with_reports(tmp_path, "s1", report_count=3)

        from forge.session import SessionManager

        real_manager = SessionManager()
        with patch("forge.cli.session_handoff.SessionManager", return_value=real_manager):
            with patch("forge.cli.session_handoff._cwd_forge_root", return_value=forge_root):
                result = runner.invoke(main, ["session", "handoff", "show", "s1", "--all"])

        assert result.exit_code == 0, result.output
        for report in reports:
            assert report.name in result.output

    def test_latest_and_all_mutually_exclusive(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["session", "handoff", "show", "s1", "--latest", "--all"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_no_session_name_outside_forge_project(self, runner: CliRunner, tmp_path: Path) -> None:
        from forge.session import SessionManager

        real_manager = SessionManager()
        with patch("forge.cli.session_handoff.SessionManager", return_value=real_manager):
            with patch("forge.cli.session_handoff._cwd_forge_root", return_value=None):
                result = runner.invoke(main, ["session", "handoff", "show"])
        assert result.exit_code != 0
        assert "Not inside a Forge project" in result.output

    def test_no_session_name_prefers_forge_session_env(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forge_root, _ = _seed_session_with_reports(tmp_path, "current", report_count=1)
        _seed_session_with_reports(tmp_path, "other", report_count=1)
        monkeypatch.setenv("FORGE_SESSION", "current")
        monkeypatch.setenv("FORGE_FORGE_ROOT", str(forge_root))

        from forge.session import SessionManager

        real_manager = SessionManager()
        with patch("forge.cli.session_handoff.SessionManager", return_value=real_manager):
            with patch("forge.cli.session_handoff._cwd_forge_root", return_value=forge_root):
                result = runner.invoke(main, ["session", "handoff", "show"])

        assert result.exit_code == 0, result.output
        assert "current" in result.output
        assert "run 0" in result.output
