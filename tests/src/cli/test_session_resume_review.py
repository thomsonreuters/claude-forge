"""Tests for ``forge session resume --review`` (D4 from runtime abstraction Phase 1).

Verifies the --review flag opens the per-child handoff file in $EDITOR before
launching Claude, and that the flag rejects incompatible combinations.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from forge.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestReviewFlagValidation:
    def test_review_requires_fresh(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["session", "resume", "some-session", "--review"])
        assert result.exit_code == 1
        assert "--review requires --fresh" in result.output

    def test_review_rejects_native_mode(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["session", "resume", "some-session", "--fresh", "--review", "--resume-mode", "native"],
        )
        assert result.exit_code == 1
        assert "--review is only meaningful in handoff mode" in result.output


class TestReviewFlagEditorInvocation:
    """--review should open the per-child handoff file in $EDITOR before launch."""

    def test_editor_is_invoked_with_child_file_path(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.session import create_session_state
        from forge.session.handoff import HandoffResult

        # Pretend we have a fake editor that always succeeds
        fake_editor = tmp_path / "fake-editor"
        fake_editor.write_text("#!/bin/sh\nexit 0\n")
        fake_editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(fake_editor))

        parent = create_session_state(
            "p1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(tmp_path),
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        child_file = tmp_path / ".forge" / "prev_sessions" / "p1" / "children" / "child-1.md"
        child_file.parent.mkdir(parents=True)
        child_file.write_text("# Context")

        child_manifest = create_session_state(
            "child-1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="p1",
            worktree_path=str(tmp_path),
        )

        handoff_result = HandoffResult(
            context_file=child_file,
            context_file_rel=".forge/prev_sessions/p1/children/child-1.md",
            transcript_artifact_path=None,
            token_estimate=None,
            lineage=["p1"],
        )

        editor_calls: list[list[str]] = []

        def fake_subprocess_run(args, *_, **__):
            editor_calls.append(list(args))
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with (
            patch("forge.cli.session_lifecycle.SessionManager") as mock_mgr_cls,
            patch("forge.cli.session_lifecycle._launch_claude_for_session", return_value=0),
            patch("forge.cli.session_lifecycle.subprocess.run", side_effect=fake_subprocess_run),
            patch("forge.cli.session.SessionManager") as mock_mgr_cls_sess,
        ):
            mgr = mock_mgr_cls.return_value
            mgr.get_session.return_value = parent
            mgr.resume_session.return_value = (child_manifest, handoff_result)
            # session module also imports SessionManager via its own namespace
            mock_mgr_cls_sess.return_value = mgr

            result = runner.invoke(
                main,
                ["session", "resume", "p1", "--fresh", "--review", "--child-name", "child-1"],
            )

        assert result.exit_code == 0, result.output
        # Editor was invoked with the per-child file path
        assert len(editor_calls) == 1
        assert editor_calls[0][0] == str(fake_editor)
        assert editor_calls[0][1] == str(child_file)

    def test_editor_command_with_args_is_split(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.session import create_session_state
        from forge.session.handoff import HandoffResult

        fake_editor = tmp_path / "fake-editor"
        fake_editor.write_text("#!/bin/sh\nexit 0\n")
        fake_editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", f"{fake_editor} --wait")

        parent = create_session_state(
            "p1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(tmp_path),
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        child_file = tmp_path / ".forge" / "prev_sessions" / "p1" / "children" / "child-1.md"
        child_file.parent.mkdir(parents=True)
        child_file.write_text("# Context")

        child_manifest = create_session_state(
            "child-1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="p1",
            worktree_path=str(tmp_path),
        )

        handoff_result = HandoffResult(
            context_file=child_file,
            context_file_rel=".forge/prev_sessions/p1/children/child-1.md",
            transcript_artifact_path=None,
            token_estimate=None,
            lineage=["p1"],
        )

        editor_calls: list[list[str]] = []

        def fake_subprocess_run(args, *_, **__):
            editor_calls.append(list(args))
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with (
            patch("forge.cli.session_lifecycle.SessionManager") as mock_mgr_cls,
            patch("forge.cli.session_lifecycle._launch_claude_for_session", return_value=0),
            patch("forge.cli.session_lifecycle.subprocess.run", side_effect=fake_subprocess_run),
            patch("forge.cli.session.SessionManager") as mock_mgr_cls_sess,
        ):
            mgr = mock_mgr_cls.return_value
            mgr.get_session.return_value = parent
            mgr.resume_session.return_value = (child_manifest, handoff_result)
            mock_mgr_cls_sess.return_value = mgr

            result = runner.invoke(
                main,
                ["session", "resume", "p1", "--fresh", "--review", "--child-name", "child-1"],
            )

        assert result.exit_code == 0, result.output
        assert editor_calls == [[str(fake_editor), "--wait", str(child_file)]]

    def test_editor_nonzero_aborts_launch(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If editor exits non-zero (user aborted), launch is skipped."""
        from forge.session import create_session_state
        from forge.session.handoff import HandoffResult

        fake_editor = tmp_path / "fake-editor"
        fake_editor.write_text("#!/bin/sh\nexit 1\n")
        fake_editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(fake_editor))

        parent = create_session_state(
            "p1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(tmp_path),
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        child_file = tmp_path / ".forge" / "prev_sessions" / "p1" / "children" / "child-1.md"
        child_file.parent.mkdir(parents=True)
        child_file.write_text("# Context")

        child_manifest = create_session_state(
            "child-1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="p1",
            worktree_path=str(tmp_path),
        )

        handoff_result = HandoffResult(
            context_file=child_file,
            context_file_rel=".forge/prev_sessions/p1/children/child-1.md",
            transcript_artifact_path=None,
            token_estimate=None,
            lineage=["p1"],
        )

        with (
            patch("forge.cli.session_lifecycle.SessionManager") as mock_mgr_cls,
            patch("forge.cli.session_lifecycle._launch_claude_for_session", return_value=0) as launch_mock,
            patch("forge.cli.session.SessionManager") as mock_mgr_cls_sess,
        ):
            mgr = mock_mgr_cls.return_value
            mgr.get_session.return_value = parent
            mgr.resume_session.return_value = (child_manifest, handoff_result)
            mock_mgr_cls_sess.return_value = mgr

            result = runner.invoke(
                main,
                ["session", "resume", "p1", "--fresh", "--review", "--child-name", "child-1"],
            )

        # Aborted -- launch was never invoked
        assert result.exit_code != 0
        launch_mock.assert_not_called()
        assert "Aborted" in result.output
        assert "forge session resume child-1" in result.output

    def test_missing_editor_exits_with_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.session import create_session_state
        from forge.session.handoff import HandoffResult

        # Set $EDITOR to a clearly-missing binary
        monkeypatch.setenv("EDITOR", "/nonexistent/never-installed-editor-99")

        parent = create_session_state(
            "p1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(tmp_path),
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        child_file = tmp_path / ".forge" / "prev_sessions" / "p1" / "children" / "child-1.md"
        child_file.parent.mkdir(parents=True)
        child_file.write_text("# Context")

        child_manifest = create_session_state(
            "child-1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="p1",
            worktree_path=str(tmp_path),
        )

        handoff_result = HandoffResult(
            context_file=child_file,
            context_file_rel=".forge/prev_sessions/p1/children/child-1.md",
            transcript_artifact_path=None,
            token_estimate=None,
            lineage=["p1"],
        )

        with (
            patch("forge.cli.session_lifecycle.SessionManager") as mock_mgr_cls,
            patch("forge.cli.session_lifecycle._launch_claude_for_session", return_value=0),
            patch("forge.cli.session.SessionManager") as mock_mgr_cls_sess,
        ):
            mgr = mock_mgr_cls.return_value
            mgr.get_session.return_value = parent
            mgr.resume_session.return_value = (child_manifest, handoff_result)
            mock_mgr_cls_sess.return_value = mgr

            result = runner.invoke(
                main,
                ["session", "resume", "p1", "--fresh", "--review", "--child-name", "child-1"],
            )

        assert result.exit_code != 0
        assert "Editor" in result.output and "not found" in result.output


class TestReviewRelaunch:
    def test_unlaunched_review_child_reuses_persisted_context(self, tmp_path: Path) -> None:
        from forge.cli.session_lifecycle import _launch_in_place
        from forge.session import SessionStore, create_session_state
        from forge.session.models import Derivation

        child_file = tmp_path / ".forge" / "prev_sessions" / "p1" / "children" / "child-1.md"
        child_file.parent.mkdir(parents=True)
        child_file.write_text("# Curated context")

        child_manifest = create_session_state(
            "child-1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="p1",
            worktree_path=str(tmp_path),
        )
        child_manifest.forge_root = str(tmp_path)
        child_manifest.confirmed.derivation = Derivation(
            parent_session="p1",
            resume_mode="handoff",
            context_file=".forge/prev_sessions/p1/children/child-1.md",
        )
        SessionStore(str(tmp_path), "child-1").write(child_manifest)

        manager = MagicMock()
        manager.index_store.sync_uuid_from_state = MagicMock()

        launch_calls: list[str | None] = []

        def fake_launch(**kwargs):
            launch_calls.append(kwargs["system_prompt_file"])
            return 0

        with (
            patch("forge.cli.session_lifecycle._launch_claude_for_session", side_effect=fake_launch),
            patch("forge.cli.session_lifecycle._persist_routing_override"),
            patch("forge.cli.session_lifecycle._get_effective_proxy_for_session", return_value=(None, None, None)),
            patch("forge.cli.session._resolve_context_limit", return_value=200000),
        ):
            with pytest.raises(SystemExit) as exc:
                _launch_in_place(manager=manager, name="child-1", manifest=child_manifest)

        assert exc.value.code == 0
        assert launch_calls == [str(child_file.resolve())]

    def test_legacy_flat_context_path_is_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from forge.cli.session_lifecycle import _resolve_derivation_context_file
        from forge.session import create_session_state
        from forge.session.models import Derivation

        legacy_file = tmp_path / ".forge" / "prev_sessions" / "p1.md"
        legacy_file.parent.mkdir(parents=True)
        legacy_file.write_text("# Legacy context")

        child_manifest = create_session_state(
            "child-1",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="p1",
            worktree_path=str(tmp_path),
        )
        child_manifest.forge_root = str(tmp_path)
        child_manifest.confirmed.derivation = Derivation(
            parent_session="p1",
            resume_mode="handoff",
            context_file=".forge/prev_sessions/p1.md",
        )

        with pytest.raises(SystemExit) as exc:
            _resolve_derivation_context_file(child_manifest)

        output = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Legacy handoff artifact format is no longer supported" in output
        assert "forge session resume p1 --fresh" in output
