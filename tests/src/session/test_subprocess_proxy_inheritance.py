"""Regression tests for session subprocess proxy inheritance."""

from __future__ import annotations

import subprocess
from pathlib import Path

from forge.session import SessionManager, SessionStore


def _init_forge_project(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    (path / ".claude").mkdir()
    (path / ".forge").mkdir()


def _set_subprocess_proxy(project: Path, name: str, proxy_id: str) -> None:
    store = SessionStore(str(project), name)
    store.update(timeout_s=5.0, mutate=lambda m: setattr(m.intent, "subprocess_proxy", proxy_id))


def test_resume_child_inherits_subprocess_proxy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_forge_project(project)
    manager = SessionManager()
    manager.start_session(name="parent", worktree_path=str(project), direct=True)
    _set_subprocess_proxy(project, "parent", "openrouter")

    child, _handoff = manager.resume_session("parent", child_name="child")

    assert child.intent.subprocess_proxy == "openrouter"
    assert SessionStore(str(project), "child").read().intent.subprocess_proxy == "openrouter"


def test_fork_child_inherits_subprocess_proxy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_forge_project(project)
    manager = SessionManager()
    manager.start_session(name="parent", worktree_path=str(project), direct=True)
    _set_subprocess_proxy(project, "parent", "openrouter")

    _parent, fork = manager.fork_session("parent", "fork")

    assert fork.intent.subprocess_proxy == "openrouter"
    assert SessionStore(str(project), "fork").read().intent.subprocess_proxy == "openrouter"


def test_relaunch_child_inherits_subprocess_proxy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_forge_project(project)
    manager = SessionManager()
    manager.start_session(name="parent", worktree_path=str(project), direct=True)
    _set_subprocess_proxy(project, "parent", "openrouter")

    _parent, child = manager.relaunch_session("parent", child_name="child")

    assert child.intent.subprocess_proxy == "openrouter"
    assert SessionStore(str(project), "child").read().intent.subprocess_proxy == "openrouter"
