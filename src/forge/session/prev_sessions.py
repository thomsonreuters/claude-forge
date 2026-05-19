"""Path layout for resume/fork context files (prev_sessions).

Centralizes the on-disk layout so process_handoff, SessionManager, fork paths,
and GC stay in sync.

Layout::

    <forge_root>/.forge/prev_sessions/
    +-- <parent>/
        +-- generated.md          # Strategy output (regeneratable cache)
        +-- children/
            +-- <child>.md        # Per-child authoritative context (durable)

The split exists so that regenerating the parent cache (re-running resume
against the same parent) never disturbs an existing child file. Once
``children/<child>.md`` exists, it is the authoritative context that gets
appended to the child session's system prompt.

Legacy note: pre-0.2.0, this was a single flat
``<forge_root>/.forge/prev_sessions/<parent>.md`` (parent-scoped, overwritten
by every resume/fork). New code never reads or writes the flat layout. GC
treats any remaining flat ``*.md`` files at the top level of
``prev_sessions/`` as orphans (see ``iter_legacy_flat_files``).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

PREV_SESSIONS_DIR = "prev_sessions"
GENERATED_FILENAME = "generated.md"
CHILDREN_DIR = "children"


def prev_sessions_root(forge_root: Path) -> Path:
    return forge_root / ".forge" / PREV_SESSIONS_DIR


def parent_dir(forge_root: Path, parent_name: str) -> Path:
    return prev_sessions_root(forge_root) / parent_name


def generated_path(forge_root: Path, parent_name: str) -> Path:
    return parent_dir(forge_root, parent_name) / GENERATED_FILENAME


def children_dir(forge_root: Path, parent_name: str) -> Path:
    return parent_dir(forge_root, parent_name) / CHILDREN_DIR


def child_path(forge_root: Path, parent_name: str, child_name: str) -> Path:
    return children_dir(forge_root, parent_name) / f"{child_name}.md"


def generated_path_rel(parent_name: str) -> str:
    """Return the forge-root-relative path to ``generated.md``."""
    return f".forge/{PREV_SESSIONS_DIR}/{parent_name}/{GENERATED_FILENAME}"


def child_path_rel(parent_name: str, child_name: str) -> str:
    """Return the forge-root-relative path to ``children/<child>.md``."""
    return f".forge/{PREV_SESSIONS_DIR}/{parent_name}/{CHILDREN_DIR}/{child_name}.md"


def ensure_child(forge_root: Path, parent_name: str, child_name: str) -> Path:
    """Create ``children/<child>.md`` as a copy of ``generated.md`` if absent.

    Idempotent: if the child file already exists (user has curated it, or it
    was created by a previous resume), leave it alone. This is the durability
    guarantee: once a child file exists, regenerating the parent cache does
    not affect it.

    Raises ``FileNotFoundError`` if neither the child file nor the parent
    cache exists -- the caller is responsible for running ``process_handoff``
    first.
    """
    target = child_path(forge_root, parent_name, child_name)
    if target.exists():
        return target

    source = generated_path(forge_root, parent_name)
    if not source.is_file():
        raise FileNotFoundError(
            f"Cannot copy parent cache to child: {source} does not exist. " "Run process_handoff() first."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def iter_parents(forge_root: Path) -> Iterator[Path]:
    """Yield each ``<parent>/`` directory under ``prev_sessions/``.

    Skips legacy flat ``.md`` files at the top level (see
    ``iter_legacy_flat_files``).
    """
    root = prev_sessions_root(forge_root)
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir():
            yield entry


def iter_children(forge_root: Path, parent_name: str) -> Iterator[Path]:
    """Yield each ``<child>.md`` under ``<parent>/children/``."""
    target = children_dir(forge_root, parent_name)
    if not target.is_dir():
        return
    for entry in target.iterdir():
        if entry.is_file() and entry.suffix == ".md":
            yield entry


def iter_legacy_flat_files(forge_root: Path) -> Iterator[Path]:
    """Yield top-level ``<parent>.md`` files (legacy pre-0.2.0 layout).

    These are orphan candidates for GC; new code never writes here.
    """
    root = prev_sessions_root(forge_root)
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_file() and entry.suffix == ".md":
            yield entry
