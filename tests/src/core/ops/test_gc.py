"""Tests for forge.core.ops.gc — garbage collection operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.core.ops.context import ExecutionContext
from forge.core.ops.gc import (
    CleanError,
    CleanReport,
    _detect_dead_installations,
    _detect_orphan_handoff_files,
    _detect_orphan_session_dirs,
    _detect_stale_active_entries,
    _detect_stale_work_queue,
    _path_in_roots,
    _resolve_tracked_roots,
    collect_clean_report,
    run_clean,
)
from forge.session import IndexStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _forge_home() -> Path:
    """Return the test-isolated FORGE_HOME (set by autouse fixture)."""
    return Path(os.environ["FORGE_HOME"])


def _seed_session(tmp_path: Path, name: str, forge_root: Path | None = None) -> Path:
    """Create a session in the index + on disk. Returns the forge_root."""
    fr = forge_root or tmp_path / "project"
    session_dir = fr / ".forge" / "sessions" / name
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "forge.session.json").write_text("{}")

    index = IndexStore()
    index.add_session(
        name=name,
        worktree_path=str(fr),
        project_root=str(tmp_path),
        forge_root=str(fr),
        checkout_root=str(fr),
        relative_path=".",
        is_incognito=False,
        is_fork=False,
        parent_session=None,
    )
    return fr


def _make_ctx(tmp_path: Path, forge_root: Path | None = None) -> ExecutionContext:
    return ExecutionContext(
        cwd=tmp_path,
        worktree_root=tmp_path,
        project_root=tmp_path,
        forge_root=forge_root,
    )


# ---------------------------------------------------------------------------
# _resolve_tracked_roots
# ---------------------------------------------------------------------------


class TestResolveTrackedRoots:
    def test_scope_project_requires_forge_root(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, forge_root=None)
        with pytest.raises(CleanError, match="Not inside a Forge project"):
            _resolve_tracked_roots(ctx, "project")

    def test_scope_project_returns_forge_root(self, tmp_path: Path) -> None:
        fr = tmp_path / "project"
        fr.mkdir()
        ctx = _make_ctx(tmp_path, forge_root=fr)
        roots = _resolve_tracked_roots(ctx, "project")
        assert roots == {fr}

    def test_scope_repo_includes_index_entries(self, tmp_path: Path) -> None:
        fr = _seed_session(tmp_path, "alpha")
        ctx = _make_ctx(tmp_path, forge_root=fr)
        roots = _resolve_tracked_roots(ctx, "repo")
        assert fr in roots

    def test_scope_all_includes_all_entries(self, tmp_path: Path) -> None:
        fr1 = _seed_session(tmp_path, "alpha", tmp_path / "proj-a")
        fr2 = _seed_session(tmp_path, "beta", tmp_path / "proj-b")
        ctx = _make_ctx(tmp_path)
        roots = _resolve_tracked_roots(ctx, "all")
        assert fr1 in roots
        assert fr2 in roots


# ---------------------------------------------------------------------------
# _detect_orphan_session_dirs
# ---------------------------------------------------------------------------


class TestDetectOrphanSessionDirs:
    def test_no_orphans(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")
        ref_set = {("alpha", str(fr))}
        result = _detect_orphan_session_dirs(ref_set, {fr})
        assert result.count == 0

    def test_detects_orphan(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")

        # Create an extra session dir not in the index
        orphan_dir = fr / ".forge" / "sessions" / "ghost"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "forge.session.json").write_text("{}")

        ref_set = {("alpha", str(fr))}
        result = _detect_orphan_session_dirs(ref_set, {fr})
        assert result.count == 1
        assert str(orphan_dir) in result.items[0]

    def test_skips_empty_dirs(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")

        # Empty dir should be ignored
        empty_dir = fr / ".forge" / "sessions" / "empty"
        empty_dir.mkdir(parents=True)

        ref_set = {("alpha", str(fr))}
        result = _detect_orphan_session_dirs(ref_set, {fr})
        assert result.count == 0

    def test_name_reuse_across_forge_roots(self, tmp_path: Path) -> None:
        """Same name in different forge_roots should not mask each other."""

        fr_a = tmp_path / "proj-a"
        fr_b = tmp_path / "proj-b"
        _seed_session(tmp_path, "alpha", fr_a)

        # Create "alpha" dir in proj-b (not in index for proj-b)
        orphan_dir = fr_b / ".forge" / "sessions" / "alpha"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "forge.session.json").write_text("{}")

        ref_set = {("alpha", str(fr_a))}
        result = _detect_orphan_session_dirs(ref_set, {fr_a, fr_b})
        assert result.count == 1
        assert str(orphan_dir) in result.items[0]


# ---------------------------------------------------------------------------
# _detect_orphan_handoff_files
# ---------------------------------------------------------------------------


class TestDetectOrphanHandoffFiles:
    def test_no_orphans(self, tmp_path: Path) -> None:
        fr = _seed_session(tmp_path, "parent")

        # New layout: <parent>/generated.md + <parent>/children/<child>.md
        # Only "parent" is in the ref_set, but a self-resume (child = parent
        # name reuse via auto-gen suffix) wouldn't happen; for "no orphans"
        # we simulate a parent dir with no children yet.
        parent_dir = fr / ".forge" / "prev_sessions" / "parent"
        parent_dir.mkdir(parents=True)
        (parent_dir / "generated.md").write_text("# Cache")

        ref_set = {("parent", str(fr))}
        result = _detect_orphan_handoff_files(ref_set, {fr})
        assert result.count == 0

    def test_detects_orphan_parent_dir(self, tmp_path: Path) -> None:
        fr = _seed_session(tmp_path, "alpha")

        # Parent dir for a session that isn't in the index -- whole dir orphan
        orphan_parent = fr / ".forge" / "prev_sessions" / "deleted-parent"
        orphan_parent.mkdir(parents=True)
        (orphan_parent / "generated.md").write_text("# Stale cache")

        ref_set = {("alpha", str(fr))}
        result = _detect_orphan_handoff_files(ref_set, {fr})
        assert result.count == 1
        assert "deleted-parent" in result.items[0]

    def test_detects_orphan_child_file(self, tmp_path: Path) -> None:
        # Both parent and one valid child are in the index; an additional
        # child file under the same parent is orphaned (e.g., child was
        # deleted from the index but the file lingered).
        fr = _seed_session(tmp_path, "parent")
        _seed_session(tmp_path, "live-child", forge_root=fr)
        from forge.session import SessionStore, create_session_state
        from forge.session.models import Derivation

        live_state = create_session_state("live-child", worktree_path=str(fr))
        live_state.forge_root = str(fr)
        live_state.confirmed.derivation = Derivation(
            parent_session="parent",
            resume_mode="handoff",
            context_file=".forge/prev_sessions/parent/children/live-child.md",
        )
        SessionStore(str(fr), "live-child").write(live_state)

        parent_dir = fr / ".forge" / "prev_sessions" / "parent"
        parent_dir.mkdir(parents=True)
        (parent_dir / "generated.md").write_text("# Cache")
        children_dir = parent_dir / "children"
        children_dir.mkdir()
        (children_dir / "live-child.md").write_text("# Live child")
        (children_dir / "deleted-child.md").write_text("# Orphan child")

        ref_set = {("parent", str(fr)), ("live-child", str(fr))}
        result = _detect_orphan_handoff_files(ref_set, {fr})
        assert result.count == 1
        assert "deleted-child.md" in result.items[0]

    def test_preserves_cross_root_child_file_referenced_by_derivation(self, tmp_path: Path) -> None:
        parent_root = _seed_session(tmp_path, "parent")
        child_root = tmp_path / "child-root"
        child_root.mkdir()

        from forge.session import SessionStore, create_session_state
        from forge.session.models import Derivation

        child_state = create_session_state("child", worktree_path=str(child_root), parent_session="parent")
        child_state.forge_root = str(child_root)
        child_state.confirmed.derivation = Derivation(
            parent_session="parent",
            resume_mode="handoff",
            context_file=".forge/prev_sessions/parent/children/child.md",
        )
        SessionStore(str(child_root), "child").write(child_state)

        parent_dir = child_root / ".forge" / "prev_sessions" / "parent"
        children_dir = parent_dir / "children"
        children_dir.mkdir(parents=True)
        (parent_dir / "generated.md").write_text("# Cache")
        live_child = children_dir / "child.md"
        live_child.write_text("# Live context")
        stale_child = children_dir / "stale.md"
        stale_child.write_text("# Stale context")

        ref_set = {("parent", str(parent_root)), ("child", str(child_root))}
        result = _detect_orphan_handoff_files(ref_set, {parent_root, child_root})

        assert str(parent_dir) not in result.items
        assert str(live_child) not in result.items
        assert result.items == [str(stale_child)]

    def test_existing_session_name_does_not_keep_unreferenced_child_file(self, tmp_path: Path) -> None:
        fr = _seed_session(tmp_path, "parent")
        _seed_session(tmp_path, "native-child", forge_root=fr)

        parent_dir = fr / ".forge" / "prev_sessions" / "parent"
        children_dir = parent_dir / "children"
        children_dir.mkdir(parents=True)
        (parent_dir / "generated.md").write_text("# Cache")
        stale = children_dir / "native-child.md"
        stale.write_text("# Stale context")

        ref_set = {("parent", str(fr)), ("native-child", str(fr))}
        result = _detect_orphan_handoff_files(ref_set, {fr})

        assert result.count == 1
        assert result.items == [str(stale)]

    def test_detects_legacy_flat_files(self, tmp_path: Path) -> None:
        # Pre-0.2.0 flat <parent>.md files at the top of prev_sessions/ are
        # always orphans -- new code never writes there.
        fr = _seed_session(tmp_path, "alpha")

        prev_dir = fr / ".forge" / "prev_sessions"
        prev_dir.mkdir(parents=True)
        (prev_dir / "alpha.md").write_text("# Legacy")  # parent is in index, still orphan
        (prev_dir / "old-deleted.md").write_text("# Legacy")

        ref_set = {("alpha", str(fr))}
        result = _detect_orphan_handoff_files(ref_set, {fr})
        assert result.count == 2

    def test_ignores_non_md_files(self, tmp_path: Path) -> None:
        fr = tmp_path / "project"
        prev_dir = fr / ".forge" / "prev_sessions"
        prev_dir.mkdir(parents=True)
        (prev_dir / "data.json").write_text("{}")

        result = _detect_orphan_handoff_files(set(), {fr})
        assert result.count == 0


# ---------------------------------------------------------------------------
# _detect_stale_active_entries
# ---------------------------------------------------------------------------


class TestDetectStaleActiveEntries:
    def test_no_entries(self, tmp_path: Path) -> None:
        result = _detect_stale_active_entries({tmp_path})
        assert result.count == 0

    def test_detects_dead_pid(self, tmp_path: Path) -> None:
        from forge.session.active import ActiveSessionStore
        from forge.session.config import LAUNCH_MODE_HOST

        store = ActiveSessionStore()
        store.upsert_session(
            session_name="dead-session",
            worktree_path=str(tmp_path),
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=999999,  # almost certainly dead
        )

        result = _detect_stale_active_entries({tmp_path})
        assert result.count == 1
        # Items encoded as "name::forge_root" for scoped cleanup
        assert any(item.startswith("dead-session::") for item in result.items)


# ---------------------------------------------------------------------------
# _detect_stale_work_queue
# ---------------------------------------------------------------------------


class TestDetectStaleWorkQueue:
    def test_no_markers(self, tmp_path: Path) -> None:
        result = _detect_stale_work_queue(set(), {tmp_path})
        assert result.count == 0

    def test_detects_orphan_marker(self, tmp_path: Path) -> None:
        queue_dir = _forge_home() / "pending-work"
        queue_dir.mkdir(parents=True)
        marker = {
            "schema_version": 1,
            "kind": "stop",
            "marker_id": "test-marker",
            "payload": {
                "session_name": "deleted-session",
                "worktree_path": str(tmp_path),
            },
        }
        (queue_dir / "test-marker.json").write_text(json.dumps(marker))

        # No sessions in ref_set
        result = _detect_stale_work_queue(set(), {tmp_path})
        assert result.count == 1

    def test_skips_valid_marker(self, tmp_path: Path) -> None:
        queue_dir = _forge_home() / "pending-work"
        queue_dir.mkdir(parents=True)
        marker = {
            "schema_version": 1,
            "kind": "stop",
            "marker_id": "test-marker",
            "payload": {
                "session_name": "alive-session",
                "worktree_path": str(tmp_path),
            },
        }
        (queue_dir / "test-marker.json").write_text(json.dumps(marker))

        ref_set = {("alive-session", str(tmp_path))}
        result = _detect_stale_work_queue(ref_set, {tmp_path})
        assert result.count == 0

    def test_scopes_by_worktree_path(self, tmp_path: Path) -> None:
        queue_dir = _forge_home() / "pending-work"
        queue_dir.mkdir(parents=True)
        marker = {
            "schema_version": 1,
            "kind": "stop",
            "marker_id": "other-project",
            "payload": {
                "session_name": "deleted-session",
                "worktree_path": "/some/other/project",
            },
        }
        (queue_dir / "other-project.json").write_text(json.dumps(marker))

        # Scope roots don't include /some/other/project
        result = _detect_stale_work_queue(set(), {tmp_path})
        assert result.count == 0

    def test_valid_marker_survives_when_forge_root_differs_from_worktree(self, tmp_path: Path) -> None:
        queue_dir = _forge_home() / "pending-work"
        queue_dir.mkdir(parents=True)

        forge_root = tmp_path / "forge-project"
        worktree_path = tmp_path / "checkout"
        marker = {
            "schema_version": 1,
            "kind": "stop",
            "marker_id": "live-marker",
            "payload": {
                "session_name": "alive-session",
                "worktree_path": str(worktree_path),
            },
        }
        (queue_dir / "live-marker.json").write_text(json.dumps(marker))

        forge_ref_set = {("alive-session", str(forge_root))}
        assert _detect_stale_work_queue(forge_ref_set, {tmp_path}).count == 1

        worktree_ref_set = {("alive-session", str(worktree_path))}
        assert _detect_stale_work_queue(worktree_ref_set, {tmp_path}).count == 0


# ---------------------------------------------------------------------------
# _path_in_roots
# ---------------------------------------------------------------------------


class TestPathInRoots:
    def test_exact_match(self, tmp_path: Path) -> None:
        assert _path_in_roots(tmp_path, {tmp_path}) is True

    def test_child_match(self, tmp_path: Path) -> None:
        child = tmp_path / "sub" / "dir"
        child.mkdir(parents=True)
        assert _path_in_roots(child, {tmp_path}) is True

    def test_no_match(self, tmp_path: Path) -> None:
        other = tmp_path.parent / "other"
        assert _path_in_roots(other, {tmp_path}) is False

    def test_empty_roots_matches_nothing(self, tmp_path: Path) -> None:
        """Empty root set returns False to prevent scope widening (P1 fix)."""
        assert _path_in_roots(tmp_path, set()) is False


# ---------------------------------------------------------------------------
# collect_clean_report
# ---------------------------------------------------------------------------


class TestCollectCleanReport:
    def test_clean_repo(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")
        ctx = _make_ctx(tmp_path, forge_root=fr)
        report = collect_clean_report(ctx=ctx, scope="repo")
        assert isinstance(report, CleanReport)
        assert report.is_clean

    def test_invalid_scope_raises(self, tmp_path: Path) -> None:

        ctx = _make_ctx(tmp_path)
        with pytest.raises(CleanError, match="Invalid scope"):
            collect_clean_report(ctx=ctx, scope="invalid")

    def test_scope_project_no_forge_root_raises(self, tmp_path: Path) -> None:

        ctx = _make_ctx(tmp_path, forge_root=None)
        with pytest.raises(CleanError, match="Not inside a Forge project"):
            collect_clean_report(ctx=ctx, scope="project")

    def test_detects_orphan_session_dir(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")

        # Create orphan
        orphan = fr / ".forge" / "sessions" / "ghost"
        orphan.mkdir(parents=True)
        (orphan / "forge.session.json").write_text("{}")

        ctx = _make_ctx(tmp_path, forge_root=fr)
        report = collect_clean_report(ctx=ctx, scope="repo")
        session_cat = next(c for c in report.categories if c.category == "session_dirs")
        assert session_cat.count == 1


# ---------------------------------------------------------------------------
# run_clean
# ---------------------------------------------------------------------------


class TestRunClean:
    def test_cleans_orphan_session_dir(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")

        orphan = fr / ".forge" / "sessions" / "ghost"
        orphan.mkdir(parents=True)
        (orphan / "forge.session.json").write_text("{}")
        assert orphan.exists()

        ctx = _make_ctx(tmp_path, forge_root=fr)
        result = run_clean(ctx=ctx, scope="repo")

        assert result.deleted_count >= 1
        assert not orphan.exists()
        assert "session_dirs" in result.categories_cleaned

    def test_cleans_orphan_handoff_file(self, tmp_path: Path) -> None:
        # Orphan parent dir (parent not in index) -- whole dir is cleaned.
        fr = _seed_session(tmp_path, "alpha")

        orphan_parent = fr / ".forge" / "prev_sessions" / "deleted-parent"
        orphan_parent.mkdir(parents=True)
        (orphan_parent / "generated.md").write_text("# Stale cache")
        (orphan_parent / "children").mkdir()
        (orphan_parent / "children" / "deleted-child.md").write_text("# Stale child")
        assert orphan_parent.exists()

        ctx = _make_ctx(tmp_path, forge_root=fr)
        result = run_clean(ctx=ctx, scope="repo")

        assert result.deleted_count >= 1
        assert not orphan_parent.exists()
        assert "handoff_files" in result.categories_cleaned

    def test_cleans_legacy_flat_handoff_file(self, tmp_path: Path) -> None:
        # Legacy pre-0.2.0 flat <parent>.md files at the top of prev_sessions/.
        # GC treats these as orphans regardless of whether parent is in index.
        fr = _seed_session(tmp_path, "alpha")

        prev_dir = fr / ".forge" / "prev_sessions"
        prev_dir.mkdir(parents=True)
        legacy = prev_dir / "alpha.md"
        legacy.write_text("# Legacy flat file")
        assert legacy.exists()

        ctx = _make_ctx(tmp_path, forge_root=fr)
        result = run_clean(ctx=ctx, scope="repo")

        assert result.deleted_count >= 1
        assert not legacy.exists()
        assert "handoff_files" in result.categories_cleaned

    def test_cleans_empty_dirs_after_child_unlink(self, tmp_path: Path) -> None:
        # Removing the last orphan child should also prune empty children/ and
        # the parent dir (which only has the generated.md cache left).
        fr = _seed_session(tmp_path, "alpha")

        parent_dir = fr / ".forge" / "prev_sessions" / "alpha"
        parent_dir.mkdir(parents=True)
        (parent_dir / "generated.md").write_text("# Cache")
        children_dir = parent_dir / "children"
        children_dir.mkdir()
        (children_dir / "deleted-child.md").write_text("# Orphan")

        ctx = _make_ctx(tmp_path, forge_root=fr)
        result = run_clean(ctx=ctx, scope="repo")

        assert "handoff_files" in result.categories_cleaned
        # children/ removed; parent dir removed because only generated.md remained
        assert not children_dir.exists()
        assert not parent_dir.exists()

    def test_nothing_to_clean(self, tmp_path: Path) -> None:

        fr = _seed_session(tmp_path, "alpha")
        ctx = _make_ctx(tmp_path, forge_root=fr)
        result = run_clean(ctx=ctx, scope="repo")
        assert result.deleted_count == 0
        assert not result.failed


# ---------------------------------------------------------------------------
# Edge case tests (P1-P3 fixes)
# ---------------------------------------------------------------------------


class TestEmptyRootRepoScope:
    """P1: repo scope with no tracked roots should not widen to global."""

    def test_empty_repo_no_active_cleanup(self, tmp_path: Path) -> None:
        """Active entries outside scope are untouched when no roots found."""
        from forge.session.active import ActiveSessionStore
        from forge.session.config import LAUNCH_MODE_HOST

        # Create a stale active entry pointing to some other project
        store = ActiveSessionStore()
        store.upsert_session(
            session_name="other-project-session",
            worktree_path="/some/other/project",
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=999999,
        )

        # Run clean from a repo with no forge roots
        ctx = _make_ctx(tmp_path, forge_root=None)
        report = collect_clean_report(ctx=ctx, scope="repo")
        active_cat = next(c for c in report.categories if c.category == "active_entries")
        assert active_cat.count == 0, "should not detect entries outside scope"

    def test_empty_repo_no_workqueue_cleanup(self, tmp_path: Path) -> None:
        """Work queue markers outside scope are untouched when no roots found."""
        queue_dir = _forge_home() / "pending-work"
        queue_dir.mkdir(parents=True)

        marker = {
            "schema_version": 1,
            "kind": "stop",
            "marker_id": "foreign",
            "payload": {"session_name": "gone", "worktree_path": "/other/repo"},
        }
        (queue_dir / "foreign.json").write_text(json.dumps(marker))

        ctx = _make_ctx(tmp_path, forge_root=None)
        report = collect_clean_report(ctx=ctx, scope="repo")
        wq_cat = next(c for c in report.categories if c.category == "work_queue")
        assert wq_cat.count == 0


class TestScopedActiveCleanup:
    """P1: active cleanup should only remove detected entries, not global."""

    def test_only_scoped_entries_removed(self, tmp_path: Path) -> None:
        from forge.session.active import ActiveSessionStore
        from forge.session.config import LAUNCH_MODE_HOST

        store = ActiveSessionStore()

        # Stale entry IN scope
        fr = _seed_session(tmp_path, "alpha")
        store.upsert_session(
            session_name="in-scope-dead",
            worktree_path=str(fr),
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=999999,
        )
        # Stale entry OUTSIDE scope
        store.upsert_session(
            session_name="out-of-scope-dead",
            worktree_path="/other/project",
            launch_mode=LAUNCH_MODE_HOST,
            launcher_pid=999998,
        )

        ctx = _make_ctx(tmp_path, forge_root=fr)
        run_clean(ctx=ctx, scope="repo")

        # in-scope should be cleaned
        assert store.get_session("in-scope-dead") is None
        # out-of-scope should survive (keys are scoped compound keys)
        remaining = store.read()
        assert any(k.startswith("out-of-scope-dead|") for k in remaining.sessions)


class TestYesJsonMode:
    """P2: --yes --json should actually perform cleanup."""

    def test_yes_json_runs_clean(self) -> None:
        from forge.core.ops.gc import CleanResult

        report = _make_report_with_orphans(1)
        clean_result = CleanResult(categories_cleaned={"session_dirs": 1}, failed=[])

        with (
            patch("forge.cli.gc.ExecutionContext.from_cwd"),
            patch("forge.cli.gc.collect_clean_report", return_value=report),
            patch("forge.cli.gc.run_clean", return_value=clean_result) as mock_run,
        ):
            from click.testing import CliRunner

            from forge.cli.gc import clean_cmd

            runner = CliRunner()
            result = runner.invoke(clean_cmd, ["--yes", "--json"])
            assert result.exit_code == 0
            mock_run.assert_called_once()
            data = json.loads(result.output)
            assert data["dry_run"] is False
            assert data["deleted"] == 1

    def test_json_dryrun_does_not_clean(self) -> None:
        report = _make_report_with_orphans(1)

        with (
            patch("forge.cli.gc.ExecutionContext.from_cwd"),
            patch("forge.cli.gc.collect_clean_report", return_value=report),
            patch("forge.cli.gc.run_clean") as mock_run,
        ):
            from click.testing import CliRunner

            from forge.cli.gc import clean_cmd

            runner = CliRunner()
            result = runner.invoke(clean_cmd, ["--json"])
            assert result.exit_code == 0
            mock_run.assert_not_called()
            data = json.loads(result.output)
            assert data["dry_run"] is True


class TestCrossRootNameReuse:
    """P2: name reuse across forge_roots for handoff and work-queue."""

    def test_handoff_name_reuse_across_roots(self, tmp_path: Path) -> None:
        """alpha parent dir in proj-a should not mask orphan alpha parent dir in proj-b."""
        fr_a = _seed_session(tmp_path, "alpha", tmp_path / "proj-a")
        fr_b = tmp_path / "proj-b"
        orphan_dir = fr_b / ".forge" / "prev_sessions" / "alpha"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "generated.md").write_text("stale")

        ref_set = {("alpha", str(fr_a))}
        result = _detect_orphan_handoff_files(ref_set, {fr_a, fr_b})
        assert result.count == 1
        assert "proj-b" in result.items[0]

    def test_workqueue_name_reuse_across_roots(self, tmp_path: Path) -> None:
        """Marker for alpha in proj-b should be stale even if alpha exists in proj-a."""
        fr_a = _seed_session(tmp_path, "alpha", tmp_path / "proj-a")

        queue_dir = _forge_home() / "pending-work"
        queue_dir.mkdir(parents=True)
        marker = {
            "schema_version": 1,
            "kind": "stop",
            "marker_id": "cross-root",
            "payload": {
                "session_name": "alpha",
                "worktree_path": str(tmp_path / "proj-b"),
            },
        }
        (queue_dir / "cross-root.json").write_text(json.dumps(marker))

        ref_set = {("alpha", str(fr_a))}
        scope_roots = {fr_a, tmp_path / "proj-b"}
        result = _detect_stale_work_queue(ref_set, scope_roots)
        assert result.count == 1


# ---------------------------------------------------------------------------
# Helpers for CLI tests
# ---------------------------------------------------------------------------


class TestDetectDeadInstallations:
    """Dead installed-manifest entries for paths that no longer exist."""

    def test_no_dead_entries(self, tmp_path: Path) -> None:
        result = _detect_dead_installations()
        assert result.count == 0

    def test_detects_dead_entry(self, tmp_path: Path) -> None:
        from forge.install.models import Installation
        from forge.install.tracking import TrackingStore

        store = TrackingStore()
        store.set_installation(
            "local",
            Installation(
                scope="local",
                mode="copy",
                profile="standard",
                project_path="/nonexistent/dead-worktree",
            ),
            project_path="/nonexistent/dead-worktree",
        )

        result = _detect_dead_installations()
        assert result.count == 1
        assert "dead-worktree" in result.items[0]

    def test_skips_live_entry(self, tmp_path: Path) -> None:
        from forge.install.models import Installation
        from forge.install.tracking import TrackingStore

        live_path = tmp_path / "alive"
        live_path.mkdir()
        store = TrackingStore()
        store.set_installation(
            "local",
            Installation(
                scope="local",
                mode="copy",
                profile="standard",
                project_path=str(live_path),
            ),
            project_path=str(live_path),
        )

        result = _detect_dead_installations()
        assert result.count == 0

    def test_run_clean_removes_dead_installation(self, tmp_path: Path) -> None:
        from forge.install.models import Installation
        from forge.install.tracking import TrackingStore

        store = TrackingStore()
        store.set_installation(
            "local",
            Installation(
                scope="local",
                mode="copy",
                profile="standard",
                project_path="/nonexistent/dead",
            ),
            project_path="/nonexistent/dead",
        )

        # Also need a forge_root for the run_clean context
        fr = _seed_session(tmp_path, "alpha")
        ctx = _make_ctx(tmp_path, forge_root=fr)
        result = run_clean(ctx=ctx, scope="all")

        assert "dead_installations" in result.categories_cleaned
        # Verify it was removed from the manifest
        manifest = store.read()
        assert len([i for _k, i in manifest.installations.items() if i.project_path == "/nonexistent/dead"]) == 0


# ---------------------------------------------------------------------------
# Helpers for CLI tests
# ---------------------------------------------------------------------------


def _make_report_with_orphans(total: int) -> CleanReport:
    """Build a CleanReport with orphan session dirs."""
    from forge.core.ops.gc import OrphanCategory

    cats = [
        OrphanCategory("session_dirs", "Orphan session dirs", total, ["/fake"] * total),
        OrphanCategory("handoff_files", "Orphan handoff files", 0, []),
        OrphanCategory("active_entries", "Stale active entries", 0, []),
        OrphanCategory("work_queue", "Stale work queue", 0, []),
        OrphanCategory("proxies", "Stale proxy entries", 0, []),
        OrphanCategory("search_docs", "Orphan search docs", 0, []),
        OrphanCategory("dead_installations", "Dead installations", 0, []),
    ]
    return CleanReport(categories=cats, scope="repo")
