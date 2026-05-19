"""Regression: fork launch + handoff must preserve parent context across modes.

Bug ID: 21x-fork-launch-handoff
Root cause:
- Structured handoff only understood requestId/message.role transcripts and
  dropped request-less legacy entries, yielding "No conversation content found."
- launch() treated untouched same-directory forks created with --no-launch as
  fresh sessions instead of completing Claude's native --resume --fork-session.
- Worktree fork handoff reused stale .forge/prev_sessions files instead of
  regenerating context from the current parent transcript.
Fix:
- Normalize transcript roles/content blocks across transcript formats and group
  request-less entries into turns.
- Detect deferred same-directory forks in launch() and relaunch them with
  Claude's native fork flow.
- Regenerate parent handoff for loadable worktree parents instead of trusting
  stale cached context files.
Affected files: src/forge/session/handoff.py, src/forge/cli/session.py
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.cli.main import main
from forge.cli.session import _generate_parent_handoff_context
from forge.session import SessionManager, SessionStore, create_session_state
from forge.session.handoff import ResumeStrategy, process_handoff
from forge.session.hooks import HookInput, handle_session_start

pytestmark = pytest.mark.regression


@pytest.fixture
def runner() -> CliRunner:
    """CLI runner for session command regressions."""
    return CliRunner()


@pytest.fixture
def session_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal git project for CLI regression tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".forge").mkdir()
    monkeypatch.chdir(project)

    return project


def test_structured_handoff_handles_requestless_legacy_entries(tmp_path: Path) -> None:
    """Worktree-style handoff should summarize legacy entries without request IDs."""
    parent_dir = tmp_path / "parent-worktree"
    fork_dir = tmp_path / "fork-worktree"
    parent_dir.mkdir()
    fork_dir.mkdir()

    transcript = parent_dir / "legacy-parent.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2025-01-15T10:00:00Z",
                        "message": {"content": [{"type": "text", "text": "legacy hello from parent"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2025-01-15T10:00:01Z",
                        "message": {"content": [{"type": "text", "text": "legacy response from assistant"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    parent_state = create_session_state(
        "legacy-parent",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=str(parent_dir),
        worktree_branch="main",
    )
    parent_state.confirmed.transcript_path = str(transcript)

    result = process_handoff(
        parent_name="legacy-parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        output_root=fork_dir,
        strategy=ResumeStrategy.STRUCTURED,
        depth=1,
        get_session=lambda _: None,
    )

    assert result.context_file is not None
    content = result.context_file.read_text(encoding="utf-8")
    assert "legacy hello from parent" in content
    assert "legacy response from assistant" in content
    assert "*Transcript not available.*" not in content
    assert "no valid turns" not in " ".join(result.warnings).lower()


def test_launch_same_dir_fork_created_with_no_launch_uses_native_fork_session(
    runner: CliRunner,
    session_project: Path,
) -> None:
    """Deferred same-directory forks should relaunch through Claude's native fork flow."""
    start_result = runner.invoke(main, ["session", "start", "fork-parent", "--no-launch"])
    assert start_result.exit_code == 0

    # UUID is hook-owned; simulate hook confirmation so fork can use --resume
    manager = SessionManager()
    store = manager.get_session_store("fork-parent")
    store.update(timeout_s=5.0, mutate=lambda m: setattr(m.confirmed, "claude_session_id", "parent-uuid-001"))

    fork_result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--no-launch"])
    assert fork_result.exit_code == 0

    parent_manifest = SessionManager().get_session("fork-parent")
    assert parent_manifest.confirmed.claude_session_id is not None

    with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
        launch_result = runner.invoke(main, ["session", "resume", "fork-child"])

    assert launch_result.exit_code == 0
    assert "Fork parent Claude conversation" in launch_result.output
    assert mock_invoke.call_args is not None
    kwargs = mock_invoke.call_args.kwargs
    assert kwargs["resume_id"] == parent_manifest.confirmed.claude_session_id
    assert kwargs["fork_session"] is True
    assert kwargs["session_id"] is None
    assert kwargs["system_prompt_file"] is None


def test_deferred_same_dir_fork_session_start_reconciles_child_uuid(
    runner: CliRunner,
    session_project: Path,
) -> None:
    """A real child SessionStart should reconcile the fork manifest with the child UUID.

    This is the closest deterministic equivalent to the manual QA flow:
    after a deferred same-directory fork is launched with ``--resume --fork-session``,
    Claude eventually materializes a child session and emits SessionStart for that
    child. Forge should attach that UUID to the fork manifest, not leave it null and
    not overwrite the parent UUID.
    """
    start_result = runner.invoke(main, ["session", "start", "fork-parent", "--no-launch"])
    assert start_result.exit_code == 0

    # UUID is hook-owned; simulate hook confirmation so fork can use --resume
    manager = SessionManager()
    parent_store = manager.get_session_store("fork-parent")
    parent_store.update(timeout_s=5.0, mutate=lambda m: setattr(m.confirmed, "claude_session_id", "parent-uuid-002"))

    fork_result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--no-launch"])
    assert fork_result.exit_code == 0

    parent_manifest = manager.get_session("fork-parent")
    child_store = manager.get_session_store("fork-child")
    child_manifest = child_store.read()

    assert parent_manifest.confirmed.claude_session_id is not None
    assert child_manifest.confirmed.claude_session_id is None

    # Simulate the first real child turn after `session resume fork-child`.
    hook_input = HookInput(
        session_id="child-uuid-456",
        transcript_path="/tmp/fork-child-transcript.jsonl",
        source="startup",
    )

    with patch.dict("os.environ", {"FORGE_SESSION": "fork-child"}, clear=True):
        result = handle_session_start(hook_input, session_project)

    assert result.success
    assert result.session_name == "fork-child"

    updated_child = child_store.read()
    assert updated_child.confirmed.claude_session_id == "child-uuid-456"
    assert updated_child.confirmed.transcript_path == "/tmp/fork-child-transcript.jsonl"
    assert updated_child.confirmed.confirmed_by == "hook:SessionStart:startup"
    assert updated_child.confirmed.claude_session_id != parent_manifest.confirmed.claude_session_id


def test_worktree_fork_handoff_regenerates_stale_context(tmp_path: Path) -> None:
    """Worktree forks regenerate parent cache; legacy flat stale files are ignored.

    Under the per-parent-directory layout the fork's launch-time context file
    lives at ``<fork>/.forge/prev_sessions/<parent>/children/<fork_name>.md``.
    Legacy pre-0.2.0 flat ``<parent>.md`` files at the top of ``prev_sessions/``
    are no longer read; new code writes only to the new path.
    """
    parent_dir = tmp_path / "parent-worktree"
    fork_dir = tmp_path / "fork-worktree"
    parent_dir.mkdir()
    fork_dir.mkdir()

    transcript = parent_dir / "parent-transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "requestId": "r1",
                "timestamp": "2025-01-15T10:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "fresh context from transcript"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    parent_state = create_session_state(
        "stale-parent",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=str(parent_dir),
        worktree_branch="main",
    )
    parent_state.confirmed.transcript_path = str(transcript)

    SessionStore(str(parent_dir), "stale-parent").write(parent_state)
    manager = SessionManager()
    manager.index_store.add_from_state(parent_state, str(parent_dir))

    # Legacy stale flat files at the OLD location. New code ignores these
    # entirely; they remain on disk until GC sweeps them.
    legacy_parent_flat = parent_dir / ".forge" / "prev_sessions" / "stale-parent.md"
    legacy_parent_flat.parent.mkdir(parents=True, exist_ok=True)
    legacy_parent_flat.write_text("legacy parent (should be ignored)\n", encoding="utf-8")

    legacy_fork_flat = fork_dir / ".forge" / "prev_sessions" / "stale-parent.md"
    legacy_fork_flat.parent.mkdir(parents=True, exist_ok=True)
    legacy_fork_flat.write_text("legacy fork (should be ignored)\n", encoding="utf-8")

    fork_state = create_session_state(
        "stale-child",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        parent_session="stale-parent",
        is_fork=True,
        worktree_path=str(fork_dir),
        worktree_branch="stale-child",
    )
    assert fork_state.worktree is not None
    fork_state.worktree.is_worktree = True

    context_path, warnings = _generate_parent_handoff_context(manager=manager, manifest=fork_state)

    expected_child = fork_dir / ".forge" / "prev_sessions" / "stale-parent" / "children" / "stale-child.md"
    assert context_path is not None
    assert context_path == expected_child.resolve()
    content = expected_child.read_text(encoding="utf-8")
    assert "fresh context from transcript" in content
    assert "legacy parent" not in content
    assert "legacy fork" not in content
    assert "Transcript not available" not in content
    assert warnings == []

    # Legacy flat files are NOT modified or removed by handoff -- that is GC's job.
    assert legacy_parent_flat.is_file()
    assert legacy_fork_flat.is_file()


def test_worktree_fork_handoff_writes_to_nested_forge_root(tmp_path: Path) -> None:
    """Nested Forge project forks should write prev_sessions under forge_root."""
    parent_dir = tmp_path / "parent-worktree"
    fork_dir = tmp_path / "fork-worktree"
    nested_forge_root = fork_dir / "packages" / "app"
    parent_dir.mkdir()
    nested_forge_root.mkdir(parents=True)

    parent_state = create_session_state(
        "parent",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=str(parent_dir),
        worktree_branch="main",
    )
    parent_state.forge_root = str(parent_dir)

    manager = SessionManager()
    SessionStore(str(parent_dir), "parent").write(parent_state)
    manager.index_store.add_from_state(parent_state, str(parent_dir))

    fork_state = create_session_state(
        "child",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        parent_session="parent",
        is_fork=True,
        worktree_path=str(fork_dir),
        worktree_branch="child",
    )
    assert fork_state.worktree is not None
    fork_state.worktree.is_worktree = True
    fork_state.forge_root = str(nested_forge_root)

    context_path, warnings = _generate_parent_handoff_context(
        manager=manager,
        manifest=fork_state,
        parent_state=parent_state,
    )

    expected_child = nested_forge_root / ".forge" / "prev_sessions" / "parent" / "children" / "child.md"
    assert context_path == expected_child.resolve()
    assert expected_child.is_file()
    assert not (fork_dir / ".forge" / "prev_sessions" / "parent" / "children" / "child.md").exists()
    assert warnings == []
