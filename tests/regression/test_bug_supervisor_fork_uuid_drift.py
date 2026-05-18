"""Regression: Stop must reconcile native fork UUID drift before guard cleanup.

Bug ID: supervisor-fork-uuid-drift
Root cause:
- A same-directory fork launched with ``--resume --fork-session`` could receive
  the parent Claude UUID during SessionStart.
- Stop later carried the real child UUID and transcript, but Forge only copied
  the transcript artifact and left the fork manifest/index pointing at the
  parent conversation.
- Deleting the fork could then delete the parent transcript and semantic
  supervisor checks could resume the wrong conversation.
Fix:
- Stop and StopFailure treat the hook payload session_id/transcript_path as
  authoritative and sync both manifest and index.
Affected files: src/forge/cli/hooks/commands.py, src/forge/guard/semantic/supervisor.py
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.cli.hooks import hooks
from forge.cli.main import main
from forge.session import SessionManager
from forge.session.hooks import HookInput, handle_session_start
from forge.session.index import IndexStore

pytestmark = pytest.mark.regression


@pytest.fixture
def session_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal Forge project with isolated global state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".forge").mkdir()
    monkeypatch.chdir(project)
    return project


def test_native_fork_stop_reconciles_child_uuid_in_manifest_and_index(
    session_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child Stop event should replace a stale parent UUID on same-dir forks."""
    runner = CliRunner()

    start_result = runner.invoke(main, ["session", "start", "guard-planner", "--no-launch"])
    assert start_result.exit_code == 0

    manager = SessionManager()
    parent_store = manager.get_session_store("guard-planner")
    parent_store.update(timeout_s=5.0, mutate=lambda m: setattr(m.confirmed, "claude_session_id", "parent-uuid"))

    fork_result = runner.invoke(
        main,
        ["session", "fork", "guard-planner", "--name", "guard-supervisor", "--no-launch"],
    )
    assert fork_result.exit_code == 0

    child_store = manager.get_session_store("guard-supervisor")
    child_manifest = child_store.read()
    assert child_manifest.is_fork is True
    assert child_manifest.parent_session == "guard-planner"
    assert child_manifest.confirmed.claude_session_id is None

    with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
        resume_result = runner.invoke(main, ["session", "resume", "guard-supervisor"])

    assert resume_result.exit_code == 0
    assert mock_invoke.call_args is not None
    assert mock_invoke.call_args.kwargs["resume_id"] == "parent-uuid"
    assert mock_invoke.call_args.kwargs["fork_session"] is True

    # Reproduce the drift: SessionStart records the inherited parent UUID for
    # the fork target before Stop reports the materialized child conversation.
    monkeypatch.setenv("FORGE_SESSION", "guard-supervisor")
    monkeypatch.setenv("FORGE_FORGE_ROOT", str(session_project))
    start_hook = handle_session_start(
        HookInput(
            session_id="parent-uuid",
            transcript_path=str(session_project / "parent.jsonl"),
            source="startup",
        ),
        session_project,
    )
    assert start_hook.success
    assert child_store.read().confirmed.claude_session_id == "parent-uuid"
    assert IndexStore().get_session("guard-supervisor", forge_root=str(session_project)).claude_session_id == (
        "parent-uuid"
    )

    child_transcript = session_project / "child.jsonl"
    child_transcript.write_text("{}\n", encoding="utf-8")
    stop_payload = {
        "hook_event_name": "Stop",
        "session_id": "child-uuid",
        "transcript_path": str(child_transcript),
    }

    stop_result = runner.invoke(hooks, ["stop"], input=json.dumps(stop_payload))

    assert stop_result.exit_code == 0
    stop_output = json.loads(stop_result.output)
    assert stop_output["success"] is True

    updated = child_store.read()
    assert updated.confirmed.claude_session_id == "child-uuid"
    assert updated.confirmed.transcript_path == str(child_transcript)
    assert updated.confirmed.confirmed_by == "hook:stop"
    assert IndexStore().get_session("guard-supervisor", forge_root=str(session_project)).claude_session_id == (
        "child-uuid"
    )
