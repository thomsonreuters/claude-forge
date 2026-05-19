"""Tests for forge.session.prev_sessions layout helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.session.prev_sessions import (
    CHILDREN_DIR,
    GENERATED_FILENAME,
    PREV_SESSIONS_DIR,
    child_path,
    child_path_rel,
    children_dir,
    ensure_child,
    generated_path,
    generated_path_rel,
    iter_children,
    iter_legacy_flat_files,
    iter_parents,
    parent_dir,
    prev_sessions_root,
)


class TestPathHelpers:
    def test_prev_sessions_root_under_forge_dir(self, tmp_path: Path) -> None:
        assert prev_sessions_root(tmp_path) == tmp_path / ".forge" / PREV_SESSIONS_DIR

    def test_parent_dir_layout(self, tmp_path: Path) -> None:
        assert parent_dir(tmp_path, "p1") == tmp_path / ".forge" / PREV_SESSIONS_DIR / "p1"

    def test_generated_path_under_parent(self, tmp_path: Path) -> None:
        expected = tmp_path / ".forge" / PREV_SESSIONS_DIR / "p1" / GENERATED_FILENAME
        assert generated_path(tmp_path, "p1") == expected

    def test_children_dir_under_parent(self, tmp_path: Path) -> None:
        expected = tmp_path / ".forge" / PREV_SESSIONS_DIR / "p1" / CHILDREN_DIR
        assert children_dir(tmp_path, "p1") == expected

    def test_child_path_under_children_dir(self, tmp_path: Path) -> None:
        expected = tmp_path / ".forge" / PREV_SESSIONS_DIR / "p1" / CHILDREN_DIR / "c1.md"
        assert child_path(tmp_path, "p1", "c1") == expected

    def test_generated_path_rel_format(self) -> None:
        assert generated_path_rel("p1") == ".forge/prev_sessions/p1/generated.md"

    def test_child_path_rel_format(self) -> None:
        assert child_path_rel("p1", "c1") == ".forge/prev_sessions/p1/children/c1.md"


class TestEnsureChild:
    def test_copies_generated_when_child_absent(self, tmp_path: Path) -> None:
        gen = generated_path(tmp_path, "p1")
        gen.parent.mkdir(parents=True)
        gen.write_text("cache content")

        target = ensure_child(tmp_path, "p1", "c1")

        assert target.is_file()
        assert target.read_text() == "cache content"
        assert target == child_path(tmp_path, "p1", "c1")

    def test_idempotent_when_child_exists(self, tmp_path: Path) -> None:
        """Existing child file is the durability guarantee -- don't overwrite."""
        gen = generated_path(tmp_path, "p1")
        gen.parent.mkdir(parents=True)
        gen.write_text("fresh cache")

        # Seed an existing child with user edits
        existing_child = child_path(tmp_path, "p1", "c1")
        existing_child.parent.mkdir(parents=True)
        existing_child.write_text("MY USER EDITS")

        target = ensure_child(tmp_path, "p1", "c1")

        assert target.read_text() == "MY USER EDITS"

    def test_raises_when_generated_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ensure_child(tmp_path, "p1", "c1")


class TestIterators:
    def test_iter_parents_empty_when_no_dir(self, tmp_path: Path) -> None:
        assert list(iter_parents(tmp_path)) == []

    def test_iter_parents_yields_directories(self, tmp_path: Path) -> None:
        for name in ("p1", "p2"):
            (tmp_path / ".forge" / PREV_SESSIONS_DIR / name).mkdir(parents=True)
        # Add a flat file at the top -- should be skipped
        flat = tmp_path / ".forge" / PREV_SESSIONS_DIR / "legacy.md"
        flat.write_text("legacy")

        names = sorted(p.name for p in iter_parents(tmp_path))
        assert names == ["p1", "p2"]

    def test_iter_children_empty_when_no_dir(self, tmp_path: Path) -> None:
        assert list(iter_children(tmp_path, "missing-parent")) == []

    def test_iter_children_yields_md_files(self, tmp_path: Path) -> None:
        dir_ = children_dir(tmp_path, "p1")
        dir_.mkdir(parents=True)
        (dir_ / "c1.md").write_text("a")
        (dir_ / "c2.md").write_text("b")
        # Non-md siblings should not be yielded
        (dir_ / "notes.txt").write_text("ignored")

        names = sorted(p.stem for p in iter_children(tmp_path, "p1"))
        assert names == ["c1", "c2"]

    def test_iter_legacy_flat_files_empty_when_no_dir(self, tmp_path: Path) -> None:
        assert list(iter_legacy_flat_files(tmp_path)) == []

    def test_iter_legacy_flat_files_yields_top_level_md(self, tmp_path: Path) -> None:
        root = tmp_path / ".forge" / PREV_SESSIONS_DIR
        root.mkdir(parents=True)
        (root / "p1.md").write_text("legacy a")
        (root / "p2.md").write_text("legacy b")
        # New-layout directory should not be yielded
        (root / "p3").mkdir()
        (root / "p3" / "generated.md").write_text("new-layout cache")
        # Non-md files at top level should not be yielded either
        (root / "data.json").write_text("{}")

        names = sorted(p.stem for p in iter_legacy_flat_files(tmp_path))
        assert names == ["p1", "p2"]
