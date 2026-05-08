"""High-level session operations coordinating stores.

SessionManager provides the business logic for session lifecycle operations,
coordinating between SessionStore and IndexStore.

The CLI layer should be thin and delegate to this class for all operations.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from forge.core.naming import generate_unique_name
from forge.core.state import now_iso

from .artifacts import resolve_artifact_path
from .claude.paths import find_project_root
from .config import (
    DEFAULT_PROXY_BASE_URL,
    DEFAULT_PROXY_TEMPLATE,
    LAUNCH_MODE_HOST,
    LAUNCH_MODE_SIDECAR,
)
from .exceptions import (
    CannotForkIncognitoError,
    ContextBudgetExceededError,
    DirtyWorktreeError,
    ForgeSessionError,
    ManifestCorruptedError,
    ManifestValidationError,
    SessionExistsError,
    SessionNotFoundError,
)
from .handoff import (
    HandoffResult,
    ResumeStrategy,
    estimate_transcript_tokens,
    process_handoff,
)
from .index import IndexStore
from .models import (
    Derivation,
    LaunchIntent,
    SessionIndexEntry,
    SessionState,
    SidecarLaunchIntent,
    create_session_state,
)
from .store import SessionStore

logger = logging.getLogger(__name__)


def _inherited_launch_intent(parent_state: SessionState) -> LaunchIntent | None:
    """Return the launch intent a derived session should inherit."""
    if parent_state.intent.launch is not None:
        return deepcopy(parent_state.intent.launch)

    if parent_state.confirmed.is_sandboxed:
        return LaunchIntent(
            mode=LAUNCH_MODE_SIDECAR,
            sidecar=SidecarLaunchIntent(),
        )

    return None


def _tracked_transcript_session_ids(state: SessionState) -> list[str]:
    """Return distinct Claude session IDs referenced by transcript artifacts."""
    transcripts = state.confirmed.artifacts.get("transcripts")
    if not isinstance(transcripts, list):
        return []

    session_ids: list[str] = []
    for artifact in transcripts:
        if not isinstance(artifact, dict):
            continue
        session_id = artifact.get("session_id")
        if isinstance(session_id, str) and session_id and session_id not in session_ids:
            session_ids.append(session_id)
    return session_ids


def _latest_transcript_artifact_path(state: SessionState) -> str | None:
    """Return the latest copied transcript artifact path from confirmed state."""
    transcripts = state.confirmed.artifacts.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        return None
    latest = transcripts[-1]
    if not isinstance(latest, dict):
        return None
    copied_path = latest.get("copied_path")
    return copied_path if isinstance(copied_path, str) else None


class SessionManager:
    """High-level session operations coordinating stores.

    This class provides the business logic layer between CLI commands
    and the underlying storage components.

    Attributes:
        index_store: Global session index manager.
    """

    def __init__(
        self,
        index_store: IndexStore | None = None,
    ) -> None:
        """Initialize the session manager.

        Args:
            index_store: Custom IndexStore instance. Creates default if None.
        """
        self.index_store = index_store or IndexStore()

    # -------------------------------------------------------------------------
    # Query Operations
    # -------------------------------------------------------------------------

    def list_sessions(
        self,
        include_incognito: bool = True,
        *,
        project_root_filter: str | None = None,
        forge_root_filter: str | None = None,
    ) -> list[tuple[str, SessionIndexEntry]]:
        """List sessions from the index, optionally filtered by scope.

        Args:
            include_incognito: Whether to include incognito sessions.
            project_root_filter: If set, only return entries matching this project_root.
            forge_root_filter: If set, only return entries matching this forge_root.

        Returns:
            List of (name, entry) tuples sorted by recency.
        """
        return self.index_store.list_sessions(
            include_incognito=include_incognito,
            project_root_filter=project_root_filter,
            forge_root_filter=forge_root_filter,
        )

    def get_session(self, name: str, forge_root: str | None = None) -> SessionState:
        """Get a session state by name, optionally scoped to a forge_root.

        Args:
            name: Session display name.
            forge_root: Scope to this project. Strict resolution when None.
        """
        entry = self.index_store.get_session(name, forge_root=forge_root)
        store = SessionStore(entry.forge_root or entry.worktree_path, name)

        if not store.exists():
            raise SessionNotFoundError(name)

        return store.read()

    def switch_session(self, name: str, forge_root: str | None = None) -> SessionState:
        """Load a session and update its last_accessed_at timestamp.

        Args:
            name: Session display name.
            forge_root: Scope to this project. Strict resolution when None.
        """
        entry = self.index_store.get_session(name, forge_root=forge_root)

        store = SessionStore(entry.forge_root or entry.worktree_path, name)
        if not store.exists():
            raise SessionNotFoundError(name)

        state = store.read()

        timestamp = now_iso()

        store.update(timeout_s=5.0, mutate=lambda m: setattr(m, "last_accessed_at", timestamp))

        entry_forge_root = entry.forge_root or entry.worktree_path
        self.index_store.update_session(name, last_accessed_at=timestamp, forge_root=entry_forge_root)

        return state

    def session_exists(self, name: str, forge_root: str | None = None) -> bool:
        """Check if a session exists, optionally scoped to a forge_root.

        Args:
            name: Session display name.
            forge_root: Scope to this project. Strict resolution when None.
        """
        return self.index_store.session_exists(name, forge_root=forge_root)

    def get_session_entry(self, name: str, forge_root: str | None = None) -> SessionIndexEntry:
        """Get a session index entry by name, optionally scoped.

        Args:
            name: Session display name.
            forge_root: Scope to this project. Strict resolution when None.
        """
        return self.index_store.get_session(name, forge_root=forge_root)

    def get_session_store(self, name: str, forge_root: str | None = None) -> SessionStore:
        """Get a SessionStore for a session by name.

        Args:
            name: Session name to look up.

        Returns:
            SessionStore instance for the session's worktree.

        Raises:
            SessionNotFoundError: If session doesn't exist.
            InvalidSessionNameError: If name is invalid.
        """
        entry = self.index_store.get_session(name, forge_root=forge_root)
        return SessionStore(entry.forge_root or entry.worktree_path, name)

    def resolve_project_root(self, worktree_path: str | Path) -> str:
        """Resolve the project root for a worktree path.

        For regular checkouts, this is the same as worktree_path.
        For git worktrees, this finds the main repository.

        Args:
            worktree_path: Path to the worktree.

        Returns:
            Absolute path to the project root.
        """
        from .worktree import get_main_repo_root

        try:
            return str(get_main_repo_root(Path(worktree_path)))
        except (ForgeSessionError, OSError):
            # GitNotFoundError (no git), GitWorktreeError (not a repo), OSError (fs)
            return str(Path(worktree_path).resolve())

    # -------------------------------------------------------------------------
    # Lifecycle Operations
    # -------------------------------------------------------------------------

    def start_session(
        self,
        name: str,
        *,
        worktree_path: str | None = None,
        create_worktree: bool = False,
        branch: str | None = None,
        proxy_template: str | None = None,
        proxy_base_url: str | None = None,
        direct: bool = False,
        is_incognito: bool = False,
        launch_mode: str = LAUNCH_MODE_HOST,
        sidecar_mounts: list[str] | None = None,
        sidecar_image: str | None = None,
        direct_model: str | None = None,
        claude_session_id: str | None = None,
    ) -> SessionState:
        """Create and register a new session.

        Creates the session state, updates the index, and sets the
        active session pointer. Does NOT invoke Claude - the CLI should
        call invoke_claude separately.

        Args:
            name: Human-friendly session name.
            worktree_path: Path to worktree (defaults to cwd).
            create_worktree: If True, create a new git worktree.
            branch: Git branch name (defaults to session name if create_worktree).
            proxy_template: Proxy template (defaults to config default when not direct).
            proxy_base_url: Proxy base URL (defaults to config default when not direct).
            direct: If True, create a direct Anthropic session with no proxy intent.
            is_incognito: Whether session auto-deletes on exit.
            launch_mode: How Forge should relaunch this session later.
            sidecar_mounts: Raw sidecar mount specs to persist for relaunch.
            sidecar_image: Optional sidecar image override to persist for relaunch.
            direct_model: Optional Claude Code env-ready direct model pin.

        Returns:
            The created session state with candidate UUID.

        Raises:
            SessionExistsError: If session name already exists.
            InvalidSessionNameError: If name is invalid.
            FileNotFoundError: If no git repository found.
            BranchExistsError: If branch already exists (when create_worktree=True).
            WorktreePathExistsError: If worktree path exists (when create_worktree=True).
            InvalidBranchNameError: If explicit branch name is invalid.
        """
        # Compute forge_root early for scoped collision check.
        # For worktree sessions, use launch CWD (before worktree creation).
        # For non-worktree sessions, use explicit worktree_path if provided.
        from forge.core.ops.context import find_forge_root

        launch_cwd = Path.cwd().resolve()
        _early_search = Path(worktree_path).resolve() if worktree_path and not create_worktree else launch_cwd
        _early_forge_root = find_forge_root(_early_search)
        _early_fr_str = str(_early_forge_root) if _early_forge_root else None

        if self.index_store.session_exists(name, forge_root=_early_fr_str):
            raise SessionExistsError(name)

        created_worktree = False
        worktree_branch: str | None = branch
        main_repo_root: Path | None = None

        def _rollback_worktree(*, resolved_worktree_path: str | None) -> None:
            if not created_worktree or resolved_worktree_path is None:
                return

            try:
                from .worktree import cleanup_worktree

                cleanup_worktree(
                    worktree_path=Path(resolved_worktree_path),
                    branch=worktree_branch,
                    delete_branch_flag=True,
                    force=True,
                    repo_root=main_repo_root,
                )
            except Exception as e:
                logger.debug("Worktree rollback cleanup failed (non-critical): %s", e)

        if create_worktree:
            from .worktree import copy_runtime_config
            from .worktree import create_worktree as git_create_worktree
            from .worktree import get_main_repo_root

            main_repo_root = get_main_repo_root()

            try:
                # Create worktree first (external side effect).
                wt_result = git_create_worktree(
                    session_name=name,
                    branch=branch,
                    cwd=main_repo_root,
                )
                created_worktree = True
                worktree_path = wt_result.worktree_path
                worktree_branch = wt_result.branch

                # Copy runtime config (best-effort; does not raise).
                copy_runtime_config(main_repo_root, Path(worktree_path))

            except Exception:
                # No Forge state has been written yet. Best-effort cleanup of any
                # created worktree/branch.
                _rollback_worktree(resolved_worktree_path=worktree_path)
                raise

        if worktree_path is None:
            worktree_path = str(Path.cwd().resolve())
        else:
            worktree_path = str(Path(worktree_path).resolve())

        # Rule 1: sessions require `forge extension enable` (.forge/ must exist).
        # For worktree sessions, use the launch CWD (captured before worktree
        # creation) — the user's nested project dir, not the bare checkout.
        from forge.core.ops.context import find_forge_root

        forge_root_search = launch_cwd if created_worktree else Path(worktree_path)
        resolved_forge_root = find_forge_root(forge_root_search)
        if resolved_forge_root is None:
            if created_worktree:
                _rollback_worktree(resolved_worktree_path=worktree_path)
            from .exceptions import ForgeNotEnabledError

            raise ForgeNotEnabledError(str(forge_root_search))

        # For worktree sessions with nested Forge projects, remap forge_root
        # into the new worktree. Root-level projects (forge_root == repo root)
        # keep the original forge_root so manifests stay under the main .forge/.
        if created_worktree and main_repo_root is not None:
            try:
                relative = resolved_forge_root.relative_to(main_repo_root)
            except ValueError:
                relative = Path(".")
            if str(relative) != ".":
                # Nested project: remap to equivalent position in new worktree
                forge_root_str = str(Path(worktree_path) / relative)
            else:
                # Root-level project: keep parent's forge_root
                forge_root_str = str(resolved_forge_root)
        else:
            forge_root_str = str(resolved_forge_root)

        # D5: Multiple sessions per worktree are allowed (per-session directories).
        # Only check that THIS session name doesn't already have a manifest.
        store = SessionStore(forge_root_str, name)
        if store.exists():
            if created_worktree:
                _rollback_worktree(resolved_worktree_path=worktree_path)
            raise SessionExistsError(name)

        # Find project root - use main repo root if we created a worktree
        if main_repo_root is not None:
            project_root: str | Path = main_repo_root
        else:
            # For non-worktree sessions, find the project root
            # (which is the same as worktree_path for regular checkouts)
            project_root = find_project_root(worktree_path)
        # checkout_root = git --show-toplevel (not CWD). For worktree-created sessions
        # main_repo_root is the logical repo, not the checkout; use get_repo_root() instead.
        from .worktree import get_repo_root

        try:
            checkout_root_str: str | None = str(get_repo_root(Path(worktree_path)))
        except Exception:
            checkout_root_str = worktree_path  # Fallback if not in a git repo

        # relative_path = forge_root relative to checkout_root
        relative_path_str: str | None = None
        if forge_root_str and checkout_root_str:
            try:
                relative_path_str = str(Path(forge_root_str).relative_to(checkout_root_str))
            except ValueError:
                logger.warning(
                    "forge_root %s is not relative to checkout_root %s; defaulting to '.'",
                    forge_root_str,
                    checkout_root_str,
                )
                relative_path_str = "."

        if direct:
            template = None
            base_url = None
        else:
            template = proxy_template or DEFAULT_PROXY_TEMPLATE
            base_url = proxy_base_url or DEFAULT_PROXY_BASE_URL

        # UUID pre-seeded if provided; SessionStart hook validates it
        state = create_session_state(
            name=name,
            proxy_template=template,
            proxy_base_url=base_url,
            is_incognito=is_incognito,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            launch_mode=launch_mode,
            sidecar_mounts=sidecar_mounts,
            sidecar_image=sidecar_image,
            direct_model=direct_model,
        )

        if claude_session_id:
            state.confirmed.claude_session_id = claude_session_id

        if create_worktree and state.worktree:
            state.worktree.is_worktree = True

        # Set forge_root on session state for downstream consumers
        state.forge_root = forge_root_str

        # Commit phase: write Forge state only after external worktree creation succeeded.
        store = SessionStore(forge_root_str, name)

        wrote_manifest = False
        added_to_index = False

        try:
            store.write(state)
            wrote_manifest = True

            self.index_store.add_from_state(
                state,
                str(project_root),
                checkout_root=checkout_root_str,
                forge_root=forge_root_str,
                relative_path=relative_path_str,
            )
            added_to_index = True

            return state

        except Exception:
            # Best-effort rollback for partial state.
            try:
                if added_to_index:
                    self.index_store.remove_session(name)
            except Exception as rollback_err:
                logger.warning("Rollback failed (index entry): %s", rollback_err)

            try:
                if wrote_manifest and store.exists():
                    store.delete()
            except Exception as rollback_err:
                logger.warning("Rollback failed (manifest delete): %s", rollback_err)

            # If we created a worktree, remove it (and branch) best-effort.
            _rollback_worktree(resolved_worktree_path=worktree_path)

            raise

    def resume_session(
        self,
        parent_name: str,
        *,
        child_name: str | None = None,
        strategy: str = "structured",
        depth: int = 1,
        context_limit: int | None = None,
        token_estimate_multiplier: float = 1.0,
        resume_mode: str = "handoff",
        forge_root: str | None = None,
    ) -> tuple[SessionState, HandoffResult]:
        """Create a new session derived from a parent with context assembly.

        Creates a new child session in the parent's worktree with context assembled
        from the parent's history. This is used when context approaches limits and
        the user wants to continue work with a fresh context window.

        When ``resume_mode="native"``, context assembly is skipped entirely. The
        caller is expected to launch Claude with ``--resume --fork-session`` to
        carry full conversation history natively. No system_prompt_file is generated.

        Does NOT invoke Claude - the CLI should call invoke_claude separately.

        Args:
            parent_name: Parent session name to derive from.
            child_name: Name for the child session (auto-generated if None).
            strategy: Context assembly strategy (minimal/structured/full).
            depth: How many ancestors to traverse (1 = parent only).
            context_limit: Context limit for budget check (required for full strategy).
            token_estimate_multiplier: Optional model-specific multiplier for heuristic budget checks.
            resume_mode: "handoff" (assemble context file) or "native" (skip assembly).

        Returns:
            Tuple of (child session state, handoff result).

        Raises:
            SessionNotFoundError: If parent session doesn't exist.
            SessionExistsError: If child_name already exists.
            InvalidSessionNameError: If name is invalid.
            ContextBudgetExceededError: If full strategy exceeds context limit.
        """
        if resume_mode not in {"handoff", "native"}:
            raise ValueError(f"Unsupported resume_mode: {resume_mode}")

        parent_entry = self.index_store.get_session(parent_name, forge_root=forge_root)
        parent_forge_root = parent_entry.forge_root or parent_entry.worktree_path
        parent_store = SessionStore(parent_forge_root, parent_name)
        if not parent_store.exists():
            raise SessionNotFoundError(parent_name)

        parent_state = parent_store.read()

        name_was_auto = child_name is None
        if name_was_auto:
            child_name = self._generate_resume_name(parent_name, forge_root=parent_forge_root)

        assert child_name is not None  # narrowing: either provided or generated

        if self.index_store.session_exists(child_name, forge_root=parent_forge_root):
            raise SessionExistsError(child_name)

        project_root = Path(self.resolve_project_root(parent_entry.worktree_path))
        parent_artifact_root = Path(parent_entry.forge_root or parent_entry.worktree_path)

        inherited_proxy = None
        if parent_state.confirmed.started_with_proxy:
            inherited_proxy = parent_state.confirmed.started_with_proxy.template

        timestamp = now_iso()

        parent_proxy_template = parent_state.intent.proxy.template if parent_state.intent.proxy else None
        parent_proxy_base_url = parent_state.intent.proxy.base_url if parent_state.intent.proxy else None

        # --- Native resume guard: when fork --into targets different
        # forge_roots, reject native resume here. Claude Code's --resume only works
        # within the same CWD's .claude/ project. For now, child always inherits
        # parent's forge_root, so this is a no-op.

        # --- Native mode: skip handoff, return early ---
        if resume_mode == "native":
            child_state = self._create_resume_child(
                child_name=child_name,
                parent_name=parent_name,
                parent_state=parent_state,
                parent_entry=parent_entry,
                inherited_proxy=inherited_proxy,
                parent_proxy_template=parent_proxy_template,
                parent_proxy_base_url=parent_proxy_base_url,
            )
            # Resolve parent transcript path for traceability (best-effort)
            transcript_artifact_path: str | None = None
            transcripts = parent_state.confirmed.artifacts.get("transcripts", [])
            if transcripts and isinstance(transcripts, list) and len(transcripts) > 0:
                latest = transcripts[-1]
                if isinstance(latest, dict):
                    transcript_artifact_path = latest.get("copied_path")

            child_state.confirmed.derivation = Derivation(
                parent_session=parent_name,
                parent_transcript=transcript_artifact_path,
                inherited_proxy=inherited_proxy,
                resume_mode="native",
                strategy=None,
                depth=1,
                resumed_at=timestamp,
                lineage=[parent_name],
                context_file=None,
                parent_forge_root=parent_entry.forge_root or parent_entry.worktree_path,
                parent_project_root=parent_entry.project_root,
            )

            handoff_result = HandoffResult(
                context_file=None,
                context_file_rel=None,
                transcript_artifact_path=transcript_artifact_path,
                token_estimate=None,
                lineage=[parent_name],
            )

            self._persist_resume_child(
                child_state=child_state,
                child_name=child_name,
                parent_name=parent_name,
                parent_entry=parent_entry,
                project_root=project_root,
                name_was_auto=name_was_auto,
            )
            return child_state, handoff_result

        # --- Handoff mode: assemble context from parent history ---
        try:
            resume_strategy = ResumeStrategy(strategy)
        except ValueError:
            resume_strategy = ResumeStrategy.STRUCTURED

        if resume_strategy == ResumeStrategy.FULL and context_limit is not None:
            transcripts = parent_state.confirmed.artifacts.get("transcripts", [])
            if transcripts and isinstance(transcripts, list) and len(transcripts) > 0:
                latest = transcripts[-1]
                if isinstance(latest, dict):
                    copied_path = latest.get("copied_path")
                    if isinstance(copied_path, str):
                        transcript_path = resolve_artifact_path(parent_artifact_root, copied_path)
                        if transcript_path is not None and transcript_path.is_file():
                            token_estimate = estimate_transcript_tokens(
                                transcript_path,
                                multiplier=token_estimate_multiplier,
                            )
                            if token_estimate > context_limit:
                                raise ContextBudgetExceededError(token_estimate, context_limit)

        def get_session_safe(session_name: str) -> SessionState | None:
            try:
                return self.get_session(session_name, forge_root=parent_forge_root)
            except SessionNotFoundError:
                return None

        handoff_result = process_handoff(
            parent_name=parent_name,
            parent_state=parent_state,
            forge_root=parent_artifact_root,
            strategy=resume_strategy,
            depth=depth,
            get_session=get_session_safe,
        )

        # claude_session_id stays None until the SessionStart hook fires
        child_state = self._create_resume_child(
            child_name=child_name,
            parent_name=parent_name,
            parent_state=parent_state,
            parent_entry=parent_entry,
            inherited_proxy=inherited_proxy,
            parent_proxy_template=parent_proxy_template,
            parent_proxy_base_url=parent_proxy_base_url,
        )

        child_state.confirmed.derivation = Derivation(
            parent_session=parent_name,
            parent_transcript=handoff_result.transcript_artifact_path,
            inherited_proxy=inherited_proxy,
            resume_mode="handoff",
            strategy=strategy,
            depth=depth,
            resumed_at=timestamp,
            lineage=handoff_result.lineage,
            context_file=handoff_result.context_file_rel,
            parent_forge_root=parent_entry.forge_root or parent_entry.worktree_path,
            parent_project_root=parent_entry.project_root,
        )

        self._persist_resume_child(
            child_state=child_state,
            child_name=child_name,
            parent_name=parent_name,
            parent_entry=parent_entry,
            project_root=project_root,
            name_was_auto=name_was_auto,
        )
        return child_state, handoff_result

    def _create_resume_child(
        self,
        *,
        child_name: str,
        parent_name: str,
        parent_state: SessionState,
        parent_entry: SessionIndexEntry,
        inherited_proxy: str | None,
        parent_proxy_template: str | None,
        parent_proxy_base_url: str | None,
    ) -> SessionState:
        """Create a child SessionState for resume (shared by native and handoff)."""
        child_state = create_session_state(
            name=child_name,
            proxy_template=inherited_proxy or parent_proxy_template,
            proxy_base_url=parent_proxy_base_url if (inherited_proxy or parent_proxy_template) else None,
            is_incognito=parent_state.is_incognito,
            worktree_path=parent_entry.worktree_path,
            worktree_branch=parent_state.worktree.branch if parent_state.worktree else None,
        )

        for field_name in ("subprocess_proxy", "policy", "memory", "system_prompt", "verification"):
            parent_val = getattr(parent_state.intent, field_name, None)
            if parent_val is not None:
                setattr(child_state.intent, field_name, deepcopy(parent_val))
        inherited_launch = _inherited_launch_intent(parent_state)
        if inherited_launch is not None:
            child_state.intent.launch = inherited_launch

        child_state.parent_session = parent_name
        child_state.is_fork = False  # Same worktree, context continuation (not a fork)
        # Propagate identity from parent
        child_state.forge_root = parent_entry.forge_root or parent_state.forge_root
        return child_state

    def _persist_resume_child(
        self,
        *,
        child_state: SessionState,
        child_name: str,
        parent_name: str,
        parent_entry: SessionIndexEntry,
        project_root: Path,
        name_was_auto: bool,
    ) -> None:
        """Write child session to disk and index (shared by native and handoff).

        Race protection: if an auto-generated name collides at add_from_state
        (concurrent resume), retry once with a fresh timestamp suffix.
        """
        for attempt in range(2):
            child_store = SessionStore(parent_entry.forge_root or parent_entry.worktree_path, child_name)
            child_store.write(child_state)

            try:
                self.index_store.add_from_state(
                    child_state,
                    str(project_root),
                    checkout_root=parent_entry.checkout_root,
                    forge_root=parent_entry.forge_root,
                    relative_path=parent_entry.relative_path,
                )
                break  # Success
            except SessionExistsError:
                child_store.delete()

                if not name_was_auto or attempt > 0:
                    raise

                child_name = self._generate_resume_name(parent_name)
                child_state.name = child_name

    def _load_existing_fork_target(
        self,
        *,
        fork_name: str,
        target_forge_root: str,
    ) -> tuple[SessionStore, SessionIndexEntry | None, SessionState | None]:
        """Return the existing manifest/index state for a fork target.

        Uses the index self-healing path so stale index-only entries do not
        block retries.
        """
        target_store = SessionStore(target_forge_root, fork_name)

        try:
            target_entry = self.index_store.get_session(fork_name, forge_root=target_forge_root)
        except SessionNotFoundError:
            target_entry = None

        target_state: SessionState | None = None
        if target_store.exists():
            try:
                target_state = target_store.read()
            except (ManifestCorruptedError, ManifestValidationError):
                target_state = None

        return target_store, target_entry, target_state

    def _can_force_replace_fork_target(
        self,
        *,
        fork_name: str,
        parent_name: str,
        target_forge_root: str,
        existing_state: SessionState | None,
        expected_worktree_path: str,
        expected_branch: str,
        expected_is_worktree: bool,
        expected_owns_worktree: bool,
    ) -> bool:
        """Return True when --force is replacing the stale child it created.

        Replacement is intentionally narrow: the existing session must already
        be a fork from this parent, point at the same target checkout/branch,
        and be inactive.
        """
        if existing_state is None:
            return False
        if not existing_state.is_fork or existing_state.parent_session != parent_name:
            return False
        if (
            existing_state.forge_root is not None
            and Path(existing_state.forge_root).resolve() != Path(target_forge_root).resolve()
        ):
            return False

        existing_worktree = existing_state.worktree
        if existing_worktree is None:
            return False
        if Path(existing_worktree.path).resolve() != Path(expected_worktree_path).resolve():
            return False
        if existing_worktree.branch != expected_branch:
            return False
        if existing_worktree.is_worktree != expected_is_worktree:
            return False
        if expected_is_worktree and getattr(existing_worktree, "owns_worktree", True) != expected_owns_worktree:
            return False

        try:
            from .active import ActiveSessionStore

            if ActiveSessionStore().get_session(fork_name, forge_root=target_forge_root) is not None:
                return False
        except Exception as e:
            logger.debug("Unable to verify active state for fork target '%s': %s", fork_name, e)
            return False

        return True

    def fork_session(
        self,
        parent_name: str,
        fork_name: str | None = None,
        *,
        direct: bool = False,
        is_incognito: bool = False,
        create_worktree: bool = False,
        branch: str | None = None,
        into_path: str | None = None,
        forge_root: str | None = None,
        force: bool = False,
    ) -> tuple[SessionState, SessionState]:
        """Fork an existing session.

        By default the fork shares the parent's directory so Claude's
        ``--resume --fork-session`` can find the conversation (conversations
        are project-scoped).  Pass ``create_worktree=True`` for code
        isolation in a separate git worktree, or ``into_path`` to land
        in an existing worktree directory.

        Args:
            parent_name: Session name to fork from.
            fork_name: Name for the fork (auto-generated if None).
            is_incognito: Whether the fork should auto-delete on exit.
            create_worktree: Create a git worktree for the fork (default False).
            branch: Override branch name (only used when create_worktree=True).
            into_path: Fork into an existing worktree directory (normalized checkout root).
            force: Replace only a conflicting target that is provably the same
                stale fork (same parent + same target) and inactive. Hard
                constraints still apply: BranchInUseError,
                BranchNotMergedError, and non-worktree paths.

        Returns:
            Tuple of (parent_manifest, fork_manifest).

        Raises:
            SessionNotFoundError: If parent doesn't exist.
            CannotForkIncognitoError: If parent is incognito.
            SessionExistsError: If fork_name already exists (and not force).
            BranchExistsError: If branch already exists (create_worktree only, not force).
            WorktreePathExistsError: If worktree path exists (create_worktree only, not force).
            BranchInUseError: If branch is checked out elsewhere (force only).
            BranchNotMergedError: If branch has unmerged work (force only).
        """
        parent = self.get_session(parent_name, forge_root=forge_root)
        parent_entry = self.index_store.get_session(parent_name, forge_root=forge_root)
        parent_forge_root = parent_entry.forge_root or parent_entry.worktree_path

        if parent.is_incognito:
            raise CannotForkIncognitoError(parent_name)

        if fork_name is None:
            existing = {name for name, _ in self.list_sessions(forge_root_filter=parent_forge_root)}
            fork_name = generate_unique_name(existing)

        parent_worktree_path = Path(parent.worktree.path) if parent.worktree else Path.cwd()
        parent_relative = parent_entry.relative_path or "."

        target_forge_root: str | None = None
        target_store: SessionStore | None = None
        target_entry: SessionIndexEntry | None = None
        target_state: SessionState | None = None
        replace_stale_target_state = False
        created_worktree = False
        rollback_worktree_path: str | None = None
        rollback_worktree_branch: str | None = None
        rollback_repo_root: Path | None = None

        def _rollback_created_worktree() -> None:
            if not created_worktree or rollback_worktree_path is None:
                return
            try:
                from .worktree import cleanup_worktree

                cleanup_worktree(
                    worktree_path=Path(rollback_worktree_path),
                    branch=rollback_worktree_branch,
                    delete_branch_flag=True,
                    force=True,
                    repo_root=rollback_repo_root,
                )
            except Exception as e:
                logger.warning("Fork rollback cleanup failed for '%s': %s", rollback_worktree_path, e)

        if into_path is not None:
            # Fork into an existing worktree (--into): land at the equivalent
            # forge_root position in the target checkout.
            from .worktree import get_main_repo_root

            target_checkout_root = into_path  # Already normalized to checkout root by CLI
            target_forge_root = str(Path(target_checkout_root) / parent_relative)

            # Validate: target must have Forge enabled at that position
            if not (Path(target_forge_root) / ".forge").is_dir():
                raise ForgeSessionError(
                    f"No Forge project at {target_forge_root}. "
                    f"Run 'forge extension enable' in {target_forge_root} first, "
                    "or use --worktree to create a new checkout with auto-enable."
                )

            fork_worktree_path = target_checkout_root
            fork_branch: str | None = branch  # CLI resolves branch from git
            project_root = str(get_main_repo_root(Path(into_path)))
            is_into = True

            assert target_forge_root is not None
            target_store, target_entry, target_state = self._load_existing_fork_target(
                fork_name=fork_name,
                target_forge_root=target_forge_root,
            )
            target_conflict_exists = target_store.exists() or target_entry is not None
            if target_conflict_exists:
                if not force:
                    raise SessionExistsError(fork_name)

                replace_stale_target_state = self._can_force_replace_fork_target(
                    fork_name=fork_name,
                    parent_name=parent_name,
                    target_forge_root=target_forge_root,
                    existing_state=target_state,
                    expected_worktree_path=fork_worktree_path,
                    expected_branch=fork_branch or fork_name,
                    expected_is_worktree=True,
                    expected_owns_worktree=False,
                )
                if not replace_stale_target_state:
                    raise SessionExistsError(fork_name)
        elif create_worktree:
            from .worktree import (
                copy_runtime_config,
            )
            from .worktree import create_worktree as git_create_worktree
            from .worktree import (
                get_main_repo_root,
                resolve_worktree_path,
                sanitize_branch_name,
            )

            repo_root = get_main_repo_root(parent_worktree_path)
            target_worktree_path = resolve_worktree_path(repo_root, fork_name)
            target_forge_root = str(target_worktree_path / parent_relative)
            target_branch = branch or sanitize_branch_name(fork_name)
            target_store, target_entry, target_state = self._load_existing_fork_target(
                fork_name=fork_name,
                target_forge_root=target_forge_root,
            )
            target_conflict_exists = target_store.exists() or target_entry is not None
            if target_conflict_exists:
                if not force:
                    raise SessionExistsError(fork_name)

                replace_stale_target_state = self._can_force_replace_fork_target(
                    fork_name=fork_name,
                    parent_name=parent_name,
                    target_forge_root=target_forge_root,
                    existing_state=target_state,
                    expected_worktree_path=str(target_worktree_path),
                    expected_branch=target_branch,
                    expected_is_worktree=True,
                    expected_owns_worktree=True,
                )
                if not replace_stale_target_state:
                    raise SessionExistsError(fork_name)
            wt_result = git_create_worktree(
                session_name=fork_name,
                branch=branch,
                cwd=repo_root,
                force=force,
                replace_owned_stale_state=replace_stale_target_state,
            )
            created_worktree = True
            rollback_worktree_path = wt_result.worktree_path
            rollback_worktree_branch = wt_result.branch
            rollback_repo_root = repo_root
            copy_runtime_config(repo_root, Path(wt_result.worktree_path))

            fork_worktree_path = wt_result.worktree_path
            fork_branch = wt_result.branch
            project_root = str(repo_root)
            is_into = False
        else:
            target_forge_root = parent_forge_root
            fork_worktree_path = str(parent_worktree_path)
            fork_branch = parent.worktree.branch if parent.worktree else None
            project_root = str(find_project_root(fork_worktree_path))
            is_into = False
            assert target_forge_root is not None
            target_store, target_entry, target_state = self._load_existing_fork_target(
                fork_name=fork_name,
                target_forge_root=target_forge_root,
            )
            target_conflict_exists = target_store.exists() or target_entry is not None
            if target_conflict_exists:
                if not force:
                    raise SessionExistsError(fork_name)

                replace_stale_target_state = self._can_force_replace_fork_target(
                    fork_name=fork_name,
                    parent_name=parent_name,
                    target_forge_root=target_forge_root,
                    existing_state=target_state,
                    expected_worktree_path=fork_worktree_path,
                    expected_branch=fork_branch or fork_name,
                    expected_is_worktree=False,
                    expected_owns_worktree=False,
                )
                if not replace_stale_target_state:
                    raise SessionExistsError(fork_name)

        if direct:
            fork_proxy_template = None
            fork_proxy_base_url = None
        else:
            fork_proxy_template = parent.intent.proxy.template if parent.intent.proxy else None
            fork_proxy_base_url = parent.intent.proxy.base_url if parent.intent.proxy else None

        fork_state = create_session_state(
            name=fork_name,
            proxy_template=fork_proxy_template,
            proxy_base_url=fork_proxy_base_url,
            parent_session=parent_name,
            is_fork=True,
            is_incognito=is_incognito,
            worktree_path=fork_worktree_path,
            worktree_branch=fork_branch,
        )

        for field_name in ("subprocess_proxy", "policy", "memory", "system_prompt", "verification"):
            parent_val = getattr(parent.intent, field_name, None)
            if parent_val is not None:
                setattr(fork_state.intent, field_name, deepcopy(parent_val))
        inherited_launch = _inherited_launch_intent(parent)
        if inherited_launch is not None:
            fork_state.intent.launch = inherited_launch
        # Direct mode: force host launch (sidecar requires a proxy)
        if direct and fork_state.intent.launch and fork_state.intent.launch.mode != LAUNCH_MODE_HOST:
            fork_state.intent.launch.mode = LAUNCH_MODE_HOST
            fork_state.intent.launch.sidecar = None

        if (create_worktree or is_into) and fork_state.worktree:
            fork_state.worktree.is_worktree = True
        if is_into and fork_state.worktree:
            fork_state.worktree.owns_worktree = False

        # Compute identity fields for the fork target.
        fork_forge_root: str | None
        fork_relative_path: str | None
        if is_into:
            assert target_forge_root is not None
            fork_forge_root = target_forge_root
            fork_checkout_root = fork_worktree_path
            fork_relative_path = parent_entry.relative_path or "."
        elif create_worktree:
            # Fresh worktree has no .forge/; propagate parent's relative position.
            parent_relative = parent_entry.relative_path or "."
            fork_forge_root = str(Path(fork_worktree_path) / parent_relative)
            fork_checkout_root = fork_worktree_path
            fork_relative_path = parent_relative
        else:
            # Same-worktree fork: auto-detect
            from forge.core.ops.context import find_forge_root

            fork_forge_root_path = find_forge_root(Path(fork_worktree_path))
            fork_forge_root = str(fork_forge_root_path) if fork_forge_root_path else None
            fork_checkout_root = fork_worktree_path
            fork_relative_path = None
            if fork_forge_root and fork_checkout_root:
                try:
                    fork_relative_path = str(Path(fork_forge_root).relative_to(fork_checkout_root))
                except ValueError:
                    fork_relative_path = "."

        fork_state.forge_root = fork_forge_root
        fork_state.confirmed.derivation = Derivation(
            parent_session=parent_name,
            parent_transcript=_latest_transcript_artifact_path(parent),
            inherited_proxy=fork_proxy_template,
            resume_mode="handoff" if (create_worktree or is_into) else "native",
            strategy=None,
            depth=1,
            resumed_at=now_iso(),
            lineage=[parent_name],
            context_file=None,
            parent_forge_root=parent_entry.forge_root or parent_entry.worktree_path,
            parent_project_root=parent_entry.project_root,
        )

        fork_store = SessionStore(fork_forge_root or fork_worktree_path, fork_name)
        restore_target_state = replace_stale_target_state and not create_worktree
        replaced_target_state = False
        wrote_manifest = False
        added_to_index = False

        def _restore_previous_target_state() -> None:
            if not restore_target_state or not replaced_target_state or target_store is None or target_state is None:
                return

            try:
                target_store.write(target_state)
            except Exception as e:
                logger.warning("Failed to restore fork target manifest '%s': %s", fork_name, e)

            if target_entry is None:
                return

            try:
                self.index_store.add_from_state(
                    target_state,
                    target_entry.project_root,
                    checkout_root=target_entry.checkout_root,
                    forge_root=target_entry.forge_root,
                    relative_path=target_entry.relative_path,
                )
            except Exception as e:
                logger.warning("Failed to restore fork target index entry '%s': %s", fork_name, e)

        try:
            # Stale session cleanup: only clear the actual target namespace after
            # all validation succeeds. Git worktree replacement (if any) has
            # already happened, so this only swaps the session metadata layer.
            if replace_stale_target_state:
                effective_fork_root = fork_forge_root or fork_worktree_path
                try:
                    self.delete_session(
                        fork_name,
                        delete_worktree=False,
                        delete_branch=False,
                        force=True,
                        forge_root=effective_fork_root,
                    )
                except SessionNotFoundError:
                    pass

                stale_store = SessionStore(effective_fork_root, fork_name)
                if stale_store.exists():
                    stale_store.delete()

                try:
                    from .active import ActiveSessionStore

                    ActiveSessionStore().clear_session(fork_name, forge_root=effective_fork_root)
                except Exception as e:
                    logger.debug("Failed to clear active session '%s' (non-critical): %s", fork_name, e)

                replaced_target_state = True

            fork_store.write(fork_state)
            wrote_manifest = True

            self.index_store.add_from_state(
                fork_state,
                project_root,
                checkout_root=fork_checkout_root,
                forge_root=fork_forge_root,
                relative_path=fork_relative_path,
            )
            added_to_index = True

            return parent, fork_state

        except Exception:
            try:
                if added_to_index:
                    self.index_store.remove_session(fork_name, forge_root=fork_forge_root)
            except Exception as rollback_err:
                logger.warning("Fork rollback failed (index entry): %s", rollback_err)

            try:
                if wrote_manifest and fork_store.exists():
                    fork_store.delete()
            except Exception as rollback_err:
                logger.warning("Fork rollback failed (manifest delete): %s", rollback_err)

            if create_worktree:
                _rollback_created_worktree()
            else:
                _restore_previous_target_state()

            raise

    def relaunch_session(
        self,
        parent_name: str,
        *,
        child_name: str | None = None,
        forge_root: str | None = None,
    ) -> tuple[SessionState, SessionState]:
        """Create a child session for relaunching a previously-used parent.

        Lightweight derivation: inherits intent/overrides/proxy, sets
        parent_session lineage. Does NOT pre-seed claude_session_id
        (launch-owned). Does NOT assemble context (unlike resume_session).

        The caller should launch Claude with ``--resume --fork-session``
        using the parent's claude_session_id so the conversation carries
        over into a distinct new Claude UUID.

        Args:
            parent_name: Session to relaunch.
            child_name: Name for the child (auto-generated if None).

        Returns:
            Tuple of (parent_state, child_state).

        Raises:
            SessionNotFoundError: If parent doesn't exist.
        """
        parent = self.get_session(parent_name, forge_root=forge_root)
        parent_entry = self.index_store.get_session(parent_name, forge_root=forge_root)
        parent_forge_root = parent_entry.forge_root or parent_entry.worktree_path

        if child_name is None:
            child_name = self._generate_relaunch_name(parent_name, forge_root=parent_forge_root)

        if self.index_store.session_exists(child_name, forge_root=parent_forge_root):
            raise SessionExistsError(child_name)

        parent_worktree_path = parent_entry.worktree_path
        project_root = parent_entry.project_root

        proxy_template = parent.intent.proxy.template if parent.intent.proxy else None
        proxy_base_url = parent.intent.proxy.base_url if parent.intent.proxy else None

        child_state = create_session_state(
            name=child_name,
            proxy_template=proxy_template,
            proxy_base_url=proxy_base_url,
            parent_session=parent_name,
            is_fork=True,
            is_incognito=parent.is_incognito,
            worktree_path=parent_worktree_path,
            worktree_branch=parent.worktree.branch if parent.worktree else None,
        )

        for field_name in ("subprocess_proxy", "policy", "memory", "system_prompt", "verification"):
            parent_val = getattr(parent.intent, field_name, None)
            if parent_val is not None:
                setattr(child_state.intent, field_name, deepcopy(parent_val))
        inherited_launch = _inherited_launch_intent(parent)
        if inherited_launch is not None:
            child_state.intent.launch = inherited_launch
        child_state.overrides = deepcopy(parent.overrides)

        # Propagate identity from parent
        child_state.forge_root = parent_entry.forge_root or parent.forge_root

        child_store = SessionStore(parent_entry.forge_root or parent_worktree_path, child_name)
        child_store.write(child_state)
        self.index_store.add_from_state(
            child_state,
            project_root,
            checkout_root=parent_entry.checkout_root,
            forge_root=parent_entry.forge_root,
            relative_path=parent_entry.relative_path,
        )

        return parent, child_state

    def _generate_relaunch_name(self, parent_name: str, forge_root: str | None = None) -> str:
        """Generate a unique name for a relaunched session (project-scoped)."""
        existing = {name for name, _ in self.list_sessions(forge_root_filter=forge_root)}
        return generate_unique_name(existing)

    def _generate_resume_name(self, parent_name: str, forge_root: str | None = None) -> str:
        """Generate a unique name for a resumed session (project-scoped)."""
        base_name = f"{parent_name}-resumed"
        if not self.index_store.session_exists(base_name, forge_root=forge_root):
            return base_name

        from datetime import datetime

        suffix = datetime.now().strftime("%H%M%S")
        return f"{parent_name}-resumed-{suffix}"

    def _find_co_resident_sessions(self, worktree_path: str, exclude: str) -> list[str]:
        """Find other sessions living in the same worktree directory.

        Uses list_sessions() (self-healing) to avoid stale entries blocking cleanup.
        """
        normalized = str(Path(worktree_path).resolve())
        return [
            name
            for name, entry in self.index_store.list_sessions()
            if str(Path(entry.worktree_path).resolve()) == normalized and name != exclude
        ]

    def delete_session(
        self,
        name: str,
        *,
        delete_transcripts: bool = True,
        delete_worktree: bool = True,
        delete_branch: bool = False,
        force: bool = False,
        forge_root: str | None = None,
    ) -> None:
        """Delete a session and optionally its worktree and transcripts.

        Removes the session from the index, deletes the manifest, and
        optionally cleans up the git worktree and transcript files.

        Args:
            name: Session name to delete.
            delete_transcripts: Whether to delete transcript files (default True).
            delete_worktree: Whether to remove the git worktree (default True).
            delete_branch: Whether to delete the git branch (default False).
            force: Force removal even with uncommitted changes (default False).

        Raises:
            SessionNotFoundError: If session doesn't exist.
            InvalidSessionNameError: If name is invalid.
            DirtyWorktreeError: If worktree has uncommitted changes and force=False.
        """
        from .claude.cleanup import cleanup_session

        entry = self.index_store.get_session(name, forge_root=forge_root)
        entry_forge_root = entry.forge_root or entry.worktree_path
        store = SessionStore(entry_forge_root, name)

        state = None
        _raw_data: dict[str, Any] | None = None
        if store.exists():
            try:
                state = store.read()
            except (ManifestCorruptedError, ManifestValidationError):
                if not force:
                    raise
                # Best-effort: read raw JSON for cleanup-relevant fields
                # even though full deserialization failed.
                _raw_data = store.read_raw()
                logger.warning(
                    "Manifest corrupted; force-deleting with best-effort cleanup "
                    "(transcript/worktree cleanup may be incomplete)"
                )

        # Build cleanup hints from raw data when state is unavailable
        _claude_session_id: str | None = None
        _worktree_info: dict[str, Any] | None = None
        if state:
            _claude_session_id = state.confirmed.claude_session_id
            if state.worktree:
                _worktree_info = {
                    "path": state.worktree.path,
                    "is_worktree": state.worktree.is_worktree,
                    "owns_worktree": getattr(state.worktree, "owns_worktree", True),
                    "branch": state.worktree.branch,
                }
        elif _raw_data:
            confirmed = _raw_data.get("confirmed", {})
            if isinstance(confirmed, dict):
                _claude_session_id = confirmed.get("claude_session_id")
            wt = _raw_data.get("worktree")
            if isinstance(wt, dict) and wt.get("path"):
                _worktree_info = {
                    "path": wt["path"],
                    "is_worktree": wt.get("is_worktree", False),
                    "owns_worktree": wt.get("owns_worktree", True),
                    "branch": wt.get("branch"),
                }

        # Worktree cleanup decision: determine BEFORE any destructive work whether
        # we'll remove the worktree. This lets the dirty preflight block everything
        # (transcripts + worktree + index removal) atomically.
        _should_cleanup_worktree = False
        if delete_worktree and _worktree_info and _worktree_info["is_worktree"]:
            _owns = _worktree_info["owns_worktree"]
            co_residents = self._find_co_resident_sessions(_worktree_info["path"], exclude=name)
            if co_residents:
                logger.info(
                    "Skipping worktree removal: %d other session(s) present (%s)",
                    len(co_residents),
                    ", ".join(co_residents[:3]),
                )
            elif not _owns:
                logger.info("Skipping worktree removal: session does not own worktree (--into)")
            else:
                _should_cleanup_worktree = True

        # Dirty-worktree preflight: only check if we'll actually remove the worktree.
        # Runs before transcript cleanup so DirtyWorktreeError blocks all destructive work.
        # Shared worktrees (co-residents or --into) skip this entirely.
        if _should_cleanup_worktree and _worktree_info:
            from .worktree import is_worktree_dirty

            worktree_path = Path(_worktree_info["path"])
            if not force and worktree_path.exists() and is_worktree_dirty(worktree_path):
                raise DirtyWorktreeError(str(worktree_path))

        if _should_cleanup_worktree and _worktree_info:
            from .worktree import cleanup_worktree

            worktree_path = Path(_worktree_info["path"])
            branch = _worktree_info["branch"] if delete_branch else None

            cleanup_result = cleanup_worktree(
                worktree_path=worktree_path,
                branch=branch,
                delete_branch_flag=delete_branch,
                force=force,
            )

            if cleanup_result.errors:
                raise ForgeSessionError(cleanup_result.errors[0])

        if delete_transcripts and _claude_session_id:
            _artifact_ids = _tracked_transcript_session_ids(state) if state else [_claude_session_id]
            cleanup_session(
                project_root=entry.forge_root or entry.worktree_path,
                claude_session_id=_claude_session_id,
                artifact_session_ids=_artifact_ids,
            )

        self.index_store.remove_session(name, forge_root=entry_forge_root)

        # Delete manifest file (only if worktree still exists or wasn't a worktree)
        if store.exists():
            store.delete()

        try:
            from .active import ActiveSessionStore

            ActiveSessionStore().clear_session(name, forge_root=entry_forge_root)
        except Exception as e:
            logger.debug("Failed to clear active session '%s' (non-critical): %s", name, e)
