"""Tests for CLI session commands.

These tests use Click's CliRunner to test commands without invoking
the real Claude binary.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from rich.console import Console

import forge.cli.session as session_cli
from forge.cli.main import main
from forge.session import IndexStore, SessionManager, SessionStore, create_session_state
from forge.session.active import ActiveSessionStore
from forge.session.config import LAUNCH_MODE_HOST
from forge.session.exceptions import DirtyWorktreeError, SessionNotFoundError
from forge.session.models import Derivation, StartedWithProxy, SystemPromptIntent


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_scoped_duplicate_sessions(project: Path) -> tuple[Path, Path]:
    index = IndexStore()

    forge_root_a = project
    forge_root_b = project / "nested-project"
    forge_root_b.mkdir(parents=True, exist_ok=True)

    worktree_a = project
    worktree_b = project / "nested-project-checkout"
    worktree_b.mkdir(parents=True, exist_ok=True)

    manifest_a = create_session_state(
        "shared",
        proxy_template="template-a",
        proxy_base_url="http://localhost:8101",
        worktree_path=str(worktree_a),
    )
    manifest_a.forge_root = str(forge_root_a)
    SessionStore(str(forge_root_a), "shared").write(manifest_a)

    manifest_b = create_session_state(
        "shared",
        proxy_template="template-b",
        proxy_base_url="http://localhost:8102",
        worktree_path=str(worktree_b),
    )
    manifest_b.forge_root = str(forge_root_b)
    SessionStore(str(forge_root_b), "shared").write(manifest_b)

    index.add_session(
        name="shared",
        worktree_path=str(worktree_a),
        project_root=str(project),
        forge_root=str(forge_root_a),
        checkout_root=str(worktree_a),
        relative_path=".",
    )
    index.add_session(
        name="shared",
        worktree_path=str(worktree_b),
        project_root=str(project),
        forge_root=str(forge_root_b),
        checkout_root=str(worktree_b),
        relative_path="nested-project",
    )

    return forge_root_a, forge_root_b


def _set_index_age(name: str, forge_root: Path, days: int) -> None:
    IndexStore().update_session(name, last_accessed_at=_iso_days_ago(days), forge_root=str(forge_root))


def test_resume_token_estimate_multiplier_skips_proxy_config_lookup(temp_env: Path) -> None:
    """Proxy-routed resume checks use the default tokenizer heuristic in v1."""
    from forge.cli import session_lifecycle

    parent = create_session_state("parent", worktree_path=str(temp_env))

    with patch("forge.config.loader.load_proxy_instance_config", side_effect=AssertionError("unexpected proxy I/O")):
        multiplier = session_lifecycle._resume_token_estimate_multiplier(
            parent_state=parent,
            effective_proxy_ref="litellm-anthropic",
        )

    assert multiplier == 1.0


def test_resume_token_estimate_multiplier_uses_direct_pin(temp_env: Path) -> None:
    """Direct 4.7 resume checks keep the model-specific tokenizer margin."""
    from forge.cli import session_lifecycle

    parent = create_session_state("parent", worktree_path=str(temp_env), direct_model="claude-opus-4-7")

    multiplier = session_lifecycle._resume_token_estimate_multiplier(
        parent_state=parent,
        effective_proxy_ref=None,
    )

    assert multiplier == 1.35


def _set_manifest_age(forge_root: Path, name: str, days: int) -> None:
    store = SessionStore(str(forge_root), name)

    def _mutate(state) -> None:
        state.last_accessed_at = _iso_days_ago(days)

    store.update(timeout_s=5.0, mutate=_mutate)


def _age_session(forge_root: Path, name: str, days: int) -> None:
    _set_index_age(name, forge_root, days)
    _set_manifest_age(forge_root, name, days)


def _read_session_manifest(forge_root: Path, name: str):
    return SessionStore(str(forge_root), name).read()


def _write_session_manifest(forge_root: Path, name: str, state) -> None:
    SessionStore(str(forge_root), name).write(state)


def _seed_supervised_duplicate_sessions(project: Path) -> tuple[Path, Path]:
    forge_root_a, forge_root_b = _seed_scoped_duplicate_sessions(project)

    target_a = _read_session_manifest(forge_root_a, "shared")
    target_a.confirmed.claude_session_id = "uuid-alpha"
    target_a.confirmed.started_with_proxy = StartedWithProxy(base_url="http://localhost:8101", template="template-a")
    _write_session_manifest(forge_root_a, "shared", target_a)

    target_b = _read_session_manifest(forge_root_b, "shared")
    target_b.confirmed.claude_session_id = "uuid-beta"
    target_b.confirmed.started_with_proxy = StartedWithProxy(base_url="http://localhost:8102", template="template-b")
    _write_session_manifest(forge_root_b, "shared", target_b)

    return forge_root_a, forge_root_b


def _seed_supervise_source_session(project: Path, forge_root: Path) -> None:
    source = create_session_state(
        "controller",
        proxy_template="controller-template",
        proxy_base_url="http://localhost:8110",
        worktree_path=str(project),
    )
    source.forge_root = str(forge_root)
    source.intent.policy.supervisor.resume_id = "shared"  # type: ignore[union-attr]
    SessionStore(str(forge_root), "controller").write(source)
    IndexStore().add_session(
        name="controller",
        worktree_path=str(project),
        project_root=str(project),
        forge_root=str(forge_root),
        checkout_root=str(project),
        relative_path=".",
    )


def _seed_session_for_supervise(project: Path, forge_root: Path, name: str = "controller") -> None:
    state = create_session_state(
        name,
        proxy_template="controller-template",
        proxy_base_url="http://localhost:8110",
        worktree_path=str(project),
    )
    state.forge_root = str(forge_root)
    SessionStore(str(forge_root), name).write(state)
    IndexStore().add_session(
        name=name,
        worktree_path=str(project),
        project_root=str(project),
        forge_root=str(forge_root),
        checkout_root=str(project),
        relative_path=".",
    )


def _read_policy_supervisor(forge_root: Path, name: str) -> str | None:
    manifest = SessionStore(str(forge_root), name).read()
    policy = manifest.intent.policy
    if policy and policy.supervisor:
        return policy.supervisor.resume_id
    return None


def _read_policy_source_model_output(result_output: str) -> bool:
    return "Source model" in result_output


def _read_policy_uuid_output(result_output: str) -> bool:
    return "Claude UUID" in result_output


def _seed_cleanup_session(project: Path, forge_root: Path, name: str = "old-session") -> None:
    state = create_session_state(
        name,
        proxy_template="cleanup-template",
        proxy_base_url="http://localhost:8120",
        worktree_path=str(project),
    )
    state.forge_root = str(forge_root)
    SessionStore(str(forge_root), name).write(state)
    IndexStore().add_session(
        name=name,
        worktree_path=str(project),
        project_root=str(project),
        forge_root=str(forge_root),
        checkout_root=str(project),
        relative_path=".",
    )
    _age_session(forge_root, name, 60)


def _seed_duplicate_list_sessions(project: Path) -> tuple[Path, Path]:
    forge_root_a, forge_root_b = _seed_scoped_duplicate_sessions(project)
    _age_session(forge_root_a, "shared", 60)
    _age_session(forge_root_b, "shared", 5)
    return forge_root_a, forge_root_b


class _BrokenActiveSessionStore:
    def list_sessions(self):
        raise RuntimeError("registry unreadable")


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI runner."""
    return CliRunner()


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up temporary environment for tests."""
    # Create temp home directory
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Widen Rich's console so long absolute paths don't wrap mid-string
    # (breaks substring assertions; macOS tmp paths are long).
    monkeypatch.setenv("COLUMNS", "500")

    # Create temp project directory with .git and .forge (Rule 1)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".forge").mkdir()

    # Change to project directory
    monkeypatch.chdir(project)

    return project


class TestSessionList:
    """Tests for 'forge session list' command."""

    def test_list_empty_shows_message(self, runner: CliRunner, temp_env: Path) -> None:
        """Should show message when no sessions exist."""
        result = runner.invoke(main, ["session", "list"])

        assert result.exit_code == 0
        assert "No sessions found" in result.output

    def test_list_shows_sessions(self, runner: CliRunner, temp_env: Path) -> None:
        """Should list existing sessions."""
        # Create a session first (mock invoke_claude)
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "test-session"])

        result = runner.invoke(main, ["session", "list"])

        assert result.exit_code == 0
        assert "test-session" in result.output

    def test_list_older_than_filters_by_scoped_identity(self, runner: CliRunner, temp_env: Path) -> None:
        """--older-than should not pull in same-name sessions from a different forge_root."""
        forge_root_a, forge_root_b = _seed_duplicate_list_sessions(temp_env)

        result = runner.invoke(main, ["session", "list", "--older-than", "30", "--scope", "repo"])

        assert result.exit_code == 0
        assert result.output.count("shared") == 1
        assert "nested-project" not in result.output
        assert str(forge_root_b.name) not in result.output

    def test_list_disambiguates_duplicate_names_in_human_output(self, runner: CliRunner, temp_env: Path) -> None:
        """Duplicate display names should show a location column for humans."""
        _seed_scoped_duplicate_sessions(temp_env)

        result = runner.invoke(main, ["session", "list", "--scope", "repo"])

        assert result.exit_code == 0
        assert "LOCATION" in result.output
        assert result.output.count("shared") == 2
        assert "nested-project" in result.output

    def test_clean_reports_active_registry_failure(self, runner: CliRunner, temp_env: Path) -> None:
        """Cleanup should surface active-registry failures instead of claiming nothing matched."""
        _seed_cleanup_session(temp_env, temp_env)

        with patch("forge.session.cleanup.ActiveSessionStore", return_value=_BrokenActiveSessionStore()):
            result = runner.invoke(main, ["session", "clean", "--older-than", "30"])

        assert result.exit_code == 1
        assert "Session cleanup aborted before evaluation completed" in result.output
        assert "registry unreadable" in result.output
        assert "No sessions older than 30 days found." not in result.output
        assert "Encountered 1 cleanup failure" in result.output
        assert "active session registry" in result.output
        assert SessionStore(str(temp_env), "old-session").exists()

    def test_clean_dry_run_warns_when_active_registry_unreadable(self, runner: CliRunner, temp_env: Path) -> None:
        """Dry-run should warn that real cleanup would abort when registry reads fail."""
        _seed_cleanup_session(temp_env, temp_env)

        with patch("forge.session.active.ActiveSessionStore", return_value=_BrokenActiveSessionStore()):
            result = runner.invoke(main, ["session", "clean", "--older-than", "30", "--dry-run"])

        assert result.exit_code == 0
        assert "Could not read active session registry" in result.output
        assert "Actual cleanup would abort" in result.output
        assert "old-session" in result.output
        assert "unreadable" not in result.output  # keep dry-run wording user-facing
        assert SessionStore(str(temp_env), "old-session").exists()

    def test_clean_older_than_reports_nothing_when_registry_healthy_and_no_matches(
        self, runner: CliRunner, temp_env: Path
    ) -> None:
        """Healthy cleanup with no old sessions should keep the existing no-op message."""
        result = runner.invoke(main, ["session", "clean", "--older-than", "30"])

        assert result.exit_code == 0
        assert "No sessions older than 30 days found." in result.output


class TestSessionShow:
    """Tests for 'forge session show' command."""

    def test_show_no_session(self, runner: CliRunner, temp_env: Path) -> None:
        """Should show message when no session specified and no FORGE_SESSION."""
        result = runner.invoke(main, ["session", "show"])

        assert result.exit_code == 0
        assert "No session specified" in result.output

    def test_show_named_session(self, runner: CliRunner, temp_env: Path) -> None:
        """Should show detailed session info by name."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "inspect-test"])

        result = runner.invoke(main, ["session", "show", "inspect-test"])

        assert result.exit_code == 0
        assert "inspect-test" in result.output
        assert "UUID" in result.output or "Basic Info" in result.output

    def test_show_nonexistent_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail for nonexistent session."""
        result = runner.invoke(main, ["session", "show", "nonexistent"])

        assert result.exit_code == 1
        assert "No session found" in result.output

    def test_show_json_output(self, runner: CliRunner, temp_env: Path) -> None:
        """--json should output merged manifest + context as JSON."""
        import json

        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "json-test"])

        result = runner.invoke(main, ["session", "show", "json-test", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["session_name"] == "json-test"
        assert "intent" in data
        assert "context" in data
        assert "model_family" in data["context"]

    def test_show_field_extraction(self, runner: CliRunner, temp_env: Path) -> None:
        """--field should extract a single value."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "field-test"])

        result = runner.invoke(main, ["session", "show", "field-test", "--field", "session_name"])

        assert result.exit_code == 0
        assert "field-test" in result.output

    def test_show_field_nested(self, runner: CliRunner, temp_env: Path) -> None:
        """--field with dot notation should extract nested values."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "nested-test"])

        result = runner.invoke(main, ["session", "show", "nested-test", "--field", "context.model_family"])

        assert result.exit_code == 0
        assert result.output.strip()  # Should output some value

    def test_show_env_fallback(self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should resolve from $FORGE_SESSION when no argument given."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "env-test"])

        monkeypatch.setenv("FORGE_SESSION", "env-test")
        result = runner.invoke(main, ["session", "show"])

        assert result.exit_code == 0
        assert "env-test" in result.output

    def test_show_computed_context_section(self, runner: CliRunner, temp_env: Path) -> None:
        """Human-readable output should include Computed Context section."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "context-test"])

        result = runner.invoke(main, ["session", "show", "context-test"])

        assert result.exit_code == 0
        assert "Computed Context" in result.output
        assert "Model Family" in result.output


def _seed_bare_session(project: Path, name: str) -> Path:
    """Write a minimal same-project session and register it in the index."""
    state = create_session_state(name, worktree_path=str(project))
    state.forge_root = str(project)
    SessionStore(str(project), name).write(state)
    IndexStore().add_session(
        name=name,
        worktree_path=str(project),
        project_root=str(project),
        forge_root=str(project),
        checkout_root=str(project),
        relative_path=".",
    )
    return project


def _mutate_manifest(project: Path, name: str, mutate) -> None:
    store = SessionStore(str(project), name)
    state = store.read()
    mutate(state)
    store.write(state)


def _write_plan_file(project: Path, relative_path: str, content: str = "# Plan") -> Path:
    """Create an on-disk plan file so displayed-path existence checks pass.

    Returns the canonicalized path (``dest.resolve()``) so assertions match
    what `Path.resolve()` produces in the CLI display (macOS normalizes
    ``/var`` -> ``/private/var``).
    """
    dest = project / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    return dest.resolve()


class TestSessionShowPlanInfo:
    """Tests for plan info surfacing in `forge session show` and `--field`."""

    def test_show_displays_plan_draft_when_populated(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "planner")
        draft = _write_plan_file(temp_env, ".claude/plans/my-plan.md")

        def _set_plan(state):
            state.confirmed.latest_plan_path = ".claude/plans/my-plan.md"

        _mutate_manifest(temp_env, "planner", _set_plan)

        result = runner.invoke(main, ["session", "show", "planner"])

        assert result.exit_code == 0
        assert f"Plan (draft): {draft}" in result.output
        assert "file missing" not in result.output

    def test_show_resolves_nested_project_draft_against_launch_root(
        self,
        runner: CliRunner,
        temp_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkout_root = temp_env
        nested_forge_root = temp_env / "nested"
        nested_forge_root.mkdir()
        draft = _write_plan_file(nested_forge_root, ".claude/plans/nested-plan.md")

        state = create_session_state("planner", worktree_path=str(checkout_root))
        state.forge_root = str(nested_forge_root)
        state.confirmed.latest_plan_path = ".claude/plans/nested-plan.md"
        SessionStore(str(nested_forge_root), "planner").write(state)
        IndexStore().add_session(
            name="planner",
            worktree_path=str(checkout_root),
            project_root=str(checkout_root),
            forge_root=str(nested_forge_root),
            checkout_root=str(checkout_root),
            relative_path="nested",
        )

        monkeypatch.chdir(nested_forge_root)
        result = runner.invoke(main, ["session", "show", "planner"])

        assert result.exit_code == 0
        assert f"Plan (draft): {draft}" in result.output
        assert "file missing" not in result.output

    def test_show_displays_approved_snapshots(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "planner")
        snap = _write_plan_file(temp_env, ".forge/artifacts/planner/plans/x.md")

        def _set_snaps(state):
            state.confirmed.artifacts["plans"] = [
                {
                    "kind": "approved",
                    "captured_at": "2026-04-16T12:00:00Z",
                    "source_path": ".claude/plans/p.md",
                    "snapshot_path": ".forge/artifacts/planner/plans/x.md",
                }
            ]

        _mutate_manifest(temp_env, "planner", _set_snaps)

        result = runner.invoke(main, ["session", "show", "planner"])

        assert result.exit_code == 0
        assert "Plans approved: 1" in result.output
        assert str(snap) in result.output
        assert "file missing" not in result.output

    def test_show_displays_missing_file_annotation(self, runner: CliRunner, temp_env: Path) -> None:
        """When the snapshot is recorded but the file is gone, surface it explicitly."""
        _seed_bare_session(temp_env, "planner")

        def _set_snaps(state):
            state.confirmed.artifacts["plans"] = [
                {
                    "kind": "approved",
                    "captured_at": "2026-04-16T12:00:00Z",
                    "source_path": ".claude/plans/p.md",
                    "snapshot_path": ".forge/artifacts/planner/plans/gone.md",
                }
            ]

        _mutate_manifest(temp_env, "planner", _set_snaps)

        result = runner.invoke(main, ["session", "show", "planner"])

        assert result.exit_code == 0
        assert "Plans approved: 1" in result.output
        assert "gone.md" in result.output
        assert "file missing" in result.output

    def test_show_omits_plan_section_when_empty(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "solo")

        result = runner.invoke(main, ["session", "show", "solo"])

        assert result.exit_code == 0
        assert "Plan (" not in result.output
        assert "Plans approved" not in result.output

    def test_show_displays_inherited_plan_from_parent_via_derivation(self, runner: CliRunner, temp_env: Path) -> None:
        """Resume sessions populate confirmed.derivation; child should see parent's plan."""
        _seed_bare_session(temp_env, "planner")
        _seed_bare_session(temp_env, "executor")
        draft = _write_plan_file(temp_env, ".claude/plans/parent-plan.md")

        def _set_parent_plan(state):
            state.confirmed.latest_plan_path = ".claude/plans/parent-plan.md"

        def _set_child_derivation(state):
            state.confirmed.derivation = Derivation(parent_session="planner")

        _mutate_manifest(temp_env, "planner", _set_parent_plan)
        _mutate_manifest(temp_env, "executor", _set_child_derivation)

        result = runner.invoke(main, ["session", "show", "executor"])

        assert result.exit_code == 0
        assert f"Plan (inherited from planner, draft): {draft}" in result.output
        assert "file missing" not in result.output

    def test_show_displays_inherited_plan_for_real_fork(self, runner: CliRunner, temp_env: Path) -> None:
        """Fork sessions use top-level parent_session (no derivation). Must still surface parent plan."""
        # Seed parent with an approved snapshot
        _seed_bare_session(temp_env, "planner")
        snap = _write_plan_file(temp_env, ".forge/artifacts/planner/plans/x.md")

        def _set_parent_plan(state):
            state.confirmed.artifacts["plans"] = [
                {
                    "kind": "approved",
                    "captured_at": "2026-04-16T12:00:00Z",
                    "source_path": ".claude/plans/p.md",
                    "snapshot_path": ".forge/artifacts/planner/plans/x.md",
                }
            ]

        _mutate_manifest(temp_env, "planner", _set_parent_plan)

        # Create a fork child the real way: top-level parent_session + is_fork=True.
        fork_state = create_session_state(
            "executor",
            parent_session="planner",
            is_fork=True,
            worktree_path=str(temp_env),
        )
        fork_state.forge_root = str(temp_env)
        SessionStore(str(temp_env), "executor").write(fork_state)
        IndexStore().add_session(
            name="executor",
            worktree_path=str(temp_env),
            project_root=str(temp_env),
            forge_root=str(temp_env),
            checkout_root=str(temp_env),
            relative_path=".",
        )

        result = runner.invoke(main, ["session", "show", "executor"])

        assert result.exit_code == 0
        assert "Plan (inherited from planner, approved snapshot)" in result.output
        assert str(snap) in result.output
        assert "file missing" not in result.output

    def test_show_prefers_approved_snapshot_over_draft_for_self(self, runner: CliRunner, temp_env: Path) -> None:
        """When a session has both a draft and an approved snapshot, show both lines."""
        _seed_bare_session(temp_env, "planner")
        _write_plan_file(temp_env, ".claude/plans/stale-draft.md")
        _write_plan_file(temp_env, ".forge/artifacts/planner/plans/x.md")

        def _set(state):
            state.confirmed.latest_plan_path = ".claude/plans/stale-draft.md"
            state.confirmed.artifacts["plans"] = [
                {
                    "kind": "approved",
                    "captured_at": "2026-04-16T12:00:00Z",
                    "source_path": ".claude/plans/p.md",
                    "snapshot_path": ".forge/artifacts/planner/plans/x.md",
                }
            ]

        _mutate_manifest(temp_env, "planner", _set)

        result = runner.invoke(main, ["session", "show", "planner"])

        assert result.exit_code == 0
        # Approved snapshot line appears BEFORE draft line so the approved path is the first thing the user sees.
        approved_idx = result.output.index("Plans approved:")
        draft_idx = result.output.index("Plan (draft):")
        assert approved_idx < draft_idx
        assert "file missing" not in result.output

    def test_show_prefers_approved_snapshot_when_inherited(self, runner: CliRunner, temp_env: Path) -> None:
        """Parent with both draft and approved: inherited line points at approved."""
        _seed_bare_session(temp_env, "planner")
        _write_plan_file(temp_env, ".claude/plans/stale-draft.md")
        approved = _write_plan_file(temp_env, ".forge/artifacts/planner/plans/real.md")

        def _set_parent(state):
            state.confirmed.latest_plan_path = ".claude/plans/stale-draft.md"
            state.confirmed.artifacts["plans"] = [
                {
                    "kind": "approved",
                    "captured_at": "2026-04-16T12:00:00Z",
                    "source_path": ".claude/plans/p.md",
                    "snapshot_path": ".forge/artifacts/planner/plans/real.md",
                }
            ]

        _mutate_manifest(temp_env, "planner", _set_parent)

        fork_state = create_session_state(
            "executor",
            parent_session="planner",
            is_fork=True,
            worktree_path=str(temp_env),
        )
        fork_state.forge_root = str(temp_env)
        SessionStore(str(temp_env), "executor").write(fork_state)
        IndexStore().add_session(
            name="executor",
            worktree_path=str(temp_env),
            project_root=str(temp_env),
            forge_root=str(temp_env),
            checkout_root=str(temp_env),
            relative_path=".",
        )

        result = runner.invoke(main, ["session", "show", "executor"])

        assert result.exit_code == 0
        assert "Plan (inherited from planner, approved snapshot)" in result.output
        assert str(approved) in result.output
        assert "stale-draft" not in result.output
        assert "file missing" not in result.output

    def test_show_json_exposes_confirmed_plan_fields(self, runner: CliRunner, temp_env: Path) -> None:
        import json

        _seed_bare_session(temp_env, "planner")

        def _set(state):
            state.confirmed.latest_plan_path = ".claude/plans/foo.md"
            state.confirmed.artifacts["plans"] = [{"kind": "approved", "snapshot_path": "snap.md"}]

        _mutate_manifest(temp_env, "planner", _set)

        result = runner.invoke(main, ["session", "show", "planner", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["confirmed"]["latest_plan_path"] == ".claude/plans/foo.md"
        assert data["confirmed"]["artifacts"]["plans"][0]["snapshot_path"] == "snap.md"
        assert data["plan"]["preferred_path"] == "snap.md"
        assert data["plan"]["kind"] == "approved"

    def test_show_json_confirmed_derivation_present(self, runner: CliRunner, temp_env: Path) -> None:
        import json

        _seed_bare_session(temp_env, "planner")
        _seed_bare_session(temp_env, "executor")

        def _set(state):
            state.confirmed.derivation = Derivation(
                parent_session="planner",
                parent_forge_root=str(temp_env),
            )

        _mutate_manifest(temp_env, "executor", _set)

        result = runner.invoke(main, ["session", "show", "executor", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["confirmed"]["derivation"]["parent_session"] == "planner"
        assert data["confirmed"]["derivation"]["parent_forge_root"] == str(temp_env)

    def test_show_json_surfaces_inherited_plan(self, runner: CliRunner, temp_env: Path) -> None:
        import json

        _seed_bare_session(temp_env, "planner")
        _seed_bare_session(temp_env, "executor")

        def _set_parent(state):
            state.confirmed.artifacts["plans"] = [
                {
                    "kind": "approved",
                    "snapshot_path": ".forge/artifacts/planner/plans/real.md",
                }
            ]

        def _set_child(state):
            state.confirmed.derivation = Derivation(parent_session="planner")

        _mutate_manifest(temp_env, "planner", _set_parent)
        _mutate_manifest(temp_env, "executor", _set_child)

        result = runner.invoke(main, ["session", "show", "executor", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["plan"]["source"] == "parent"
        assert data["plan"]["parent_session"] == "planner"
        assert data["plan"]["preferred_path"] == ".forge/artifacts/planner/plans/real.md"
        assert data["plan"]["kind"] == "approved"

    def test_show_field_extraction_plan_path(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "planner")

        def _set(state):
            state.confirmed.latest_plan_path = ".claude/plans/foo.md"

        _mutate_manifest(temp_env, "planner", _set)

        result = runner.invoke(main, ["session", "show", "planner", "--field", "confirmed.latest_plan_path"])

        assert result.exit_code == 0
        assert result.output.strip() == ".claude/plans/foo.md"

    def test_show_field_extraction_inherited_plan_path(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "planner")
        _seed_bare_session(temp_env, "executor")

        def _set_parent(state):
            state.confirmed.latest_plan_path = ".claude/plans/parent-plan.md"

        def _set_child(state):
            state.confirmed.derivation = Derivation(parent_session="planner")

        _mutate_manifest(temp_env, "planner", _set_parent)
        _mutate_manifest(temp_env, "executor", _set_child)

        result = runner.invoke(main, ["session", "show", "executor", "--field", "plan.preferred_path"])

        assert result.exit_code == 0
        assert result.output.strip() == ".claude/plans/parent-plan.md"

    def test_show_field_extraction_none_returns_empty(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "planner")

        result = runner.invoke(main, ["session", "show", "planner", "--field", "confirmed.latest_plan_path"])

        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_show_field_extraction_missing_path_errors(self, runner: CliRunner, temp_env: Path) -> None:
        _seed_bare_session(temp_env, "planner")

        result = runner.invoke(main, ["session", "show", "planner", "--field", "confirmed.nonexistent_field"])

        assert result.exit_code == 1
        assert "not found" in result.output


class TestSessionShowPolicy:
    """Tests for confirmed.policy exposure in session show."""

    def test_show_json_includes_confirmed_policy(self, runner: CliRunner, temp_env: Path) -> None:
        import json

        from forge.session.models import PolicyConfirmed

        _seed_bare_session(temp_env, "executor")

        def _set(state):
            state.confirmed.policy = PolicyConfirmed(
                forge_version="0.1.0",
                bundles=[],
                rules_active=["semantic.supervisor"],
                decisions=[
                    {"final_decision": "allow", "context_summary": "Edit:src/foo.py"},
                ],
            )

        _mutate_manifest(temp_env, "executor", _set)

        result = runner.invoke(main, ["session", "show", "executor", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["confirmed"]["policy"] is not None
        assert data["confirmed"]["policy"]["rules_active"] == ["semantic.supervisor"]
        assert len(data["confirmed"]["policy"]["decisions"]) == 1
        assert data["confirmed"]["policy"]["decisions"][0]["final_decision"] == "allow"

    def test_show_json_confirmed_policy_null_when_empty(self, runner: CliRunner, temp_env: Path) -> None:
        import json

        _seed_bare_session(temp_env, "bare")

        result = runner.invoke(main, ["session", "show", "bare", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["confirmed"]["policy"] is None

    def test_show_field_confirmed_policy_decisions(self, runner: CliRunner, temp_env: Path) -> None:
        from forge.session.models import PolicyConfirmed

        _seed_bare_session(temp_env, "executor")

        def _set(state):
            state.confirmed.policy = PolicyConfirmed(
                decisions=[
                    {"final_decision": "deny", "context_summary": "Write:src/bar.py"},
                    {"final_decision": "allow", "context_summary": "Edit:src/bar.py"},
                ],
            )

        _mutate_manifest(temp_env, "executor", _set)

        result = runner.invoke(main, ["session", "show", "executor", "--field", "confirmed.policy.decisions"])

        assert result.exit_code == 0
        assert "deny" in result.output
        assert "allow" in result.output

    def test_show_human_includes_policy_evals(self, runner: CliRunner, temp_env: Path) -> None:
        from forge.session.models import PolicyConfirmed

        _seed_bare_session(temp_env, "executor")

        def _set(state):
            state.confirmed.policy = PolicyConfirmed(
                decisions=[
                    {"final_decision": "allow", "context_summary": "Edit:src/foo.py"},
                    {"final_decision": "deny", "context_summary": "Write:src/bar.py"},
                    {"final_decision": "allow", "context_summary": "Edit:src/baz.py"},
                ],
            )

        _mutate_manifest(temp_env, "executor", _set)

        result = runner.invoke(main, ["session", "show", "executor"])

        assert result.exit_code == 0
        assert "Policy Evals:" in result.output
        assert "3 evaluations" in result.output


class TestSessionStart:
    """Tests for 'forge session start' command."""

    def test_start_creates_session(self, runner: CliRunner, temp_env: Path) -> None:
        """Should create a new session."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "start", "new-session"])

        assert result.exit_code == 0
        assert "Created session" in result.output
        assert "new-session" in result.output

    def test_start_tracks_active_session_during_launch(self, runner: CliRunner, temp_env: Path) -> None:
        """Active-session registry should be present during launch and cleared after exit."""
        captured: dict[str, str | bool | None] = {}

        def fake_invoke(*_args, **_kwargs):
            entry = ActiveSessionStore().get_session("tracked-start")
            captured["was_active"] = entry is not None
            captured["session_id"] = entry.claude_session_id if entry else None
            return 0

        with patch("forge.cli.session.invoke_claude", side_effect=fake_invoke):
            result = runner.invoke(main, ["session", "start", "tracked-start"])

        assert result.exit_code == 0
        assert captured["was_active"] is True
        assert isinstance(captured["session_id"], str)
        assert ActiveSessionStore().get_session("tracked-start") is None

    def test_start_defaults_to_direct(self, runner: CliRunner, temp_env: Path) -> None:
        """No flags should default to direct mode."""
        result = runner.invoke(main, ["session", "start", "direct-default", "--no-launch"])

        assert result.exit_code == 0
        assert "Routing: direct" in result.output

        manager = SessionManager()
        manifest = manager.get_session_store("direct-default").read()
        assert manifest.intent.proxy is None

    def test_start_direct_creates_session_without_proxy(self, runner: CliRunner, temp_env: Path) -> None:
        """--no-proxy should create a session with no proxy intent."""
        result = runner.invoke(main, ["session", "start", "direct-test", "--no-proxy", "--no-launch"])

        assert result.exit_code == 0
        assert "Routing: direct" in result.output

        manager = SessionManager()
        manifest = manager.get_session_store("direct-test").read()
        assert manifest.intent.proxy is None

    def test_start_worktree_uses_nested_forge_roots_for_extension_inheritance(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nested Forge projects should inherit/install extensions at nested roots."""
        parent_nested_root = temp_env / "packages" / "app"
        parent_nested_root.mkdir(parents=True)
        (parent_nested_root / ".forge").mkdir()
        monkeypatch.chdir(parent_nested_root)

        worktree_root = temp_env / "wt-nested"
        worktree_root.mkdir()
        child_nested_root = worktree_root / "packages" / "app"
        child_nested_root.mkdir(parents=True)

        manifest = create_session_state(
            "wt-nested",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(worktree_root),
            worktree_branch="wt-nested",
        )
        assert manifest.worktree is not None
        manifest.worktree.is_worktree = True
        manifest.forge_root = str(child_nested_root)

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session._auto_install_extensions") as mock_auto,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.start_session.return_value = manifest

            result = runner.invoke(main, ["session", "start", "wt-nested", "--worktree", "--no-launch"])

        assert result.exit_code == 0
        mock_auto.assert_called_once_with(
            install_root=child_nested_root,
            parent_project_root=parent_nested_root,
            force_extensions=None,
        )

    def test_start_with_model(self, runner: CliRunner, temp_env: Path) -> None:
        """--model pins direct Claude sessions through env vars."""
        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "start", "model-test", "--model", "opus-4-7"])

        assert result.exit_code == 0
        assert mock_invoke.call_args is not None
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["model"] is None
        assert kwargs["env_vars"]["ANTHROPIC_MODEL"] == "opus"
        assert kwargs["env_vars"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-7"

        state = SessionStore(str(temp_env), "model-test").read()
        assert state.intent.launch is not None
        assert state.intent.launch.direct_model == "claude-opus-4-7"

    def test_start_with_model_no_launch_stores_normalized_pin(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(
            main,
            ["session", "start", "model-no-launch", "--model", "claude-opus-4-7[1m]", "--no-launch"],
        )

        assert result.exit_code == 0
        state = SessionStore(str(temp_env), "model-no-launch").read()
        assert state.intent.launch is not None
        assert state.intent.launch.direct_model == "claude-opus-4-7[1m]"

    def test_start_with_sonnet_model_sets_sonnet_env(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(
                main,
                ["session", "start", "sonnet-model", "--model", "claude-sonnet-4-6[1m]"],
            )

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["env_vars"]["ANTHROPIC_MODEL"] == "sonnet"
        assert kwargs["env_vars"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-4-6[1m]"

    def test_start_with_model_accepts_subprocess_proxy(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(
                main,
                ["session", "start", "model-subprocess", "--model", "opus-4-7", "--subprocess-proxy", "openrouter"],
            )

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["env_vars"]["FORGE_SUBPROCESS_PROXY"] == "openrouter"
        assert kwargs["env_vars"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-7"

    @pytest.mark.parametrize("flag", ["--proxy", "--sidecar", "--host-proxy"])
    def test_start_with_model_rejects_proxy_routing_flags(
        self,
        runner: CliRunner,
        temp_env: Path,
        flag: str,
    ) -> None:
        args = ["session", "start", "bad-model", "--model", "opus-4-7", flag]
        if flag == "--proxy":
            args.append("litellm-openai")

        result = runner.invoke(main, args)

        assert result.exit_code == 1
        assert "--model" in result.output

    def test_start_with_unknown_model_rejects_before_create(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "start", "bad-model", "--model", "claude-opus-4.7.1"])

        assert result.exit_code == 1
        assert "Unknown direct Claude model" in result.output
        assert not SessionStore(str(temp_env), "bad-model").exists()

    def test_start_duplicate_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail when session already exists."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "duplicate-test"])

        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "start", "duplicate-test"])

        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_start_without_name_auto_generates(self, runner: CliRunner, temp_env: Path) -> None:
        """Should auto-generate a name when none provided."""
        with (
            patch("forge.cli.session.generate_unique_name", return_value="auto-test-session"),
            patch("forge.cli.session.invoke_claude", return_value=0),
        ):
            result = runner.invoke(main, ["session", "start"])

        assert result.exit_code == 0
        assert "auto-test-session" in result.output

    def test_start_without_name_direct(self, runner: CliRunner, temp_env: Path) -> None:
        """Auto-name should work with --no-proxy flag."""
        with patch("forge.cli.session.generate_unique_name", return_value="auto-direct"):
            result = runner.invoke(main, ["session", "start", "--no-proxy", "--no-launch"])

        assert result.exit_code == 0
        assert "auto-direct" in result.output
        assert "Routing: direct" in result.output

    def test_start_help_shows_optional_name(self, runner: CliRunner) -> None:
        """Click should render [NAME] for optional argument."""
        result = runner.invoke(main, ["session", "start", "--help"])

        assert result.exit_code == 0
        assert "[NAME]" in result.output

    def test_start_sidecar_persists_launch_preferences(self, runner: CliRunner, temp_env: Path) -> None:
        """Sidecar start should persist relaunch image/mount settings."""
        result = runner.invoke(
            main,
            [
                "session",
                "start",
                "sidecar-test",
                "--sidecar",
                "--mount",
                "/data:/mnt/data:ro",
                "--image",
                "forge-sidecar:test",
                "--no-launch",
            ],
        )

        assert result.exit_code == 0

        manager = SessionManager()
        manifest = manager.get_session_store("sidecar-test").read()
        assert manifest.intent.launch is not None
        assert manifest.intent.launch.mode == "sidecar"
        assert manifest.intent.launch.sidecar is not None
        assert manifest.intent.launch.sidecar.mounts == ["/data:/mnt/data:ro"]
        assert manifest.intent.launch.sidecar.image == "forge-sidecar:test"


class TestSessionDelete:
    """Tests for 'forge session delete' command."""

    def test_delete_removes_session(self, runner: CliRunner, temp_env: Path) -> None:
        """Should delete the session."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "delete-test"])

        result = runner.invoke(main, ["session", "delete", "delete-test", "--yes"])

        assert result.exit_code == 0
        assert "Deleted session" in result.output

    def test_delete_nonexistent_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail for nonexistent session."""
        result = runner.invoke(main, ["session", "delete", "nonexistent", "--yes"])

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_delete_prompts_without_yes(self, runner: CliRunner, temp_env: Path) -> None:
        """Should prompt for confirmation without --yes."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "confirm-test"])

        # Simulate 'n' response to confirmation
        result = runner.invoke(main, ["session", "delete", "confirm-test"], input="n\n")

        assert "Cancelled" in result.output

    def test_delete_warns_when_session_is_active(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete confirmation should warn when runtime state says the session is live."""
        runner.invoke(main, ["session", "start", "active-delete", "--no-launch"])
        ActiveSessionStore().upsert_session(
            "active-delete",
            worktree_path=str(temp_env),
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=os.getpid(),
            claude_session_id="uuid-live-123",
        )

        result = runner.invoke(main, ["session", "delete", "active-delete"], input="n\n")

        assert result.exit_code == 0
        assert "appears to still be active" in result.output
        assert "Launcher PID" in result.output
        assert "Cancelled" in result.output

    def test_delete_yes_shows_warning_but_skips_prompt(self, runner: CliRunner, temp_env: Path) -> None:
        """--yes shows active-session warning (informational) but skips confirmation."""
        runner.invoke(main, ["session", "start", "forced-active-delete", "--no-launch"])
        ActiveSessionStore().upsert_session(
            "forced-active-delete",
            worktree_path=str(temp_env),
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=os.getpid(),
        )

        result = runner.invoke(main, ["session", "delete", "forced-active-delete", "--yes"])

        assert result.exit_code == 0
        assert "appears to still be active" in result.output
        assert "Deleted session" in result.output

    def test_delete_multiple_sessions(self, runner: CliRunner, temp_env: Path) -> None:
        """Should delete multiple sessions in one command."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "multi-1"])
            runner.invoke(main, ["session", "start", "multi-2"])
            runner.invoke(main, ["session", "start", "multi-3"])

        result = runner.invoke(main, ["session", "delete", "multi-1", "multi-2", "multi-3", "--yes"])

        assert result.exit_code == 0
        assert "Deleted session" in result.output
        assert "multi-1" in result.output
        assert "multi-2" in result.output
        assert "multi-3" in result.output
        assert "3 deleted" in result.output

        manager = SessionManager()
        assert not manager.session_exists("multi-1")
        assert not manager.session_exists("multi-2")
        assert not manager.session_exists("multi-3")

    def test_delete_all_sessions(self, runner: CliRunner, temp_env: Path) -> None:
        """--all should delete every session."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "all-1"])
            runner.invoke(main, ["session", "start", "all-2"])

        result = runner.invoke(main, ["session", "delete", "--all", "--yes"])

        assert result.exit_code == 0
        assert "Deleted session" in result.output
        assert "2 deleted" in result.output

        manager = SessionManager()
        assert len(manager.list_sessions()) == 0

    def test_delete_all_with_names_errors(self, runner: CliRunner, temp_env: Path) -> None:
        """--all with explicit names should error."""
        result = runner.invoke(main, ["session", "delete", "--all", "some-name", "--yes"])

        assert result.exit_code == 1
        assert "Cannot combine --all" in result.output

    def test_delete_no_args_errors(self, runner: CliRunner, temp_env: Path) -> None:
        """No names and no --all should error."""
        result = runner.invoke(main, ["session", "delete"])

        assert result.exit_code == 1
        assert "Provide session name(s) or use --all" in result.output

    def test_delete_all_empty_is_noop(self, runner: CliRunner, temp_env: Path) -> None:
        """--all with no sessions should show a message and succeed."""
        result = runner.invoke(main, ["session", "delete", "--all", "--yes"])

        assert result.exit_code == 0
        assert "No sessions to delete" in result.output

    def test_delete_partial_failure(self, runner: CliRunner, temp_env: Path) -> None:
        """Should continue deleting after a failure and report summary."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "exists-1"])

        result = runner.invoke(main, ["session", "delete", "exists-1", "nonexistent", "--yes"])

        assert result.exit_code == 1
        assert "Deleted session" in result.output
        assert "exists-1" in result.output
        assert "not found" in result.output
        assert "1 deleted" in result.output
        assert "1 failed" in result.output

    def test_delete_all_prompts_without_yes(self, runner: CliRunner, temp_env: Path) -> None:
        """--all without --yes should prompt with session list."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "prompt-1"])
            runner.invoke(main, ["session", "start", "prompt-2"])

        result = runner.invoke(main, ["session", "delete", "--all"], input="n\n")

        assert "all 2 session(s)" in result.output
        assert "Cancelled" in result.output

    def test_delete_all_warns_about_active_sessions(self, runner: CliRunner, temp_env: Path) -> None:
        """--all confirmation should summarize live sessions before deletion."""
        runner.invoke(main, ["session", "start", "all-active-1", "--no-launch"])
        runner.invoke(main, ["session", "start", "all-active-2", "--no-launch"])
        ActiveSessionStore().upsert_session(
            "all-active-2",
            worktree_path=str(temp_env),
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=os.getpid(),
        )

        result = runner.invoke(main, ["session", "delete", "--all"], input="n\n")

        assert result.exit_code == 0
        assert "appear to still be active" in result.output
        assert "all-active-2" in result.output
        assert "Cancelled" in result.output

    def test_delete_dirty_worktree_shows_force_tip(self, runner: CliRunner, temp_env: Path) -> None:
        """Single-session dirty worktree failures should keep the force guidance."""
        with patch("forge.cli.session._delete_single_session", side_effect=DirtyWorktreeError("/tmp/wt")):
            result = runner.invoke(main, ["session", "delete", "dirty-sess", "--yes"])

        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "/tmp/wt" in result.output
        assert "Use --force to remove anyway, or commit/stash your changes first." in result.output

    def test_delete_single_session_not_found_uses_cli_error_format(self, runner: CliRunner, temp_env: Path) -> None:
        """Single-session ForgeSessionError should preserve standard CLI formatting."""
        with patch(
            "forge.cli.session._delete_single_session",
            side_effect=SessionNotFoundError("missing-sess"),
        ):
            result = runner.invoke(main, ["session", "delete", "missing-sess", "--yes"])

        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "missing-sess" in result.output
        assert "Deleted session" not in result.output

    def test_delete_multi_session_forge_error_uses_per_target_summary(self, runner: CliRunner, temp_env: Path) -> None:
        """Multi-session ForgeSessionError should be reported per target without aborting immediately."""
        with patch("forge.cli.session._delete_single_session") as mock_delete:
            mock_delete.side_effect = [None, SessionNotFoundError("missing-sess")]
            result = runner.invoke(main, ["session", "delete", "ok-sess", "missing-sess", "--yes"])

        assert result.exit_code == 1
        assert "Deleted session" in result.output
        assert "ok-sess" in result.output
        assert "missing-sess" in result.output
        assert "1 deleted" in result.output
        assert "1 failed" in result.output
        assert "Error:" in result.output

    def test_delete_cross_project_resolves(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete from wrong forge_root should resolve cross-project and succeed."""
        forge_root_a, forge_root_b = _seed_scoped_duplicate_sessions(temp_env)

        # CWD is temp_env (forge_root_a), but delete the session scoped to forge_root_b
        other_name = "other-project-sess"
        other_manifest = create_session_state(
            other_name,
            proxy_template="t",
            proxy_base_url="http://localhost:9999",
            worktree_path=str(forge_root_b),
        )
        other_manifest.forge_root = str(forge_root_b)
        SessionStore(str(forge_root_b), other_name).write(other_manifest)
        IndexStore().add_session(
            name=other_name,
            worktree_path=str(forge_root_b),
            project_root=str(temp_env),
            forge_root=str(forge_root_b),
        )

        result = runner.invoke(main, ["session", "delete", other_name, "--yes"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert "nested-project" in result.output  # cross-project note

    def test_delete_cross_project_ambiguous_shows_all_roots(self, runner: CliRunner, temp_env: Path) -> None:
        """Delete of a duplicate name from wrong project should list all locations."""
        _seed_scoped_duplicate_sessions(temp_env)

        # "shared" exists in forge_root_a (temp_env) and forge_root_b (temp_env/nested-project).
        # Create a third forge_root where "shared" does NOT exist, and run delete from there.
        forge_root_c = temp_env / "other-project"
        forge_root_c.mkdir(parents=True, exist_ok=True)
        (forge_root_c / ".forge").mkdir(parents=True, exist_ok=True)

        # Patch _cwd_forge_root and os.getcwd so the orphan check
        # (SessionStore at Path.cwd()) doesn't find the session on disk.
        with (
            patch("forge.cli.session._cwd_forge_root", return_value=str(forge_root_c)),
            patch("os.getcwd", return_value=str(forge_root_c)),
        ):
            result = runner.invoke(main, ["session", "delete", "shared", "--yes"])

        assert result.exit_code == 1
        assert "Ambiguous" in result.output or "multiple" in result.output.lower()


class TestSessionIncognito:
    """Tests for 'forge session incognito' command."""

    def test_incognito_creates_session(self, runner: CliRunner, temp_env: Path) -> None:
        """Should create an incognito session."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "incognito", "incognito-test"])

        assert result.exit_code == 0
        assert "incognito" in result.output.lower()

    def test_incognito_generates_name(self, runner: CliRunner, temp_env: Path) -> None:
        """Should generate name when not provided."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "incognito"])

        assert result.exit_code == 0
        assert "Created incognito session" in result.output

    def test_incognito_direct_clears_proxy_env(self, runner: CliRunner, temp_env: Path) -> None:
        """Direct incognito sessions should unset proxy routing env vars."""
        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "incognito", "direct-incognito", "--no-proxy"])

        assert result.exit_code == 0
        assert "Routing: direct" in result.output
        kwargs = mock_invoke.call_args.kwargs
        assert "ANTHROPIC_BASE_URL" not in kwargs["env_vars"]
        assert "ACTIVE_TEMPLATE" not in kwargs["env_vars"]
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in kwargs["env_vars"]
        assert sorted(kwargs["unset_env_vars"]) == [
            "ACTIVE_TEMPLATE",
            "ANTHROPIC_BASE_URL",
        ]


def _seed_cross_project_session(project: Path, session_name: str = "cross-sess") -> Path:
    """Seed a session in a nested forge_root that the current CWD can't reach."""
    other_root = project / "nested-sub"
    other_root.mkdir(parents=True, exist_ok=True)
    (other_root / ".forge").mkdir(parents=True, exist_ok=True)

    manifest = create_session_state(
        session_name,
        proxy_template="t",
        proxy_base_url="http://localhost:9999",
        worktree_path=str(other_root),
    )
    manifest.forge_root = str(other_root)
    SessionStore(str(other_root), session_name).write(manifest)
    IndexStore().add_session(
        name=session_name,
        worktree_path=str(other_root),
        project_root=str(project),
        forge_root=str(other_root),
    )
    return other_root


class TestCrossProjectHints:
    """Cross-project 'not found' hints across all affected commands."""

    def test_resume_cross_project_shows_hint(self, runner: CliRunner, temp_env: Path) -> None:
        """Resume from wrong forge_root should hint where the session lives."""
        _seed_cross_project_session(temp_env)

        result = runner.invoke(main, ["session", "resume", "cross-sess"])

        assert result.exit_code == 1
        assert "not found in current project" in result.output
        assert "nested-sub" in result.output

    def test_cross_project_hint_does_not_wrap_target_path(self, temp_env: Path, tmp_path: Path) -> None:
        """Cross-project hints should keep the target path intact on narrow terminals."""
        _seed_cross_project_session(temp_env)
        output = tmp_path / "hint-output.txt"

        with output.open("w", encoding="utf-8") as handle:
            narrow_console = Console(file=handle, width=40, force_terminal=False)
            with patch.object(session_cli, "console", narrow_console):
                hinted = session_cli._hint_cross_project_session("cross-sess", str(temp_env))

        rendered = output.read_text(encoding="utf-8")
        assert hinted is True
        assert "nested-sub" in rendered
        assert "nested-su\nb" not in rendered

    def test_fork_cross_project_shows_hint(self, runner: CliRunner, temp_env: Path) -> None:
        """Fork from wrong forge_root should hint where the parent lives."""
        _seed_cross_project_session(temp_env)

        result = runner.invoke(main, ["session", "fork", "cross-sess", "--name", "child"])

        assert result.exit_code == 1
        assert "not found in current project" in result.output
        assert "nested-sub" in result.output

    def test_show_cross_project_resolves(self, runner: CliRunner, temp_env: Path) -> None:
        """Show from wrong forge_root should resolve cross-project and succeed."""
        _seed_cross_project_session(temp_env)

        result = runner.invoke(main, ["session", "show", "cross-sess"])

        assert result.exit_code == 0
        assert "cross-sess" in result.output
        assert "nested-sub" in result.output  # cross-project note

    def test_shell_cross_project_shows_hint(self, runner: CliRunner, temp_env: Path) -> None:
        """Shell from wrong forge_root should hint where the session lives."""
        _seed_cross_project_session(temp_env)

        result = runner.invoke(main, ["session", "shell", "cross-sess"])

        assert result.exit_code == 1
        assert "not found in current project" in result.output
        assert "nested-sub" in result.output


class TestCrossProjectResolution:
    """Commands that resolve sessions across forge_root boundaries."""

    def test_show_cross_project_json(self, runner: CliRunner, temp_env: Path) -> None:
        """JSON output should work for cross-project sessions."""
        _seed_cross_project_session(temp_env)

        result = runner.invoke(main, ["session", "show", "cross-sess", "--json"])

        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["session_name"] == "cross-sess"

    def test_delete_all_stays_project_scoped(self, runner: CliRunner, temp_env: Path) -> None:
        """--all only deletes sessions in the current forge_root, not cross-project."""
        _seed_cross_project_session(temp_env)

        # Also seed a session in the current forge_root
        local = create_session_state(
            "local-sess", proxy_template="t", proxy_base_url="http://localhost:9999", worktree_path=str(temp_env)
        )
        local.forge_root = str(temp_env)
        SessionStore(str(temp_env), "local-sess").write(local)
        IndexStore().add_session(
            name="local-sess", worktree_path=str(temp_env), project_root=str(temp_env), forge_root=str(temp_env)
        )

        result = runner.invoke(main, ["session", "delete", "--all", "--yes"])

        assert result.exit_code == 0
        assert "local-sess" in result.output

        # Cross-project session should still exist
        from forge.session.manager import SessionManager

        remaining = SessionManager().list_sessions()
        remaining_names = [n for n, _ in remaining]
        assert "cross-sess" in remaining_names

    def test_set_cross_project_resolves(self, runner: CliRunner, temp_env: Path) -> None:
        """set --session should resolve cross-project sessions."""
        _seed_cross_project_session(temp_env)

        result = runner.invoke(main, ["session", "set", "agent", "custom", "--session", "cross-sess"])

        assert result.exit_code == 0
        assert "agent" in result.output

    def test_reset_cross_project_resolves(self, runner: CliRunner, temp_env: Path) -> None:
        """reset --session should resolve cross-project sessions."""
        _seed_cross_project_session(temp_env)

        # First set an override, then reset it
        runner.invoke(main, ["session", "set", "agent", "custom", "--session", "cross-sess"])
        result = runner.invoke(main, ["session", "reset", "agent", "--session", "cross-sess"])

        assert result.exit_code == 0
        assert "Reset" in result.output or "override" in result.output.lower()

    def test_delete_all_refuses_outside_forge_project(self, runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
        """--all should refuse when _cwd_forge_root() is None (outside any Forge project)."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        # Directory with no .forge/
        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        (bare_dir / ".git").mkdir()
        monkeypatch.chdir(bare_dir)

        result = runner.invoke(main, ["session", "delete", "--all", "--yes"])

        assert result.exit_code == 1
        assert "requires being inside a Forge project" in result.output

    def test_delete_cross_project_corrupt_manifest(self, runner: CliRunner, temp_env: Path) -> None:
        """Force-delete should work on cross-project sessions with corrupt manifests."""
        other_root = _seed_cross_project_session(temp_env)

        # Corrupt the manifest
        manifest_path = other_root / ".forge" / "sessions" / "cross-sess" / "forge.session.json"
        manifest_path.write_text("{invalid json")

        result = runner.invoke(main, ["session", "delete", "cross-sess", "--yes", "--force"])

        # Should succeed (force-delete cleans up despite corrupt manifest)
        assert result.exit_code == 0

    def test_show_ambiguous_shows_locations(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Show of ambiguous name from a third forge_root should list all locations."""
        _seed_scoped_duplicate_sessions(temp_env)

        # "shared" exists in forge_root_a (temp_env) and forge_root_b (temp_env/nested-project).
        # Run show from a third forge_root where "shared" does NOT exist.
        forge_root_c = temp_env / "other-project"
        forge_root_c.mkdir(parents=True, exist_ok=True)
        (forge_root_c / ".forge").mkdir(parents=True, exist_ok=True)

        # Must patch CWD too — resolve_session_identifier derives its own
        # forge_root from Path.cwd(), not from session._cwd_forge_root().
        monkeypatch.chdir(forge_root_c)
        with patch("forge.cli.session._cwd_forge_root", return_value=str(forge_root_c)):
            result = runner.invoke(main, ["session", "show", "shared"])

        assert result.exit_code == 1
        assert "Ambiguous" in result.output or "multiple" in result.output.lower()


class TestResumeProjectScoping:
    """Resume/relaunch should stay scoped to the current forge_root."""

    def test_resume_force_active_relaunches_current_project_duplicate(self, runner: CliRunner, temp_env: Path) -> None:
        """--force relaunch should not fall through to an ambiguous duplicate in another project."""
        forge_root_a, forge_root_b = _seed_scoped_duplicate_sessions(temp_env)

        current_state = _read_session_manifest(forge_root_a, "shared")
        current_state.confirmed.claude_session_id = "uuid-alpha"
        current_state.confirmed.confirmed_by = "hook:SessionStart:startup"
        _write_session_manifest(forge_root_a, "shared", current_state)

        other_state = _read_session_manifest(forge_root_b, "shared")
        other_state.confirmed.claude_session_id = "uuid-beta"
        other_state.confirmed.confirmed_by = "hook:SessionStart:startup"
        _write_session_manifest(forge_root_b, "shared", other_state)

        ActiveSessionStore().upsert_session(
            "shared",
            worktree_path=str(forge_root_a),
            launch_mode=LAUNCH_MODE_HOST,
            forge_root=str(forge_root_a),
            launcher_pid=os.getpid(),
        )

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "resume", "shared", "--force"])

        assert result.exit_code == 0, result.output
        assert mock_invoke.call_args is not None
        assert mock_invoke.call_args.kwargs["resume_id"] == "uuid-alpha"

        manager = SessionManager()
        project_a_sessions = [name for name, _ in manager.list_sessions(forge_root_filter=str(forge_root_a))]
        project_b_sessions = [name for name, _ in manager.list_sessions(forge_root_filter=str(forge_root_b))]
        assert project_a_sessions.count("shared") == 1
        assert len(project_a_sessions) == 2
        assert project_b_sessions == ["shared"]


class TestSessionFork:
    """Tests for 'forge session fork' command."""

    def test_fork_direct_parent_clears_proxy_env(self, runner: CliRunner, temp_env: Path) -> None:
        """Direct same-dir forks should not inherit proxy env from the shell."""
        parent = create_session_state(
            "fork-parent",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_state = create_session_state(
            "fork-child",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(temp_env),
        )

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child"])

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert "ANTHROPIC_BASE_URL" not in kwargs["env_vars"]
        assert "ACTIVE_TEMPLATE" not in kwargs["env_vars"]
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in kwargs["env_vars"]
        assert sorted(kwargs["unset_env_vars"]) == [
            "ACTIVE_TEMPLATE",
            "ANTHROPIC_BASE_URL",
        ]
        assert kwargs["model"] is None

    def test_non_direct_sidecar_fork_uses_sidecar_launcher(self, runner: CliRunner, temp_env: Path) -> None:
        """A sidecar parent should fork through the sidecar launch path."""
        runner.invoke(main, ["session", "start", "fork-sidecar-parent", "--sidecar", "--no-launch"])

        store = SessionStore(str(temp_env), "fork-sidecar-parent")

        def _confirm_parent(m: object) -> None:
            m.confirmed.claude_session_id = "parent-sidecar-uuid"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_parent)

        with (
            patch("forge.sidecar.docker.is_docker_available", return_value=True),
            patch("forge.sidecar.get_secrets_for_template", return_value={}),
            patch("forge.sidecar.run_sidecar_session", return_value=0) as mock_run_sidecar,
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
        ):
            result = runner.invoke(main, ["session", "fork", "fork-sidecar-parent", "--name", "fork-sidecar-child"])

        assert result.exit_code == 0, result.output
        assert mock_run_sidecar.called is True
        assert mock_invoke.called is False

    def test_fork_default_no_worktree(self, runner: CliRunner, temp_env: Path) -> None:
        """Default fork stays in parent's directory (no worktree)."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(temp_env),
        )

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child"])

        assert result.exit_code == 0
        # No worktree info in output
        assert "Worktree:" not in result.output
        # Claude invoked with --resume --fork-session
        assert mock_invoke.call_args is not None
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["resume_id"] == "parent-uuid"
        assert kwargs["fork_session"] is True
        # Manager called without create_worktree
        call_kwargs = mock_manager.fork_session.call_args.kwargs
        assert call_kwargs.get("create_worktree") is False

    def test_fork_with_worktree_starts_fresh_with_context(self, runner: CliRunner, temp_env: Path) -> None:
        """Worktree fork starts fresh Claude with parent handoff context (no --resume)."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="fork-child",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True

        context_file = fork_worktree / ".forge" / "prev_sessions" / "fork-parent.md"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("# Parent context\n")

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
            patch(
                "forge.cli.session._generate_parent_handoff_context",
                return_value=(context_file, []),
            ),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--worktree"])

        assert result.exit_code == 0
        assert "Worktree:" in result.output
        # UUID pre-seeded for fresh worktree fork
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs.get("session_id") is not None
        assert len(kwargs["session_id"]) == 36  # UUID format
        assert kwargs.get("name") == "fork-child"
        assert kwargs.get("resume_id") is None
        assert kwargs.get("fork_session") is None
        assert kwargs.get("system_prompt_file") is not None
        call_kwargs = mock_manager.fork_session.call_args.kwargs
        assert call_kwargs["create_worktree"] is True

    def test_fork_worktree_uses_nested_forge_roots_for_extension_inheritance(
        self, runner: CliRunner, temp_env: Path
    ) -> None:
        """Worktree forks should install inherited extensions at the child nested root."""
        parent_nested_root = temp_env / "packages" / "app"
        parent_nested_root.mkdir(parents=True)

        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(parent_nested_root),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"
        parent.forge_root = str(parent_nested_root)

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        child_nested_root = fork_worktree / "packages" / "app"
        child_nested_root.mkdir(parents=True)
        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="fork-child",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True
        fork_state.forge_root = str(child_nested_root)

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude") as mock_invoke,
            patch("forge.cli.session._auto_install_extensions") as mock_auto,
            patch("forge.cli.session._generate_parent_handoff_context", return_value=(None, [])),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(
                main,
                ["session", "fork", "fork-parent", "--name", "fork-child", "--worktree", "--no-launch"],
            )

        assert result.exit_code == 0
        mock_invoke.assert_not_called()
        mock_auto.assert_called_once_with(
            install_root=child_nested_root,
            parent_project_root=parent_nested_root,
            force_extensions=None,
        )

    def test_fork_worktree_full_strategy_budget_uses_parent_forge_root(
        self, runner: CliRunner, temp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full-strategy preflight should read transcript artifacts from parent forge_root."""
        parent_nested_root = temp_env / "packages" / "app"
        parent_nested_root.mkdir(parents=True)
        (parent_nested_root / ".forge" / "artifacts" / "fork-parent" / "transcripts").mkdir(parents=True)
        monkeypatch.chdir(parent_nested_root)

        transcript_path = parent_nested_root / ".forge" / "artifacts" / "fork-parent" / "transcripts" / "large.jsonl"
        transcript_path.write_text("x" * 4096)

        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(parent_nested_root),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"
        parent.forge_root = str(parent_nested_root)
        parent.confirmed.artifacts["transcripts"] = [
            {"copied_path": ".forge/artifacts/fork-parent/transcripts/large.jsonl"}
        ]

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session._resolve_context_limit", return_value=100),
            patch("forge.cli.session.invoke_claude") as mock_invoke,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.get_session.return_value = parent
            mock_manager.resolve_project_root.return_value = str(temp_env)

            result = runner.invoke(
                main,
                ["session", "fork", "fork-parent", "--name", "fork-child", "--worktree", "--strategy", "full"],
            )

        assert result.exit_code == 1
        assert "exceeds context limit" in result.output
        mock_manager.fork_session.assert_not_called()
        mock_invoke.assert_not_called()

    def test_fork_branch_implies_worktree(self, runner: CliRunner, temp_env: Path) -> None:
        """--branch automatically enables --worktree."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="custom-branch",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0),
            patch(
                "forge.cli.session._generate_parent_handoff_context",
                return_value=(None, []),
            ),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(
                main,
                [
                    "session",
                    "fork",
                    "fork-parent",
                    "--name",
                    "fork-child",
                    "--branch",
                    "custom-branch",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_manager.fork_session.call_args.kwargs
        assert call_kwargs["create_worktree"] is True
        assert call_kwargs["branch"] == "custom-branch"

    def test_fork_worktree_requires_git_repo(self, runner: CliRunner, temp_env: Path) -> None:
        """Fork with --worktree requires a proper git repository."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "fork-parent"])

        result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--worktree"])

        assert result.exit_code == 1
        assert "git" in result.output.lower()

    def test_worktree_fork_never_attempts_resume(self, runner: CliRunner, temp_env: Path) -> None:
        """Worktree fork should never try --resume --fork-session (it can't work cross-project)."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        fork = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="fork-child",
        )
        assert fork.worktree is not None
        fork.worktree.is_worktree = True

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
            patch(
                "forge.cli.session._generate_parent_handoff_context",
                return_value=(None, []),
            ),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--worktree"])

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs.get("resume_id") is None
        assert kwargs.get("fork_session") is None

    def test_fork_no_worktree_no_launch_tip_on_failure(self, runner: CliRunner, temp_env: Path) -> None:
        """Non-worktree fork failure should NOT show cross-worktree tip."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(temp_env),
        )

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=1),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child"])

        assert result.exit_code == 1
        assert "forge session launch" not in result.output

    def test_fork_direct_flag_passes_to_manager(self, runner: CliRunner, temp_env: Path) -> None:
        """--no-proxy flag is forwarded to manager.fork_session."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        # Manager returns a fork with no proxy (direct mode)
        fork_state = create_session_state(
            "fork-child",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(temp_env),
        )

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--no-proxy"])

        assert result.exit_code == 0
        call_kwargs = mock_manager.fork_session.call_args.kwargs
        assert call_kwargs["parent_name"] == "fork-parent"
        assert call_kwargs["fork_name"] == "fork-child"
        assert call_kwargs["direct"] is True
        assert call_kwargs["is_incognito"] is False
        assert call_kwargs["create_worktree"] is False

    def test_fork_direct_uses_configured_model_override(self, runner: CliRunner, temp_env: Path) -> None:
        """Direct forks should honor the configured direct-model override."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_state = create_session_state(
            "fork-child",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(temp_env),
        )

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
            patch("forge.runtime_config.get_default_direct_model", return_value="claude-sonnet-4-6"),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--no-proxy"])

        assert result.exit_code == 0
        assert mock_invoke.call_args is not None
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["model"] is None
        assert kwargs["env_vars"]["ANTHROPIC_MODEL"] == "sonnet"
        assert kwargs["env_vars"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-4-6"

    def test_fork_no_launch_skips_claude(self, runner: CliRunner, temp_env: Path) -> None:
        """--no-launch should create fork without invoking Claude."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(temp_env),
        )

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude") as mock_invoke,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--no-launch"])

        assert result.exit_code == 0
        assert "--no-launch" in result.output
        mock_invoke.assert_not_called()

    def test_fork_worktree_no_launch_generates_context_and_prints_tip(self, runner: CliRunner, temp_env: Path) -> None:
        """--worktree --no-launch should generate context and print cd tip."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="fork-child",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True

        context_file = fork_worktree / ".forge" / "prev_sessions" / "fork-parent.md"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("# Parent context\n")

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude") as mock_invoke,
            patch(
                "forge.cli.session._generate_parent_handoff_context",
                return_value=(context_file, []),
            ),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(
                main,
                [
                    "session",
                    "fork",
                    "fork-parent",
                    "--name",
                    "fork-child",
                    "--worktree",
                    "--no-launch",
                ],
            )

        assert result.exit_code == 0
        assert "--no-launch" in result.output
        # Rich wraps long lines; normalize whitespace for assertions
        normalized = " ".join(result.output.split())
        assert "forge session resume fork-child" in normalized
        compact = "".join(result.output.split())
        assert f"cd{fork_worktree}&&forgesessionresumefork-child" in compact
        mock_invoke.assert_not_called()

    def test_fork_worktree_no_launch_tip_uses_nested_forge_root(self, runner: CliRunner, temp_env: Path) -> None:
        """Nested worktree forks should print the nested Forge root, not checkout root."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        nested_root = fork_worktree / "experiments" / "drafting" / "iterative-drafting-poc"
        nested_root.mkdir(parents=True)
        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="fork-child",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True
        fork_state.forge_root = str(nested_root)

        context_file = nested_root / ".forge" / "prev_sessions" / "fork-parent.md"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("# Parent context\n")

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude") as mock_invoke,
            patch("forge.cli.session._auto_install_extensions", return_value=False),
            patch(
                "forge.cli.session._generate_parent_handoff_context",
                return_value=(context_file, []),
            ),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(
                main,
                [
                    "session",
                    "fork",
                    "fork-parent",
                    "--name",
                    "fork-child",
                    "--worktree",
                    "--no-launch",
                ],
            )

        assert result.exit_code == 0
        normalized = " ".join(result.output.split())
        assert "forge session resume fork-child" in normalized
        compact = "".join(result.output.split())
        assert f"cd{nested_root}&&forgesessionresumefork-child" in compact
        mock_invoke.assert_not_called()

    def test_fork_worktree_post_exit_tip_uses_nested_forge_root(self, runner: CliRunner, temp_env: Path) -> None:
        """Host worktree forks should print the nested resume dir after Claude exits."""
        parent = create_session_state(
            "fork-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "parent-uuid"

        fork_worktree = temp_env / "fork-child"
        fork_worktree.mkdir()
        nested_root = fork_worktree / "experiments" / "drafting" / "iterative-drafting-poc"
        nested_root.mkdir(parents=True)
        fork_state = create_session_state(
            "fork-child",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="fork-parent",
            is_fork=True,
            worktree_path=str(fork_worktree),
            worktree_branch="fork-child",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True
        fork_state.forge_root = str(nested_root)

        context_file = nested_root / ".forge" / "prev_sessions" / "fork-parent.md"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("# Parent context\n")

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0),
            patch("forge.cli.session.run_with_active_session", side_effect=lambda runner, **kw: runner()),
            patch("forge.cli.session._warn_if_hooks_missing"),
            patch("forge.cli.session._warn_if_version_outdated"),
            patch("forge.cli.session._auto_install_extensions", return_value=False),
            patch(
                "forge.cli.session._generate_parent_handoff_context",
                return_value=(context_file, []),
            ),
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.fork_session.return_value = (parent, fork_state)

            result = runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--worktree"])

        assert result.exit_code == 0
        normalized = " ".join(result.output.replace("\x1b[2K", "").split())
        assert "Reconnect to this conversation with:" in normalized
        compact = "".join(result.output.replace("\x1b[2K", "").split())
        assert f"cd{nested_root}&&forgesessionresumefork-child" in compact


class TestSessionForkIntoPreflight:
    """Tests for --into cross-repo preflight validation."""

    def test_into_cross_repo_rejected_before_fork(self, runner: CliRunner, temp_env: Path) -> None:
        """--into targeting a different repo should fail before fork_session() is called."""
        parent = create_session_state(
            "planner",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )

        into_dir = temp_env / "other-worktree"
        into_dir.mkdir()

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            # Simulate --into target resolving to a real git checkout
            patch("subprocess.run") as mock_run,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.get_session.return_value = parent

            def fake_git_run(cmd, **kwargs):
                """Return different git-common-dir for target vs parent."""
                from unittest.mock import MagicMock

                result = MagicMock()
                result.returncode = 0
                git_c_path = cmd[cmd.index("-C") + 1] if "-C" in cmd else None
                if "--show-toplevel" in cmd:
                    result.stdout = str(into_dir)
                elif "--git-common-dir" in cmd:
                    if git_c_path and str(into_dir) in git_c_path:
                        result.stdout = "/repos/other-repo/.git"
                    else:
                        result.stdout = str(temp_env / ".git")
                elif "--abbrev-ref" in cmd:
                    result.stdout = "some-branch"
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = fake_git_run

            result = runner.invoke(
                main,
                ["session", "fork", "planner", "--into", str(into_dir)],
            )

        assert result.exit_code != 0
        assert "not part of the same repository" in result.output
        # fork_session should never have been called
        mock_manager.fork_session.assert_not_called()

    def test_into_same_repo_passes_preflight(self, runner: CliRunner, temp_env: Path) -> None:
        """--into targeting the same repo should pass preflight and reach fork_session()."""
        parent = create_session_state(
            "planner",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "uuid-parent"

        fork_state = create_session_state(
            "reviewer",
            parent_session="planner",
            is_fork=True,
            worktree_path=str(temp_env / "wt"),
        )

        into_dir = temp_env / "existing-wt"
        into_dir.mkdir()

        common_git = str(temp_env / ".git")

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude", return_value=0),
            patch("subprocess.run") as mock_run,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.get_session.return_value = parent
            mock_manager.fork_session.return_value = (parent, fork_state)

            def fake_git_run(cmd, **kwargs):
                from unittest.mock import MagicMock

                result = MagicMock()
                result.returncode = 0
                if "--show-toplevel" in cmd:
                    result.stdout = str(into_dir)
                elif "--git-common-dir" in cmd:
                    # Same repo for both target and parent
                    result.stdout = common_git
                elif "--abbrev-ref" in cmd:
                    result.stdout = "feat-branch"
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = fake_git_run

            result = runner.invoke(
                main,
                ["session", "fork", "planner", "--into", str(into_dir), "--no-launch"],
            )

        assert "not part of the same repository" not in result.output
        # fork_session should have been called
        mock_manager.fork_session.assert_called_once()

    def test_into_skip_extension_check_uses_nested_target_root(self, runner: CliRunner, temp_env: Path) -> None:
        """Existing local installs should be detected at the target nested Forge root."""
        parent_nested_root = temp_env / "packages" / "app"
        parent_nested_root.mkdir(parents=True)

        parent = create_session_state(
            "planner",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(parent_nested_root),
            worktree_branch="main",
        )
        parent.confirmed.claude_session_id = "uuid-parent"
        parent.forge_root = str(parent_nested_root)

        into_dir = temp_env / "existing-wt"
        into_dir.mkdir()
        child_nested_root = into_dir / "packages" / "app"
        child_nested_root.mkdir(parents=True)

        fork_state = create_session_state(
            "reviewer",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            parent_session="planner",
            is_fork=True,
            worktree_path=str(into_dir),
            worktree_branch="reviewer",
        )
        assert fork_state.worktree is not None
        fork_state.worktree.is_worktree = True
        fork_state.forge_root = str(child_nested_root)

        common_git = str(temp_env / ".git")

        with (
            patch("forge.cli.session.SessionManager") as mock_manager_cls,
            patch("forge.cli.session.invoke_claude") as mock_invoke,
            patch("forge.cli.session._auto_install_extensions") as mock_auto,
            patch("forge.cli.session._generate_parent_handoff_context", return_value=(None, [])),
            patch("forge.install.tracking.TrackingStore") as mock_tracking_cls,
            patch("subprocess.run") as mock_run,
        ):
            mock_manager = mock_manager_cls.return_value
            mock_manager.get_session.return_value = parent
            mock_manager.fork_session.return_value = (parent, fork_state)

            tracking_store = mock_tracking_cls.return_value
            tracking_store.get_installation.side_effect = lambda scope, path=None: (
                object() if scope == "local" and path == str(child_nested_root) else None
            )

            def fake_git_run(cmd, **kwargs):
                from unittest.mock import MagicMock

                result = MagicMock()
                result.returncode = 0
                if "--show-toplevel" in cmd:
                    result.stdout = str(into_dir)
                elif "--git-common-dir" in cmd:
                    result.stdout = common_git
                elif "--abbrev-ref" in cmd:
                    result.stdout = "reviewer"
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = fake_git_run

            result = runner.invoke(
                main,
                ["session", "fork", "planner", "--name", "reviewer", "--into", str(into_dir), "--no-launch"],
            )

        assert result.exit_code == 0
        tracking_store.get_installation.assert_any_call("local", str(child_nested_root))
        mock_auto.assert_not_called()
        mock_invoke.assert_not_called()


class TestSessionResumeExtended:
    """Additional session resume tests."""

    def test_resume_combines_custom_prompt_under_forge_launch_context(self, runner: CliRunner, temp_env: Path) -> None:
        """Resume should store combined prompt files under .forge/launch-context."""
        custom_prompt = temp_env / "custom-system.md"
        custom_prompt.write_text("Custom system prompt", encoding="utf-8")

        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "resume-parent", "--no-launch"])

        manager = SessionManager()
        parent_store = manager.get_session_store("resume-parent")

        # Simulate SessionStart hook setting the UUID (launch-owned)
        parent_session_id = "simulated-resume-parent-uuid"

        def _set_parent_prompt_and_uuid(manifest) -> None:
            manifest.intent.system_prompt = SystemPromptIntent(file=str(custom_prompt))
            manifest.confirmed.claude_session_id = parent_session_id

        parent_store.update(timeout_s=5.0, mutate=_set_parent_prompt_and_uuid)

        from forge.session.claude.paths import get_transcript_path

        transcript_path = get_transcript_path(str(temp_env.resolve()), parent_session_id)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            '{"requestId":"r1","timestamp":"2025-01-15T10:00:00Z","message":{"role":"user","content":[{"type":"text","text":"resume context"}]}}\n',
            encoding="utf-8",
        )

        def _set_parent_transcript(manifest) -> None:
            manifest.confirmed.transcript_path = str(transcript_path)

        parent_store.update(timeout_s=5.0, mutate=_set_parent_transcript)

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "resume", "resume-parent", "--fresh"])

        assert result.exit_code == 0
        assert mock_invoke.call_args is not None
        prompt_file = mock_invoke.call_args.kwargs["system_prompt_file"]
        assert prompt_file is not None
        prompt_path = Path(prompt_file)
        assert prompt_path.parent == temp_env / ".forge" / "launch-context"
        prompt_content = prompt_path.read_text(encoding="utf-8")
        assert "Custom system prompt" in prompt_content
        assert "# Session Context: resume-parent" in prompt_content


class TestSessionResume:
    """Tests for 'forge session resume' command."""

    def test_resume_fresh_creates_derived_session(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh should create a derived session from an existing one."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "resume-test"])

        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "resume", "resume-test", "--fresh"])

        assert result.exit_code == 0
        assert "Created derived session" in result.output

    def test_resume_fresh_direct_parent_keeps_child_direct(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh on a direct parent should launch the child without proxy env."""
        runner.invoke(main, ["session", "start", "resume-direct", "--no-proxy", "--no-launch"])

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "resume", "resume-direct", "--fresh"])

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert "ANTHROPIC_BASE_URL" not in kwargs["env_vars"]
        assert "ACTIVE_TEMPLATE" not in kwargs["env_vars"]
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in kwargs["env_vars"]
        assert sorted(kwargs["unset_env_vars"]) == [
            "ACTIVE_TEMPLATE",
            "ANTHROPIC_BASE_URL",
        ]
        assert kwargs["model"] is None

    def test_resume_fresh_direct_uses_configured_model_override(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh direct resume should honor the configured direct-model override."""
        runner.invoke(main, ["session", "start", "resume-direct", "--no-proxy", "--no-launch"])

        with (
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
            patch("forge.runtime_config.get_default_direct_model", return_value="claude-sonnet-4-6"),
        ):
            result = runner.invoke(main, ["session", "resume", "resume-direct", "--fresh"])

        assert result.exit_code == 0
        assert mock_invoke.call_args is not None
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["model"] is None
        assert kwargs["env_vars"]["ANTHROPIC_MODEL"] == "sonnet"
        assert kwargs["env_vars"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-4-6"

    def test_resume_nonexistent_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail for nonexistent session."""
        result = runner.invoke(main, ["session", "resume", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output


class TestResumeNativeMode:
    """Tests for --resume-mode native|handoff on forge session resume."""

    def test_resume_fresh_default_is_handoff(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh without --resume-mode should use handoff (assembled context)."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "native-test"])

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "resume", "native-test", "--fresh"])

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        # Handoff mode uses session_id (new session), not resume_id
        assert kwargs.get("session_id") is not None
        assert kwargs.get("resume_id") is None
        assert kwargs.get("fork_session") is False

    def test_resume_fresh_native_uses_resume_fork_session(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh --resume-mode native should use --resume --fork-session."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "native-test"])

        # Set confirmed session evidence (UUID + confirmed_by, required for native mode)
        store = SessionStore(str(temp_env), "native-test")

        def _confirm_native_test(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-123"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = "hook:SessionStart:startup"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_native_test)

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "resume", "native-test", "--fresh", "--resume-mode", "native"])

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        # Native mode uses resume_id + fork_session, not session_id
        assert kwargs.get("resume_id") == "parent-uuid-123"
        assert kwargs.get("fork_session") is True
        assert kwargs.get("session_id") is None
        # Must NOT pass system_prompt_file
        assert kwargs.get("system_prompt_file") is None

    def test_resume_fresh_native_no_handoff_generation(self, runner: CliRunner, temp_env: Path) -> None:
        """Native mode must not call handoff generation at all."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "native-nogen"])

        store = SessionStore(str(temp_env), "native-nogen")

        def _confirm_nogen(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-456"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = "hook:SessionStart:startup"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_nogen)

        with (
            patch("forge.cli.session.invoke_claude", return_value=0),
            patch("forge.session.manager.process_handoff") as mock_handoff,
        ):
            result = runner.invoke(main, ["session", "resume", "native-nogen", "--fresh", "--resume-mode", "native"])

        assert result.exit_code == 0
        mock_handoff.assert_not_called()

    def test_resume_fresh_native_requires_claude_session_id(self, runner: CliRunner, temp_env: Path) -> None:
        """Native mode requires parent to have a confirmed claude_session_id."""
        # Create a session with no claude_session_id (simulate never-launched)
        state = create_session_state(
            "no-uuid",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8085",
            worktree_path=str(temp_env),
            worktree_branch="main",
        )
        assert state.confirmed.claude_session_id is None
        from forge.session.index import IndexStore

        store = SessionStore(str(temp_env), "no-uuid")
        store.write(state)
        idx = IndexStore()
        idx.add_from_state(state, str(temp_env))

        result = runner.invoke(main, ["session", "resume", "no-uuid", "--fresh", "--resume-mode", "native"])

        assert result.exit_code == 1
        assert "resume-mode native requires a parent" in result.output

    def test_resume_fresh_native_accepts_inferred_transcript_file(self, runner: CliRunner, temp_env: Path) -> None:
        """Native mode should accept transcript-backed parents even without confirmed_by."""
        runner.invoke(main, ["session", "start", "native-inferred", "--no-launch"])

        store = SessionStore(str(temp_env), "native-inferred")

        def _set_transcript_backed(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-inferred"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = None  # type: ignore[attr-defined]
            m.confirmed.transcript_path = None  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_set_transcript_backed)

        from forge.session.claude.paths import get_transcript_path

        transcript_path = get_transcript_path(str(temp_env), "parent-uuid-inferred")
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text('{"message":{"role":"user","content":[{"type":"text","text":"hello"}]}}\n')

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(
                main,
                ["session", "resume", "native-inferred", "--fresh", "--resume-mode", "native"],
            )

        assert result.exit_code == 0, result.output
        assert mock_invoke.call_args is not None
        assert mock_invoke.call_args.kwargs["resume_id"] == "parent-uuid-inferred"
        assert mock_invoke.call_args.kwargs["fork_session"] is True

    def test_resume_fresh_native_rejects_missing_transcript_file_without_confirmation(
        self, runner: CliRunner, temp_env: Path
    ) -> None:
        """A stale transcript_path string should not count as resumable evidence."""
        runner.invoke(main, ["session", "start", "native-stale", "--no-launch"])

        store = SessionStore(str(temp_env), "native-stale")

        def _set_stale_transcript(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-stale"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = None  # type: ignore[attr-defined]
            m.confirmed.transcript_path = str(temp_env / "missing-transcript.jsonl")  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_set_stale_transcript)

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(
                main,
                ["session", "resume", "native-stale", "--fresh", "--resume-mode", "native"],
            )

        assert result.exit_code == 1
        assert mock_invoke.called is False
        assert "resume-mode native requires a parent" in result.output

    def test_resume_mode_without_fresh_is_error(self, runner: CliRunner, temp_env: Path) -> None:
        """--resume-mode without --fresh should error."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "mode-test"])

        result = runner.invoke(main, ["session", "resume", "mode-test", "--resume-mode", "native"])

        assert result.exit_code == 1
        assert "--resume-mode requires --fresh" in result.output

    def test_resume_fresh_native_warns_about_strategy(self, runner: CliRunner, temp_env: Path) -> None:
        """--resume-mode native with explicit --strategy should print a warning tip."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "warn-test"])

        store = SessionStore(str(temp_env), "warn-test")

        def _confirm_warn(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-789"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = "hook:SessionStart:startup"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_warn)

        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(
                main,
                ["session", "resume", "warn-test", "--fresh", "--resume-mode", "native", "--strategy", "full"],
            )

        assert result.exit_code == 0
        assert "Tip:" in result.output
        assert "--strategy is ignored" in result.output

    def test_resume_fresh_native_with_proxy_override(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh --resume-mode native --proxy should apply routing override."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "proxy-native"])

        store = SessionStore(str(temp_env), "proxy-native")

        def _confirm_proxy(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-abc"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = "hook:SessionStart:startup"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_proxy)

        with (
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
            patch(
                "forge.cli.session._resolve_routing_from_cli",
                return_value=type(
                    "R",
                    (),
                    {
                        "proxy_id": "test-proxy",
                        "template": "litellm-test",
                        "base_url": "http://localhost:9999",
                        "context_limit": None,
                    },
                )(),
            ),
        ):
            result = runner.invoke(
                main,
                ["session", "resume", "proxy-native", "--fresh", "--resume-mode", "native", "--proxy", "test-proxy"],
            )

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs.get("resume_id") == "parent-uuid-abc"
        assert kwargs.get("fork_session") is True
        # Proxy env should be set
        assert "ANTHROPIC_BASE_URL" in kwargs.get("env_vars", {})

    def test_resume_fresh_native_with_direct_flag(self, runner: CliRunner, temp_env: Path) -> None:
        """--fresh --resume-mode native --no-proxy should strip proxy env."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "direct-native"])

        store = SessionStore(str(temp_env), "direct-native")

        def _confirm_direct(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-def"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = "hook:SessionStart:startup"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_direct)

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(
                main,
                ["session", "resume", "direct-native", "--fresh", "--resume-mode", "native", "--no-proxy"],
            )

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs.get("resume_id") == "parent-uuid-def"
        assert kwargs.get("fork_session") is True
        # Direct mode should unset proxy env vars
        assert "ANTHROPIC_BASE_URL" not in kwargs.get("env_vars", {})

    def test_resume_fresh_native_persists_derivation(self, runner: CliRunner, temp_env: Path) -> None:
        """Native resume should persist correct derivation fields in child manifest."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "persist-parent"])

        store = SessionStore(str(temp_env), "persist-parent")

        def _confirm_persist(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid-persist"  # type: ignore[attr-defined]
            m.confirmed.confirmed_by = "hook:SessionStart:startup"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_persist)

        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(
                main,
                [
                    "session",
                    "resume",
                    "persist-parent",
                    "--fresh",
                    "--resume-mode",
                    "native",
                    "--child-name",
                    "persist-child",
                ],
            )

        assert result.exit_code == 0

        # Read child manifest from disk and verify derivation fields
        child_store = SessionStore(str(temp_env), "persist-child")
        child_state = child_store.read()

        assert child_state.parent_session == "persist-parent"
        assert child_state.is_fork is False

        deriv = child_state.confirmed.derivation
        assert deriv is not None
        assert deriv.resume_mode == "native"
        assert deriv.strategy is None
        assert deriv.context_file is None
        assert deriv.parent_session == "persist-parent"
        assert deriv.depth == 1
        assert deriv.lineage == ["persist-parent"]


class TestProxyDirectFlags:
    """Tests for --proxy/--no-proxy flag consistency across commands."""

    def test_start_proxy_and_direct_mutually_exclusive(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "start", "test", "--proxy", "foo", "--no-proxy"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_resume_proxy_and_direct_mutually_exclusive(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "resume", "test", "--proxy", "foo", "--no-proxy"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_fork_proxy_and_direct_mutually_exclusive(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "parent", "--no-proxy"])
        result = runner.invoke(main, ["session", "fork", "parent", "--proxy", "foo", "--no-proxy"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_incognito_proxy_and_direct_mutually_exclusive(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "incognito", "--proxy", "foo", "--no-proxy"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_resume_direct_overrides_parent_proxy(self, runner: CliRunner, temp_env: Path) -> None:
        """--no-proxy on resume should clear proxy env even if parent had a proxy."""
        manager = SessionManager()
        manager.start_session(
            name="proxy-parent",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8084",
        )

        with patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke:
            result = runner.invoke(main, ["session", "resume", "proxy-parent", "--no-proxy"])

        assert result.exit_code == 0
        kwargs = mock_invoke.call_args.kwargs
        assert "ANTHROPIC_BASE_URL" not in kwargs["env_vars"]
        assert "ANTHROPIC_BASE_URL" in kwargs["unset_env_vars"]

    def test_proxy_routed_resume_ignores_stored_direct_model_env(self, runner: CliRunner, temp_env: Path) -> None:
        """--proxy on resume should not inject a stored direct-model pin."""
        runner.invoke(main, ["session", "start", "proxy-resume-parent", "--model", "opus-4-7", "--no-launch"])
        routing = session_cli.ResolvedRouting(
            template="litellm-openai",
            base_url="http://localhost:9999",
            proxy_id="test-proxy",
        )

        with (
            patch("forge.cli.session._resolve_routing_from_cli", return_value=routing),
            patch("forge.cli.session._resolve_context_limit", return_value=None),
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
        ):
            result = runner.invoke(main, ["session", "resume", "proxy-resume-parent", "--proxy", "test-proxy"])

        assert result.exit_code == 0, result.output
        env_vars = mock_invoke.call_args.kwargs["env_vars"]
        assert env_vars["ANTHROPIC_BASE_URL"] == "http://localhost:9999"
        assert "ANTHROPIC_MODEL" not in env_vars
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env_vars

    def test_proxy_routed_fork_ignores_stored_direct_model_env(self, runner: CliRunner, temp_env: Path) -> None:
        """--proxy on fork should not inject an inherited direct-model pin."""
        runner.invoke(main, ["session", "start", "proxy-fork-parent", "--model", "opus-4-7", "--no-launch"])
        store = SessionStore(str(temp_env), "proxy-fork-parent")

        def _confirm_parent(m: object) -> None:
            m.confirmed.claude_session_id = "parent-uuid"  # type: ignore[attr-defined]

        store.update(timeout_s=5.0, mutate=_confirm_parent)
        routing = session_cli.ResolvedRouting(
            template="litellm-openai",
            base_url="http://localhost:9999",
            proxy_id="test-proxy",
        )

        with (
            patch("forge.cli.session._resolve_routing_from_cli", return_value=routing),
            patch("forge.cli.session._resolve_context_limit", return_value=None),
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
        ):
            result = runner.invoke(
                main,
                ["session", "fork", "proxy-fork-parent", "--name", "proxy-fork-child", "--proxy", "test-proxy"],
            )

        assert result.exit_code == 0, result.output
        env_vars = mock_invoke.call_args.kwargs["env_vars"]
        assert env_vars["ANTHROPIC_BASE_URL"] == "http://localhost:9999"
        assert "ANTHROPIC_MODEL" not in env_vars
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env_vars

    def test_resume_direct_on_sidecar_parent_uses_host_path(self, runner: CliRunner, temp_env: Path) -> None:
        """--no-proxy on resume should override inherited sidecar launch mode."""
        runner.invoke(
            main,
            [
                "session",
                "start",
                "resume-sidecar-parent",
                "--sidecar",
                "--no-launch",
            ],
        )

        with (
            patch("forge.cli.session.invoke_claude", return_value=0) as mock_invoke,
            patch("forge.sidecar.run_sidecar_session", return_value=0) as mock_run_sidecar,
        ):
            result = runner.invoke(
                main,
                [
                    "session",
                    "resume",
                    "resume-sidecar-parent",
                    "--fresh",
                    "--child-name",
                    "resume-sidecar-child",
                    "--no-proxy",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_invoke.called is True
        assert mock_run_sidecar.called is False

        manager = SessionManager()
        child_state = manager.get_session("resume-sidecar-child")
        assert child_state.intent.proxy is None
        assert child_state.intent.launch is not None
        assert child_state.intent.launch.mode == LAUNCH_MODE_HOST
        assert child_state.intent.launch.sidecar is None

    def test_fork_proxy_no_launch_persists_intent(self, runner: CliRunner, temp_env: Path) -> None:
        """--proxy on fork --no-launch should persist routing to manifest."""
        import json

        # Create parent with direct routing
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "persist-parent", "--no-proxy"])

        # Write a proxy registry so resolve_proxy succeeds
        forge_home = Path(os.environ.get("FORGE_HOME", Path.home() / ".forge"))
        registry_path = forge_home / "proxies" / "index.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "test-proxy": {
                            "proxy_id": "test-proxy",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "status": "healthy",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with patch("forge.cli.claude._healthcheck_proxy", lambda **_: None):
            result = runner.invoke(
                main,
                [
                    "session",
                    "fork",
                    "persist-parent",
                    "--name",
                    "persist-child",
                    "--proxy",
                    "test-proxy",
                    "--no-launch",
                ],
            )

        assert result.exit_code == 0, result.output

        # Verify the manifest has the overridden proxy
        manager = SessionManager()
        child_state = manager.get_session("persist-child")
        assert child_state.intent.proxy is not None
        assert child_state.intent.proxy.template == "litellm-openai"
        assert child_state.intent.proxy.base_url == "http://localhost:8085"

    def test_routing_override_preserves_confirmed_proxy_on_disk(self, runner: CliRunner, temp_env: Path) -> None:
        """--proxy/--no-proxy should not clear confirmed.started_with_proxy on disk.

        A failed launch must not leave the manifest with cleared confirmed state.
        Only intent should be persisted; confirmed is hook-owned.
        """
        manager = SessionManager()
        manager.start_session(
            name="confirmed-proxy-test",
            proxy_template="litellm-openai",
            proxy_base_url="http://localhost:8084",
        )

        # Simulate hook confirmation with a proxy snapshot
        store = SessionStore(str(Path.cwd()), "confirmed-proxy-test")
        manifest = store.read()
        manifest.confirmed.started_with_proxy = StartedWithProxy(
            base_url="http://localhost:8084",
            proxy_id="old-proxy",
            template="litellm-openai",
        )
        manifest.confirmed.claude_session_id = "test-uuid-for-resume"
        manifest.confirmed.confirmed_by = "hook:SessionStart:startup"
        store.write(manifest)

        # Resume with --no-proxy (should change intent but not clear confirmed on disk)
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "resume", "confirmed-proxy-test", "--no-proxy"])

        assert result.exit_code == 0, result.output

        # Verify: intent cleared (direct mode), but confirmed proxy preserved on disk
        updated = store.read()
        assert updated.intent.proxy is None, "intent.proxy should be cleared for --no-proxy"
        assert (
            updated.confirmed.started_with_proxy is not None
        ), "confirmed.started_with_proxy should NOT be cleared on disk"
        assert updated.confirmed.started_with_proxy.proxy_id == "old-proxy"


class TestDirectDeprecatedAlias:
    """Verify hidden --direct alias still works for backward compatibility."""

    def test_start_direct_alias_still_works(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "start", "alias-test", "--direct", "--no-launch"])
        assert result.exit_code == 0

    def test_resume_direct_alias_still_works(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "alias-resume", "--no-proxy"])
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "resume", "alias-resume", "--direct"])
        assert result.exit_code == 0

    def test_fork_direct_alias_still_works(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "alias-fork-parent", "--no-proxy"])
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "fork", "alias-fork-parent", "--direct"])
        assert result.exit_code == 0

    def test_incognito_direct_alias_still_works(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            result = runner.invoke(main, ["session", "incognito", "alias-incog", "--direct"])
        assert result.exit_code == 0


class TestSupervisorProxyFlags:
    """Tests for --supervisor-proxy / --no-supervisor-proxy on start and fork."""

    def test_start_supervisor_proxy_mutual_exclusivity(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(
            main,
            ["session", "start", "test", "--supervise", "planner", "--supervisor-proxy", "x", "--no-supervisor-proxy"],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_start_supervisor_proxy_requires_supervise(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "start", "test", "--supervisor-proxy", "x", "--no-launch"])
        assert result.exit_code == 1
        assert "require --supervise" in result.output

    def test_start_no_supervisor_proxy_requires_supervise(self, runner: CliRunner, temp_env: Path) -> None:
        result = runner.invoke(main, ["session", "start", "test", "--no-supervisor-proxy", "--no-launch"])
        assert result.exit_code == 1
        assert "require --supervise" in result.output

    def test_fork_supervisor_proxy_mutual_exclusivity(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "sup-parent", "--no-proxy"])
        result = runner.invoke(
            main,
            ["session", "fork", "sup-parent", "--supervise", "--supervisor-proxy", "x", "--no-supervisor-proxy"],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_fork_supervisor_proxy_requires_supervise(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "sup-parent2", "--no-proxy"])
        result = runner.invoke(main, ["session", "fork", "sup-parent2", "--supervisor-proxy", "x"])
        assert result.exit_code == 1
        assert "require --supervise" in result.output

    def test_start_bad_supervisor_proxy_leaves_no_session(self, runner: CliRunner, temp_env: Path) -> None:
        """Bad --supervisor-proxy should fail before creating session state."""
        from unittest.mock import MagicMock

        with patch("forge.guard.semantic.supervisor.validate_supervisor_target") as mock_validate:
            mock_state = MagicMock()
            mock_state.confirmed.started_with_proxy = None
            mock_state.forge_root = None
            mock_validate.return_value = mock_state
            result = runner.invoke(
                main,
                [
                    "session",
                    "start",
                    "bad-proxy-test",
                    "--supervise",
                    "planner",
                    "--supervisor-proxy",
                    "nonexistent-proxy",
                    "--no-launch",
                ],
            )
        assert result.exit_code == 1
        assert "not found" in result.output
        manager = SessionManager()
        sessions = {n for n, _ in manager.list_sessions()}
        assert "bad-proxy-test" not in sessions

    def test_fork_bad_supervisor_proxy_leaves_no_fork(self, runner: CliRunner, temp_env: Path) -> None:
        """Bad --supervisor-proxy should fail before creating fork state."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "fork-badproxy-parent", "--no-proxy"])
        result = runner.invoke(
            main,
            ["session", "fork", "fork-badproxy-parent", "--supervise", "--supervisor-proxy", "nonexistent-proxy"],
        )
        assert result.exit_code == 1
        assert "not found" in result.output
        manager = SessionManager()
        sessions = {n for n, _ in manager.list_sessions()}
        assert "fork-badproxy-parent" in sessions  # parent still exists
        fork_names = {n for n, _ in manager.list_sessions() if n != "fork-badproxy-parent"}
        assert not any("fork-badproxy-parent" in n for n in fork_names)


class TestMainGroup:
    """Tests for main CLI group."""

    def test_help(self, runner: CliRunner) -> None:
        """Should show help."""
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "Claude Forge" in result.output

    def test_session_subcommand_help(self, runner: CliRunner) -> None:
        """Should show session subcommand help."""
        result = runner.invoke(main, ["session", "--help"])

        assert result.exit_code == 0
        assert "start" in result.output
        assert "resume" in result.output
        assert "list" in result.output
        assert "launch" not in result.output


class TestSessionSetOverride:
    """Tests for 'forge session set' command."""

    def test_set_updates_overrides(self, runner: CliRunner, temp_env: Path) -> None:
        """Should set an override value."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "set-test"])

        result = runner.invoke(main, ["session", "set", "--session", "set-test", "policy.fail_mode", "closed"])

        assert result.exit_code == 0
        assert "Set" in result.output
        assert "policy.fail_mode" in result.output
        assert "closed" in result.output

    def test_set_nested_key(self, runner: CliRunner, temp_env: Path) -> None:
        """Should set nested key values."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "nested-test"])

        result = runner.invoke(main, ["session", "set", "--session", "nested-test", "custom.my_flag", "true"])

        assert result.exit_code == 1
        assert "custom.* is not supported" in result.output

    def test_set_json_value(self, runner: CliRunner, temp_env: Path) -> None:
        """Should parse JSON values."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "json-test"])

        result = runner.invoke(main, ["session", "set", "--session", "json-test", "custom.count", "42"])

        assert result.exit_code == 1
        assert "custom.* is not supported" in result.output

    def test_set_null_clears(self, runner: CliRunner, temp_env: Path) -> None:
        """Should set null value (clears field)."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "null-test"])

        result = runner.invoke(main, ["session", "set", "--session", "null-test", "custom.flag", "null"])

        assert result.exit_code == 1
        assert "custom.* is not supported" in result.output

    def test_set_invalid_key_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail for invalid key."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "invalid-key-test"])

        result = runner.invoke(
            main, ["session", "set", "--session", "invalid-key-test", "confirmed.claude_session_id", "value"]
        )

        assert result.exit_code == 1
        assert "cannot override" in result.output or "Error" in result.output

    def test_set_no_session_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail when no session exists."""
        result = runner.invoke(main, ["session", "set", "policy.fail_mode", "closed"])

        assert result.exit_code == 1
        # Should mention no active session or session not found

    def test_set_with_session_option(self, runner: CliRunner, temp_env: Path) -> None:
        """Should accept --session option."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "target-session"])

        result = runner.invoke(
            main,
            ["session", "set", "--session", "target-session", "model_tier", "haiku"],
        )

        assert result.exit_code == 1
        assert "unknown field" in result.output


class TestSessionReset:
    """Tests for 'forge session reset' command."""

    def test_reset_single_key(self, runner: CliRunner, temp_env: Path) -> None:
        """Should reset a single override key."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "reset-single"])

        # Set an override first
        runner.invoke(main, ["session", "set", "--session", "reset-single", "policy.fail_mode", "closed"])

        # Reset it
        result = runner.invoke(main, ["session", "reset", "--session", "reset-single", "policy.fail_mode"])

        assert result.exit_code == 0
        assert "Reset" in result.output or "policy.fail_mode" in result.output

    def test_reset_all(self, runner: CliRunner, temp_env: Path) -> None:
        """Should reset all overrides with --all."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "reset-all"])

        # Set some overrides
        runner.invoke(main, ["session", "set", "--session", "reset-all", "policy.fail_mode", "closed"])
        # model_tier no longer exists; setting it should fail
        runner.invoke(main, ["session", "set", "--session", "reset-all", "model_tier", "haiku"])

        # Reset all
        result = runner.invoke(main, ["session", "reset", "--session", "reset-all", "--all"])

        assert result.exit_code == 0
        assert "Cleared" in result.output or "all" in result.output.lower()

    def test_reset_nonexistent_key_noop(self, runner: CliRunner, temp_env: Path) -> None:
        """Should be a no-op for key that isn't overridden."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "reset-noop"])

        result = runner.invoke(main, ["session", "reset", "--session", "reset-noop", "policy.fail_mode"])

        # Should succeed (no-op)
        assert result.exit_code == 0

    def test_reset_no_session_fails(self, runner: CliRunner, temp_env: Path) -> None:
        """Should fail when no session exists."""
        result = runner.invoke(main, ["session", "reset", "--session", "nonexistent", "policy.fail_mode"])

        assert result.exit_code == 1

    def test_reset_key_and_all_errors(self, runner: CliRunner, temp_env: Path) -> None:
        """Should error when both key and --all provided."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "reset-conflict"])

        result = runner.invoke(main, ["session", "reset", "--session", "reset-conflict", "policy.fail_mode", "--all"])

        assert result.exit_code == 1
        assert "Cannot specify both" in result.output or "conflict" in result.output.lower()

    def test_reset_no_args_clears_all(self, runner: CliRunner, temp_env: Path) -> None:
        """Reset with no args clears all overrides (same as --all)."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "reset-neither"])

        # Set some overrides first
        runner.invoke(main, ["session", "set", "--session", "reset-neither", "policy.fail_mode", "closed"])

        # Reset with no args
        result = runner.invoke(main, ["session", "reset", "--session", "reset-neither"])

        # Should succeed and clear all overrides
        assert result.exit_code == 0
        assert "Cleared" in result.output or "No overrides" in result.output


class TestInspectShowsOverrides:
    """Tests that show command displays override information."""

    def test_show_displays_overrides_section(self, runner: CliRunner, temp_env: Path) -> None:
        """Show should display active overrides."""
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "inspect-override"])

        # Set an override
        runner.invoke(main, ["session", "set", "--session", "inspect-override", "policy.fail_mode", "closed"])

        # Show
        result = runner.invoke(main, ["session", "show", "inspect-override"])

        assert result.exit_code == 0
        # Should show the override somehow
        assert "closed" in result.output or "override" in result.output.lower()


class TestTransactionalBehavior:
    """Tests that manifest is not modified when validation fails."""

    def test_no_write_on_invalid_type(self, runner: CliRunner, temp_env: Path) -> None:
        """Manifest file should be unchanged when set fails type validation."""
        import json

        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "transactional-test"])

        # Read manifest contents before (per-session directory)
        manifest_path = temp_env / ".forge" / "sessions" / "transactional-test" / "forge.session.json"
        content_before = manifest_path.read_text()
        data_before = json.loads(content_before)

        # Attempt to set an invalid value (tags should be list, not string)
        result = runner.invoke(
            main, ["session", "set", "--session", "transactional-test", "memory.tags", '"not-a-list"']
        )

        # Should fail
        assert result.exit_code == 1

        # Manifest should be unchanged
        content_after = manifest_path.read_text()
        data_after = json.loads(content_after)

        # Core fields should be identical
        assert data_before["name"] == data_after["name"]
        assert data_before["overrides"] == data_after["overrides"]

    def test_no_write_on_invalid_key(self, runner: CliRunner, temp_env: Path) -> None:
        """Manifest file should be unchanged when set fails key validation."""

        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "invalid-key-transact"])

        manifest_path = temp_env / ".forge" / "sessions" / "invalid-key-transact" / "forge.session.json"
        content_before = manifest_path.read_text()

        # Attempt to set a confirmed field (should be rejected)
        result = runner.invoke(main, ["session", "set", "--session", "invalid-key-transact", "confirmed.foo", "bar"])

        assert result.exit_code == 1

        # Manifest should be unchanged
        content_after = manifest_path.read_text()
        assert content_before == content_after


class TestCwdGuardWiring:
    """Verify session commands call the correct CWD guard."""

    def test_start_calls_require_repo_root(self, runner: CliRunner, temp_env: Path) -> None:
        with (
            patch("forge.cli.guards.require_repo_root", return_value=temp_env) as mock_rr,
            patch("forge.cli.guards.require_main_repo_root") as mock_mrr,
            patch("forge.cli.session.invoke_claude", return_value=0),
        ):
            runner.invoke(main, ["session", "start", "guard-test"])
        mock_rr.assert_called_once()
        mock_mrr.assert_not_called()

    def test_start_worktree_calls_require_main_repo_root(self, runner: CliRunner, temp_env: Path) -> None:
        with (
            patch("forge.cli.guards.require_repo_root") as mock_rr,
            patch("forge.cli.guards.require_main_repo_root", return_value=temp_env) as mock_mrr,
            patch("forge.cli.session.invoke_claude", return_value=0),
            patch("forge.session.worktree.get_main_repo_root", return_value=temp_env),
            patch("forge.session.worktree.create_worktree") as mock_wt,
            patch("forge.session.worktree.copy_runtime_config"),
        ):
            from forge.session.worktree.create import WorktreeResult

            mock_wt.return_value = WorktreeResult(
                worktree_path=str(temp_env / "wt"),
                branch="guard-wt-test",
                created_branch=True,
            )
            (temp_env / "wt").mkdir()
            runner.invoke(main, ["session", "start", "guard-wt-test", "--worktree", "--no-proxy", "--no-launch"])
        mock_mrr.assert_called_once()
        mock_rr.assert_not_called()

    def test_fork_calls_require_repo_root(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "fork-parent", "--no-proxy", "--no-launch"])

        with (
            patch("forge.cli.guards.require_repo_root", return_value=temp_env) as mock_rr,
            patch("forge.cli.guards.require_main_repo_root") as mock_mrr,
            patch("forge.cli.session.invoke_claude", return_value=0),
        ):
            runner.invoke(main, ["session", "fork", "fork-parent", "--name", "fork-child", "--no-proxy"])
        mock_rr.assert_called_once()
        mock_mrr.assert_not_called()

    def test_fork_worktree_calls_require_main_repo_root(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "fork-wt-parent", "--no-proxy", "--no-launch"])

        with (
            patch("forge.cli.guards.require_repo_root") as mock_rr,
            patch("forge.cli.guards.require_main_repo_root", return_value=temp_env) as mock_mrr,
        ):
            # Don't need full worktree setup — guard is called before fork_session
            runner.invoke(
                main, ["session", "fork", "fork-wt-parent", "--name", "fork-wt-child", "--worktree", "--no-proxy"]
            )
        mock_mrr.assert_called_once()
        mock_rr.assert_not_called()

    def test_fork_into_skips_guards(self, runner: CliRunner, temp_env: Path) -> None:
        with patch("forge.cli.session.invoke_claude", return_value=0):
            runner.invoke(main, ["session", "start", "fork-into-parent", "--no-proxy", "--no-launch"])

        with (
            patch("forge.cli.guards.require_repo_root") as mock_rr,
            patch("forge.cli.guards.require_main_repo_root") as mock_mrr,
        ):
            # --into has its own validation; CWD guards should not be called
            runner.invoke(main, ["session", "fork", "fork-into-parent", "--into", str(temp_env)])
        mock_rr.assert_not_called()
        mock_mrr.assert_not_called()

    def test_incognito_calls_require_repo_root(self, runner: CliRunner, temp_env: Path) -> None:
        with (
            patch("forge.cli.guards.require_repo_root", return_value=temp_env) as mock_rr,
            patch("forge.cli.session.invoke_claude", return_value=0),
        ):
            runner.invoke(main, ["session", "incognito", "guard-incog", "--no-proxy"])
        mock_rr.assert_called_once()
