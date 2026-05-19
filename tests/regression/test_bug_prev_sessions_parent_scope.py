"""Regression: resume context artifacts are child-scoped, not parent-scoped.

Bug ID: prev-sessions-parent-scope
Root cause: process_handoff wrote a single flat ``.forge/prev_sessions/<parent>.md``
file shared across every child derived from the parent. The child manifest
stored that overwriteable path in ``Derivation.context_file``. A subsequent
resume/fork from the same parent silently rewrote the file -- any user
curation between sessions was lost, and the first child's manifest still
pointed at content that no longer represented its state.
Fix: Split into ``<parent>/generated.md`` (regeneratable cache) and
``<parent>/children/<child>.md`` (per-child authoritative file). Each child
gets its own durable file; regenerating the parent cache never disturbs an
existing child file.
Affected files: src/forge/session/handoff.py, src/forge/session/manager.py,
src/forge/session/prev_sessions.py, src/forge/core/ops/gc.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.session import create_session_state
from forge.session.handoff import ResumeStrategy, process_handoff
from forge.session.models import SessionState
from forge.session.prev_sessions import child_path, generated_path

pytestmark = pytest.mark.regression


def _parent_state(worktree_path: str) -> SessionState:
    state = create_session_state(
        "parent",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=worktree_path,
    )
    state.confirmed.claude_session_id = "test-uuid"
    return state


def test_two_children_get_independent_files(tmp_path: Path) -> None:
    """Two children of the same parent each get their own per-child file."""
    parent_dir = tmp_path / "parent-worktree"
    parent_dir.mkdir()

    parent_state = _parent_state(str(parent_dir))

    # First resume: creates generated.md + children/child-a.md
    result_a = process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
        child_name="child-a",
    )
    assert result_a.context_file == child_path(parent_dir, "parent", "child-a")
    assert generated_path(parent_dir, "parent").is_file()
    assert child_path(parent_dir, "parent", "child-a").is_file()

    # Second resume: creates children/child-b.md, regenerates generated.md
    result_b = process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
        child_name="child-b",
    )
    assert result_b.context_file == child_path(parent_dir, "parent", "child-b")
    assert child_path(parent_dir, "parent", "child-a").is_file()
    assert child_path(parent_dir, "parent", "child-b").is_file()

    # Files are at distinct paths
    assert result_a.context_file != result_b.context_file


def test_user_edits_to_child_survive_sibling_resume(tmp_path: Path) -> None:
    """Editing one child's file is not clobbered by another child's --fresh resume.

    This is the load-bearing durability guarantee that motivates the
    per-child layout.
    """
    parent_dir = tmp_path / "parent-worktree"
    parent_dir.mkdir()

    parent_state = _parent_state(str(parent_dir))

    # Resume into child-a
    process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
        child_name="child-a",
    )
    child_a_file = child_path(parent_dir, "parent", "child-a")
    # User curates child-a between resumes
    child_a_file.write_text("USER CURATION ON CHILD A\n", encoding="utf-8")
    assert "USER CURATION" in child_a_file.read_text()

    # Resume into child-b -- regenerates parent cache, must not touch child-a
    process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
        child_name="child-b",
    )

    # User edits in child-a survive
    assert child_a_file.read_text() == "USER CURATION ON CHILD A\n"
    # child-b is fresh (does NOT contain child-a's user curation)
    child_b_content = child_path(parent_dir, "parent", "child-b").read_text()
    assert "USER CURATION" not in child_b_content


def test_existing_child_is_not_clobbered_by_re_resume(tmp_path: Path) -> None:
    """ensure_child is idempotent -- existing child files survive re-resume."""
    parent_dir = tmp_path / "parent-worktree"
    parent_dir.mkdir()

    parent_state = _parent_state(str(parent_dir))

    process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
        child_name="child-a",
    )
    child_file = child_path(parent_dir, "parent", "child-a")
    child_file.write_text("CURATED\n", encoding="utf-8")

    # Re-resume with same child name (not realistic from CLI but tests idempotency)
    process_handoff(
        parent_name="parent",
        parent_state=parent_state,
        forge_root=parent_dir,
        strategy=ResumeStrategy.MINIMAL,
        depth=1,
        get_session=lambda _: None,
        child_name="child-a",
    )

    assert child_file.read_text() == "CURATED\n"
