"""Regression: process_handoff output_root separates read/write paths.

Bug ID: 21x-handoff-output-root
Root cause: _generate_parent_handoff_context passed the fork's worktree as
project_root to process_handoff. Transcript artifacts live under the parent's
worktree, so the lookup failed and the handoff file was generated with no
conversation content.
Fix: Added output_root parameter to process_handoff so transcript lookup uses
the parent's project_root while the context file is written to the fork's
output_root.
Affected files: src/forge/session/handoff.py, src/forge/cli/session.py
"""

import pytest

from forge.session.handoff import ResumeStrategy, process_handoff
from forge.session.models import SessionState

pytestmark = pytest.mark.regression


def _minimal_parent_state(worktree_path: str) -> SessionState:
    from forge.session import create_session_state

    state = create_session_state(
        "parent",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=worktree_path,
    )
    state.confirmed.claude_session_id = "test-uuid"
    return state


def test_output_root_writes_context_to_separate_directory(tmp_path):
    """Context file should be written to output_root, not project_root."""
    parent_dir = tmp_path / "parent-worktree"
    fork_dir = tmp_path / "fork-worktree"
    parent_dir.mkdir()
    fork_dir.mkdir()

    parent_state = _minimal_parent_state(str(parent_dir))

    result = process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        output_root=fork_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
    )

    assert result.context_file is not None
    assert result.context_file.is_file()
    # Written to fork dir, not parent dir
    assert str(fork_dir) in str(result.context_file)
    assert str(parent_dir) not in str(result.context_file)
    assert (fork_dir / ".forge" / "prev_sessions" / "parent" / "generated.md").is_file()
    assert not (parent_dir / ".forge" / "prev_sessions" / "parent" / "generated.md").exists()


def test_output_root_none_defaults_to_project_root(tmp_path):
    """When output_root is None, context file goes to project_root (existing behavior)."""
    parent_dir = tmp_path / "worktree"
    parent_dir.mkdir()

    parent_state = _minimal_parent_state(str(parent_dir))

    result = process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        output_root=None,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
    )

    assert result.context_file is not None
    assert (parent_dir / ".forge" / "prev_sessions" / "parent" / "generated.md").is_file()
