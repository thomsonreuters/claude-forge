"""Regression: resume auto-name retry must retarget child handoff context."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.session import SessionManager, SessionStore, create_session_state
from forge.session.exceptions import SessionExistsError
from forge.session.prev_sessions import child_path, child_path_rel

pytestmark = pytest.mark.regression


def test_resume_autoname_retry_updates_context_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    parent = create_session_state(
        "parent",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=str(project),
    )
    parent.forge_root = str(project)

    manager = SessionManager()
    SessionStore(str(project), "parent").write(parent)
    manager.index_store.add_from_state(
        parent,
        str(project),
        checkout_root=str(project),
        forge_root=str(project),
        relative_path=".",
    )

    original_add = manager.index_store.add_from_state
    failed_once = False

    def fail_base_name_once(state, *args, **kwargs):
        nonlocal failed_once
        if state.name == "parent-resumed" and not failed_once:
            failed_once = True
            manager.index_store.add_session(
                name="parent-resumed",
                worktree_path=str(project),
                project_root=str(project),
                forge_root=str(project),
                checkout_root=str(project),
                relative_path=".",
                is_incognito=False,
                is_fork=False,
                parent_session="parent",
            )
            raise SessionExistsError("parent-resumed")
        return original_add(state, *args, **kwargs)

    manager.index_store.add_from_state = fail_base_name_once  # type: ignore[method-assign]

    child, handoff = manager.resume_session("parent")

    assert failed_once is True
    assert child.name.startswith("parent-resumed-")
    expected_rel = child_path_rel("parent", child.name)
    expected_path = child_path(project, "parent", child.name)
    original_path = child_path(project, "parent", "parent-resumed")
    assert child.confirmed.derivation is not None
    assert child.confirmed.derivation.context_file == expected_rel
    assert handoff.context_file_rel == expected_rel
    assert handoff.context_file == expected_path
    assert expected_path.is_file()
    assert not original_path.exists()

    persisted = SessionStore(str(project), child.name).read()
    assert persisted.confirmed.derivation is not None
    assert persisted.confirmed.derivation.context_file == expected_rel
