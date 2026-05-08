"""CLI regressions for session subprocess proxy routing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.cli.main import main
from forge.session import create_session_state


@pytest.fixture
def temp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COLUMNS", "500")

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".forge").mkdir()
    monkeypatch.chdir(project)
    return project


def test_fork_threads_subprocess_proxy_env(temp_project: Path) -> None:
    """Fork launch env should preserve an inherited subprocess proxy."""
    parent = create_session_state(
        "fork-parent",
        worktree_path=str(temp_project),
        worktree_branch="main",
    )
    parent.confirmed.claude_session_id = "parent-uuid"

    fork_state = create_session_state(
        "fork-child",
        parent_session="fork-parent",
        is_fork=True,
        worktree_path=str(temp_project),
    )
    fork_state.intent.subprocess_proxy = "openrouter"

    with (
        patch("forge.cli.session.SessionManager") as mock_manager_cls,
        patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
    ):
        mock_manager = mock_manager_cls.return_value
        mock_manager.fork_session.return_value = (parent, fork_state)

        result = CliRunner().invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child"])

    assert result.exit_code == 0, result.output
    kwargs = mock_invoke.call_args.kwargs
    assert kwargs["env_vars"]["FORGE_SUBPROCESS_PROXY"] == "openrouter"
