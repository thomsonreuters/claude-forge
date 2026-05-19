"""Garbage collection operations (command-core).

Detects and removes orphaned Forge state:
- Session directories not in the global index
- Handoff files for sessions not in the index
- Stale active-session entries (dead PIDs)
- Stale work-queue markers (session gone or worktree gone)
- Stale proxy entries (dead PIDs, orphaned "starting" state)
- Orphaned search documents (transcript files deleted)

All detect functions are read-only (no mutations). The run_clean()
function is the only mutator.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .context import ExecutionContext

_log = logging.getLogger(__name__)

VALID_SCOPES = {"repo", "project", "all"}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrphanCategory:
    """A single category of detected orphans."""

    category: str
    description: str
    count: int
    items: list[str]


@dataclass(frozen=True)
class CleanReport:
    """Aggregated orphan detection report (read-only)."""

    categories: list[OrphanCategory]
    scope: str

    @property
    def total_count(self) -> int:
        return sum(c.count for c in self.categories)

    @property
    def is_clean(self) -> bool:
        return self.total_count == 0


@dataclass
class CleanResult:
    """Result of an actual cleanup run."""

    categories_cleaned: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def deleted_count(self) -> int:
        return sum(self.categories_cleaned.values())


class CleanError(RuntimeError):
    """Raised when forge clean cannot proceed."""


# ---------------------------------------------------------------------------
# Forge-root discovery
# ---------------------------------------------------------------------------


def _resolve_tracked_roots(ctx: ExecutionContext, scope: str) -> set[Path]:
    """Build the set of forge_roots to scan from tracked sources.

    Sources (no filesystem crawl):
    1. ctx.forge_root (current project)
    2. Session index entries (filtered by scope)
    3. Installed manifest entries (project_path)

    Raises CleanError for --scope project when no forge_root.
    """
    if scope == "project":
        if ctx.forge_root is None:
            raise CleanError("Not inside a Forge project. Run from a directory with .forge/ or use --scope repo.")
        return {ctx.forge_root}

    from forge.session import SessionManager

    manager = SessionManager()

    if scope == "repo":
        entries = manager.list_sessions(
            include_incognito=True,
            project_root_filter=str(ctx.project_root),
        )
    else:  # "all"
        entries = manager.list_sessions(include_incognito=True)

    roots: set[Path] = set()
    for _name, entry in entries:
        fr = entry.forge_root or entry.worktree_path
        if fr:
            roots.add(Path(fr))

    # Add current forge_root
    if ctx.forge_root is not None:
        roots.add(ctx.forge_root)

    # Add installed-manifest roots. For repo scope, match by project_root
    # from the index entries rather than path containment, because git
    # worktrees are typically siblings of the main checkout, not children.
    # `roots` at this point contains index-derived roots (already filtered
    # by project_root for repo scope).
    index_roots = set(roots)
    try:
        from forge.install.tracking import TrackingStore

        manifest = TrackingStore().read()
        for _key, installation in manifest.installations.items():
            pp = installation.project_path
            if pp is None:
                continue
            p = Path(pp)
            if scope == "repo" and not _belongs_to_project(p, ctx.project_root, index_roots):
                continue
            if p.is_dir() and (p / ".forge").is_dir():
                roots.add(p)
    except Exception:
        _log.debug("Could not read installed manifest for root discovery", exc_info=True)

    return roots


def _belongs_to_project(candidate: Path, project_root: Path, known_roots: set[Path]) -> bool:
    """Check if candidate belongs to the same logical project.

    Handles sibling worktrees (common git layout) that live beside the
    main checkout rather than under it. Two checks:
    1. Path containment (regular subdirectories)
    2. Already in the known roots set (discovered via session index,
       which records project_root per entry)
    """
    resolved = candidate.resolve()
    # Direct containment (subdirectory or equal)
    try:
        resolved.relative_to(project_root.resolve())
        return True
    except ValueError:
        pass
    # Already discovered via index (which filters by project_root)
    return resolved in known_roots


# ---------------------------------------------------------------------------
# Reference set
# ---------------------------------------------------------------------------


def _list_reference_entries(
    ctx: ExecutionContext,
    scope: str,
) -> list[tuple[str, str, str | None]]:
    """Return scoped session reference tuples from the index.

    Each tuple contains ``(name, forge_root, worktree_path)`` for categories
    that need different identity axes.
    """
    from forge.session import SessionManager

    manager = SessionManager()

    if scope == "project" and ctx.forge_root is not None:
        entries = manager.list_sessions(
            include_incognito=True,
            forge_root_filter=str(ctx.forge_root),
        )
    elif scope == "repo":
        entries = manager.list_sessions(
            include_incognito=True,
            project_root_filter=str(ctx.project_root),
        )
    else:  # "all"
        entries = manager.list_sessions(include_incognito=True)

    return [(name, entry.forge_root or entry.worktree_path, entry.worktree_path) for name, entry in entries]


def _build_reference_set(ctx: ExecutionContext, scope: str, scope_roots: set[Path]) -> set[tuple[str, str]]:
    """Build the set of (session_name, forge_root) tuples from the index.

    Uses list_sessions() which triggers self-healing. The returned set
    is filtered to only include sessions whose forge_root is in scope_roots.
    """
    result: set[tuple[str, str]] = set()
    for name, forge_root, _worktree_path in _list_reference_entries(ctx, scope):
        if forge_root and Path(forge_root) in scope_roots:
            result.add((name, forge_root))
    return result


def _build_worktree_reference_set(ctx: ExecutionContext, scope: str, scope_roots: set[Path]) -> set[tuple[str, str]]:
    """Build the set of (session_name, worktree_path) tuples for queue markers."""
    result: set[tuple[str, str]] = set()
    for name, _forge_root, worktree_path in _list_reference_entries(ctx, scope):
        if worktree_path and _path_in_roots(Path(worktree_path), scope_roots):
            result.add((name, worktree_path))
    return result


def _build_handoff_context_reference_set(ref_set: set[tuple[str, str]]) -> set[str]:
    """Build absolute paths referenced by session derivation context_file fields."""
    from forge.session import SessionStore

    result: set[str] = set()
    for name, forge_root in ref_set:
        try:
            state = SessionStore(forge_root, name).read()
        except Exception:
            _log.debug("Could not read session manifest for handoff GC: %s (%s)", name, forge_root, exc_info=True)
            continue

        derivation = state.confirmed.derivation
        if derivation is None or not derivation.context_file:
            continue

        context_path = Path(derivation.context_file).expanduser()
        if not context_path.is_absolute():
            context_path = Path(forge_root) / context_path
        result.add(str(context_path.resolve()))

    return result


# ---------------------------------------------------------------------------
# Pure detect functions (read-only)
# ---------------------------------------------------------------------------


def _detect_orphan_session_dirs(ref_set: set[tuple[str, str]], forge_roots: set[Path]) -> OrphanCategory:
    """Find session directories not in the index for their forge_root."""
    orphans: list[str] = []
    for forge_root in forge_roots:
        sessions_dir = forge_root / ".forge" / "sessions"
        if not sessions_dir.is_dir():
            continue
        for child in sessions_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if (name, str(forge_root)) not in ref_set:
                # Verify it has content (not just an empty dir)
                manifest = child / "forge.session.json"
                if manifest.is_file() or any(child.iterdir()):
                    orphans.append(str(child))
    return OrphanCategory(
        category="session_dirs",
        description="Session directories not in the index",
        count=len(orphans),
        items=sorted(orphans),
    )


def _detect_orphan_handoff_files(ref_set: set[tuple[str, str]], forge_roots: set[Path]) -> OrphanCategory:
    """Find orphaned resume-context artifacts under ``prev_sessions/``.

    Walks the per-parent layout (``<parent>/generated.md`` +
    ``<parent>/children/<child>.md``) and identifies three kinds of orphans:

    1. ``<parent>/`` directories whose parent session is not in the index --
       the whole directory is orphaned (rmtree).
    2. ``children/<child>.md`` files not referenced by any child session's
       ``Derivation.context_file`` (within a still-referenced parent dir) --
       just the file.
    3. Top-level ``<parent>.md`` files (legacy pre-0.2.0 flat layout) --
       always orphaned since new code never writes here.

    Parent liveness checks (session_name, forge_root) against the ref_set to
    handle name reuse across different Forge projects correctly. Child files
    are kept only when an indexed session derivation references that exact path.
    """
    from forge.session import prev_sessions as _ps

    orphans: list[str] = []
    referenced_context_files = _build_handoff_context_reference_set(ref_set)

    for forge_root in forge_roots:
        prev_root = _ps.prev_sessions_root(forge_root)
        if not prev_root.is_dir():
            continue
        names_in_root = {name for name, fr in ref_set if fr == str(forge_root)}

        # 1 + 2: per-parent directories and their child files
        for parent_dir_path in _ps.iter_parents(forge_root):
            parent_name = parent_dir_path.name
            child_files = list(_ps.iter_children(forge_root, parent_name))
            referenced_children = [
                child_file for child_file in child_files if str(child_file.resolve()) in referenced_context_files
            ]

            if parent_name not in names_in_root and not referenced_children:
                orphans.append(str(parent_dir_path))
                continue

            # Parent dir is live either because the parent session lives in
            # this Forge root, or because a cross-worktree child references a
            # child context file under it. Remove only unreferenced children.
            for child_file in child_files:
                if str(child_file.resolve()) not in referenced_context_files:
                    orphans.append(str(child_file))

        # 3: legacy flat files at the top of prev_sessions/
        for legacy_file in _ps.iter_legacy_flat_files(forge_root):
            orphans.append(str(legacy_file))

    return OrphanCategory(
        category="handoff_files",
        description="Orphaned resume-context artifacts (handoff files)",
        count=len(orphans),
        items=sorted(orphans),
    )


def _detect_stale_active_entries(scope_roots: set[Path]) -> OrphanCategory:
    """Find active-session entries with dead PIDs, scoped by worktree_path.

    Read-only: reads the active index and checks liveness without mutating.
    """
    from forge.session.active import ActiveSessionStore

    store = ActiveSessionStore()
    try:
        index = store.read()
    except Exception:
        _log.debug("Could not read active session index", exc_info=True)
        return OrphanCategory(
            category="active_entries",
            description="Stale active-session entries (dead PIDs)",
            count=0,
            items=[],
        )

    from forge.session.identity import session_name_from_key

    stale: list[str] = []
    for key, entry in index.sessions.items():
        entry_path = Path(entry.worktree_path)
        if not _path_in_roots(entry_path, scope_roots):
            continue
        if not store._entry_is_live(entry):
            # Encode display_name::forge_root so the clean phase can
            # pass forge_root to clear_session for exact scoped deletion.
            display_name = session_name_from_key(key)
            forge_root = entry.forge_root or entry.worktree_path
            stale.append(f"{display_name}::{forge_root}")

    return OrphanCategory(
        category="active_entries",
        description="Stale active-session entries (dead PIDs)",
        count=len(stale),
        items=sorted(stale),
    )


def _detect_stale_work_queue(ref_set: set[tuple[str, str]], scope_roots: set[Path]) -> OrphanCategory:
    """Find pending work-queue markers for sessions not in the index.

    Scoped by worktree_path in the marker payload. Checks
    (session_name, worktree_path) against the ref_set to avoid
    cross-root name masking.
    Read-only: reads marker files without mutation.
    """
    from forge.core.paths import get_forge_home

    queue_dir = get_forge_home() / "pending-work"
    if not queue_dir.is_dir():
        return OrphanCategory(
            category="work_queue",
            description="Stale work-queue markers",
            count=0,
            items=[],
        )

    stale: list[str] = []

    for marker_file in queue_dir.iterdir():
        if not marker_file.is_file() or marker_file.suffix != ".json":
            continue
        try:
            data = json.loads(marker_file.read_text(encoding="utf-8"))
            payload = data.get("payload", {})
            wt_path = payload.get("worktree_path", "")
            session_name = payload.get("session_name", "")

            # Scope filter: skip markers outside scope roots
            if wt_path and not _path_in_roots(Path(wt_path), scope_roots):
                continue

            # Orphan check: session (name, worktree_path) not in ref_set
            if session_name and (session_name, wt_path) not in ref_set:
                stale.append(str(marker_file))
        except (json.JSONDecodeError, OSError):
            _log.debug("Could not read work-queue marker %s", marker_file, exc_info=True)
            continue

    return OrphanCategory(
        category="work_queue",
        description="Stale work-queue markers",
        count=len(stale),
        items=sorted(stale),
    )


def _detect_stale_proxies() -> OrphanCategory:
    """Find proxy entries with dead PIDs or orphaned starting state.

    Read-only: reads the proxy registry without mutation.
    Global scope (proxies have no project affinity).
    """
    from forge.core.process import is_pid_alive
    from forge.proxy.proxies import ProxyRegistryStore, _is_orphaned_starting

    try:
        store = ProxyRegistryStore()
        registry = store.read()
    except Exception:
        _log.debug("Could not read proxy registry", exc_info=True)
        return OrphanCategory(
            category="proxies",
            description="Stale proxy entries (dead PIDs)",
            count=0,
            items=[],
        )

    stale: list[str] = []
    for proxy_id, entry in registry.proxies.items():
        if entry.pid is not None:
            if not is_pid_alive(entry.pid):
                stale.append(proxy_id)
        elif entry.status == "starting" and _is_orphaned_starting(entry):
            stale.append(proxy_id)

    return OrphanCategory(
        category="proxies",
        description="Stale proxy entries (dead PIDs)",
        count=len(stale),
        items=sorted(stale),
    )


def _detect_orphan_search_docs(forge_roots: set[Path]) -> OrphanCategory:
    """Find search-index documents whose transcript files no longer exist.

    Read-only: reads the document store without calling prune_missing().
    """
    orphans: list[str] = []

    for forge_root in forge_roots:
        try:
            from forge.search.store import SearchDocumentStore

            doc_store = SearchDocumentStore(forge_root=forge_root)
            docs = doc_store.read()
            for doc in docs:
                if not Path(doc.transcript_path).is_file():
                    orphans.append(doc.transcript_path)
        except Exception:
            _log.debug("Could not read search store for %s", forge_root, exc_info=True)
            continue

    return OrphanCategory(
        category="search_docs",
        description="Orphaned search documents (transcript deleted)",
        count=len(orphans),
        items=sorted(orphans),
    )


def _detect_dead_installations() -> OrphanCategory:
    """Find installed-manifest entries whose project_path no longer exists.

    Always global (like proxies): installed.json is global state in
    ~/.forge/. A dead path is dead regardless of which repo you're in,
    and dead paths can't be scoped by containment (they don't exist).
    """
    try:
        from forge.install.models import parse_installation_key
        from forge.install.tracking import TrackingStore

        manifest = TrackingStore().read()
    except Exception:
        _log.debug("Could not read installed manifest", exc_info=True)
        return OrphanCategory(
            category="dead_installations",
            description="Installed-manifest entries for missing paths",
            count=0,
            items=[],
        )

    dead: list[str] = []
    for key, installation in manifest.installations.items():
        pp = installation.project_path
        if pp is None:
            continue
        if not Path(pp).is_dir():
            inst_scope, _ = parse_installation_key(key)
            dead.append(f"{inst_scope}:{pp}")

    return OrphanCategory(
        category="dead_installations",
        description="Installed-manifest entries for missing paths",
        count=len(dead),
        items=sorted(dead),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_in_roots(candidate: Path, roots: set[Path]) -> bool:
    """Check if candidate path is under (or equal to) any root in the set.

    Returns False for an empty root set — prevents repo-scope from
    silently widening to global scope when no tracked roots exist.
    """
    if not roots:
        return False
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Report (read-only)
# ---------------------------------------------------------------------------


def collect_clean_report(*, ctx: ExecutionContext, scope: str = "repo") -> CleanReport:
    """Scan for orphaned objects and return a report.

    Pure detection: no mutations. Safe for dry-run.

    Raises:
        CleanError: If scope=project and no forge_root.
    """
    if scope not in VALID_SCOPES:
        raise CleanError(f"Invalid scope: {scope!r}. Must be one of {VALID_SCOPES}")

    scope_roots = _resolve_tracked_roots(ctx, scope)

    ref_set = _build_reference_set(ctx, scope, scope_roots)
    worktree_ref_set = _build_worktree_reference_set(ctx, scope, scope_roots)

    categories = [
        _detect_orphan_session_dirs(ref_set, scope_roots),
        _detect_orphan_handoff_files(ref_set, scope_roots),
        _detect_stale_active_entries(scope_roots),
        _detect_stale_work_queue(worktree_ref_set, scope_roots),
        _detect_stale_proxies(),
        _detect_orphan_search_docs(scope_roots),
        _detect_dead_installations(),
    ]

    return CleanReport(categories=categories, scope=scope)


# ---------------------------------------------------------------------------
# Cleanup (mutating)
# ---------------------------------------------------------------------------


def run_clean(*, ctx: ExecutionContext, scope: str = "repo") -> CleanResult:
    """Detect orphaned objects and delete them.

    Calls collect_clean_report() first, then performs deletions.

    Raises:
        CleanError: If scope=project and no forge_root.
    """
    report = collect_clean_report(ctx=ctx, scope=scope)
    result = CleanResult()

    for category in report.categories:
        if category.count == 0:
            continue

        cleaned = 0
        if category.category == "session_dirs":
            cleaned = _clean_session_dirs(category.items, result)
        elif category.category == "handoff_files":
            cleaned = _clean_handoff_files(category.items, result)
        elif category.category == "active_entries":
            cleaned = _clean_active_entries(category.items)
        elif category.category == "work_queue":
            cleaned = _clean_files(category.items, result)
        elif category.category == "proxies":
            cleaned = _clean_proxies()
        elif category.category == "search_docs":
            cleaned = _clean_search_docs(report, result)
        elif category.category == "dead_installations":
            cleaned = _clean_dead_installations(category.items, result)

        if cleaned > 0:
            result.categories_cleaned[category.category] = cleaned

    return result


def _clean_session_dirs(items: list[str], result: CleanResult) -> int:
    """Remove orphaned session directories."""
    cleaned = 0
    for path_str in items:
        try:
            shutil.rmtree(path_str)
            cleaned += 1
        except OSError as e:
            result.failed.append((path_str, str(e)))
    return cleaned


def _clean_files(items: list[str], result: CleanResult) -> int:
    """Remove orphaned files (work-queue markers)."""
    cleaned = 0
    for path_str in items:
        try:
            Path(path_str).unlink()
            cleaned += 1
        except OSError as e:
            result.failed.append((path_str, str(e)))
    return cleaned


def _clean_handoff_files(items: list[str], result: CleanResult) -> int:
    """Remove orphaned resume-context artifacts.

    Items may be:
    - ``<parent>/`` directories (whole orphaned parents) -- rmtree
    - ``<parent>/children/<child>.md`` files -- unlink, then prune empty
      ``children/`` and parent dirs that contain only ``generated.md``
    - Top-level ``<parent>.md`` legacy flat files -- unlink
    """
    cleaned = 0
    dirs_to_check: set[Path] = set()

    for path_str in items:
        path = Path(path_str)
        try:
            if path.is_dir():
                shutil.rmtree(path)
                cleaned += 1
            elif path.is_file():
                # Track parent for empty-dir cleanup after unlinking
                if path.parent.name == "children":
                    dirs_to_check.add(path.parent)
                path.unlink()
                cleaned += 1
        except OSError as e:
            result.failed.append((path_str, str(e)))

    # Post-cleanup: drop empty children/ and parent dirs left behind.
    # If parent dir contains only generated.md (no children left), the cache
    # is dead weight too -- the whole parent dir goes.
    for children_dir in dirs_to_check:
        try:
            if not children_dir.is_dir() or any(children_dir.iterdir()):
                continue
            children_dir.rmdir()
            parent = children_dir.parent
            if not parent.is_dir():
                continue
            remaining = list(parent.iterdir())
            if not remaining or (len(remaining) == 1 and remaining[0].name == "generated.md"):
                shutil.rmtree(parent)
        except OSError:
            # Best-effort post-cleanup
            pass

    return cleaned


def _clean_active_entries(items: list[str]) -> int:
    """Clean only the specific stale active-session entries detected.

    Does NOT call list_sessions() which would self-heal the entire
    registry — that would clean entries outside the requested scope.
    """
    from forge.session.active import ActiveSessionStore

    store = ActiveSessionStore()
    cleaned = 0
    for item in items:
        # Items encoded as "display_name::forge_root" by detect phase
        if "::" in item:
            name, forge_root = item.split("::", 1)
        else:
            name, forge_root = item, None
        try:
            if store.clear_session(name, forge_root=forge_root):
                cleaned += 1
        except Exception:
            pass
    return cleaned


def _clean_proxies() -> int:
    """Clean stale proxy entries by delegating to existing prune function."""
    from forge.proxy.proxy_orchestrator import prune_stale_proxies

    result = prune_stale_proxies()
    return len(result.pruned_proxy_ids)


def _clean_dead_installations(items: list[str], result: CleanResult) -> int:
    """Remove installed-manifest entries whose project_path no longer exists."""
    from forge.install.tracking import TrackingStore

    store = TrackingStore()
    cleaned = 0
    for item in items:
        # Items are "scope:path" strings
        parts = item.split(":", 1)
        if len(parts) != 2:
            continue
        scope, project_path = parts
        try:
            if store.remove_installation(scope, project_path):
                cleaned += 1
        except Exception as e:
            result.failed.append((item, str(e)))
    return cleaned


def _clean_search_docs(report: CleanReport, result: CleanResult) -> int:
    """Clean orphaned search documents per forge_root."""
    from forge.search.bm25_store import BM25IndexStore
    from forge.search.content_store import ContentStore
    from forge.search.index_state import IndexStateStore
    from forge.search.store import SearchDocumentStore

    # Collect forge_roots from the scope_roots used to generate the report
    # We re-derive from the search_docs category items (transcript paths)
    search_cat = next((c for c in report.categories if c.category == "search_docs"), None)
    if search_cat is None or search_cat.count == 0:
        return 0

    # Group orphaned transcript paths by forge_root
    # We need to know which forge_root each transcript belongs to.
    # Re-scan the forge_roots and prune per-root.
    cleaned = 0
    scope_roots = _extract_forge_roots_from_report(report)

    for forge_root in scope_roots:
        try:
            doc_store = SearchDocumentStore(forge_root=forge_root)
            bm25_store = BM25IndexStore(forge_root=forge_root)
            content_store = ContentStore(forge_root=forge_root)
            index_store = IndexStateStore(forge_root=forge_root)

            removed_docs = doc_store.prune_missing()
            for path in removed_docs:
                bm25_store.remove_document(path)
                content_store.remove(path)
            removed_index = index_store.prune_missing()
            cleaned += len(removed_docs) + len(removed_index)
        except Exception as e:
            result.failed.append((str(forge_root), str(e)))

    return cleaned


def _extract_forge_roots_from_report(report: CleanReport) -> set[Path]:
    """Extract forge_roots that had search orphans from the report items."""
    search_cat = next((c for c in report.categories if c.category == "search_docs"), None)
    if search_cat is None:
        return set()

    roots: set[Path] = set()
    for transcript_path in search_cat.items:
        # Transcript paths are under <forge_root>/.forge/artifacts/
        p = Path(transcript_path)
        # Walk up to find .forge/
        for parent in p.parents:
            if parent.name == ".forge" and parent.parent.is_dir():
                roots.add(parent.parent)
                break
    return roots
