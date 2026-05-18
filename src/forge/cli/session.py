"""Session management CLI commands.

Commands for managing Claude Code sessions:
- start: Create and start a new session
- resume: Resume a session (reattach or --fresh for context handoff)
- fork: Fork an existing session
- delete: Delete a session
- list: List all sessions
- show: Show the current or named session
- switch: Switch to a different session
- shell: Open a shell in a sidecar session
- set/reset: Manage session overrides
- incognito: Start an incognito session

Lifecycle commands (start, resume, fork, incognito) live in session_lifecycle.py.
Management commands (delete, list, clean, show, etc.) live in session_manage.py.
Both are re-exported here so ``patch("forge.cli.session.XXX")`` keeps working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console

from forge.core.paths import display_path
from forge.core.reactive.env import (
    FORGE_SUBPROCESS_BASE_URL_VAR,
    FORGE_SUBPROCESS_PROXY_ID_VAR,
    FORGE_SUBPROCESS_PROXY_VAR,
    FORGE_SUBPROCESS_TEMPLATE_VAR,
)
from forge.core.state import parse_iso
from forge.session import (
    LAUNCH_MODE_HOST,
    LAUNCH_MODE_SIDECAR,
    ActiveSessionEntry,
    ForgeSessionError,
    SessionIndexEntry,
    SessionManager,
    SessionState,
)
from forge.session.exceptions import (
    AmbiguousSessionError,
    SessionNotFoundError,
)

logger = logging.getLogger(__name__)

# Shared console for Rich output
console = Console()


# --- Routing resolution ---


@dataclass(frozen=True)
class ResolvedRouting:
    """Resolved proxy routing for a session launch.

    Produced by _resolve_routing_from_cli() and threaded through
    launch_new_session, resume, fork, etc.
    """

    template: str | None = None
    base_url: str | None = None
    proxy_id: str | None = None
    context_limit: int | None = None

    @property
    def is_direct(self) -> bool:
        return self.base_url is None


def _resolve_routing_from_cli(
    *,
    proxy_name: str | None,
    direct: bool,
) -> ResolvedRouting:
    """Resolve --proxy/--no-proxy CLI flags to a ResolvedRouting.

    Performs registry lookup + healthcheck for --proxy. Returns
    a direct routing for --no-proxy. Callers must validate mutual
    exclusivity before calling.

    Raises click.ClickException on resolution/healthcheck failure.
    """
    if direct or not proxy_name:
        return ResolvedRouting()

    from forge.cli.claude import _get_context_limit_for_proxy, _healthcheck_proxy
    from forge.proxy.proxies import (
        ProxyRegistryCorruptedError,
        ProxyRegistryStore,
        ProxyResolutionError,
        resolve_proxy,
    )

    store = ProxyRegistryStore()
    try:
        registry = store.read()
    except ProxyRegistryCorruptedError as e:
        raise click.ClickException(str(e))

    try:
        entry = resolve_proxy(registry, proxy_name)
    except ProxyResolutionError as e:
        raise click.ClickException(str(e))

    try:
        _healthcheck_proxy(
            base_url=entry.base_url,
            expected_template=entry.template,
            expected_proxy_id=entry.proxy_id,
        )
    except ValueError as e:
        msg = str(e)
        if "not running" in msg:
            msg += f"\nTip: Run 'forge proxy start {entry.proxy_id}' to start it."
        raise click.ClickException(msg)

    return ResolvedRouting(
        template=entry.template,
        base_url=entry.base_url,
        proxy_id=entry.proxy_id,
        context_limit=_get_context_limit_for_proxy(entry.proxy_id),
    )


def _apply_routing_override_to_state(
    *,
    state: SessionState,
    routing: ResolvedRouting | None,
    direct: bool,
) -> None:
    """Apply a CLI routing override to an in-memory session state."""
    if not routing and not direct:
        return

    from forge.session.models import LaunchIntent, ProxyIntent

    # Explicit CLI routing beats any stale last-launch proxy snapshot.
    state.confirmed.started_with_proxy = None

    if direct:
        state.intent.proxy = None
        if state.intent.launch is None:
            state.intent.launch = LaunchIntent(mode=LAUNCH_MODE_HOST)
        else:
            state.intent.launch.mode = LAUNCH_MODE_HOST
            state.intent.launch.sidecar = None
        return

    assert routing is not None
    state.intent.proxy = ProxyIntent(
        template=routing.template or "",
        base_url=routing.base_url or "",
    )


def _persist_routing_override(
    *,
    forge_root: Path,
    session_name: str,
    routing: ResolvedRouting | None,
    direct: bool,
) -> None:
    """Persist a --proxy/--no-proxy CLI override into the session manifest.

    Called after manager.fork_session()/resume_session() creates the child
    so the intent reflects the override, not the inherited parent routing.
    This ensures --no-launch forks retain the requested proxy.

    Only persists intent changes -- confirmed.started_with_proxy is hook-owned
    and must not be cleared on disk before a successful launch. The in-memory
    clearing in _apply_routing_override_to_state() is sufficient for the
    current launch; the SessionStart hook will update confirmed on success.
    """
    if not routing and not direct:
        return

    from forge.session import SessionStore
    from forge.session.models import LaunchIntent, ProxyIntent

    store = SessionStore(str(forge_root), session_name)

    def _mutate(m: SessionState) -> None:
        if direct:
            m.intent.proxy = None
            if m.intent.launch is None:
                m.intent.launch = LaunchIntent(mode=LAUNCH_MODE_HOST)
            else:
                m.intent.launch.mode = LAUNCH_MODE_HOST
                m.intent.launch.sidecar = None
        elif routing is not None:
            m.intent.proxy = ProxyIntent(
                template=routing.template or "",
                base_url=routing.base_url or "",
            )

    try:
        store.update(timeout_s=5.0, mutate=_mutate)
    except Exception:
        logger.debug("Failed to persist routing override to manifest", exc_info=True)


def _cwd_forge_root() -> str | None:
    """Resolve forge_root from CWD for project-scoped session lookups."""
    try:
        from forge.core.ops.context import find_forge_root

        fr = find_forge_root(Path.cwd().resolve())
        return str(fr) if fr else None
    except Exception:
        return None


def _session_scope_key(name: str, entry: SessionIndexEntry) -> tuple[str, str]:
    """Return the list/cleanup identity tuple for a session entry."""
    return (name, entry.forge_root or entry.worktree_path)


def _session_list_location(entry: SessionIndexEntry) -> str:
    """Return a short location label for human session-list disambiguation."""
    if entry.relative_path and entry.relative_path != ".":
        return entry.relative_path

    root = entry.forge_root or entry.worktree_path
    return Path(root).name if root else "."


def _default_context_limit() -> int:
    from forge.runtime_config import get_runtime_config

    return get_runtime_config().context_limit


def _resolve_context_limit(proxy_ref: str | None) -> int:
    """Compute context limit by resolving a proxy for the given proxy_id or template name.

    Uses resolve_proxy_optional() which tries exact proxy_id match first,
    then unique active template match. Falls back to _default_context_limit()
    if no match, ambiguous, or config is malformed.

    Args:
        proxy_ref: Proxy ID or template name (e.g., "openrouter-gemini").

    Returns:
        Context window size in tokens, or _default_context_limit() if no match found.
    """
    if not proxy_ref:
        return _default_context_limit()

    try:
        from forge.config.loader import load_proxy_instance_config
        from forge.core.models import get_context_window_tokens
        from forge.proxy.proxies import ProxyRegistryStore, resolve_proxy_optional

        store = ProxyRegistryStore()
        registry = store.read()

        entry = resolve_proxy_optional(registry, proxy_ref)
        if entry is None:
            logger.debug(f"No matching proxy found for '{proxy_ref}', using default")
            return _default_context_limit()

        proxy_config = load_proxy_instance_config(entry.proxy_id)
        if proxy_config is None:
            logger.debug(f"No proxy config found for {entry.proxy_id}, using default")
            return _default_context_limit()

        tier = proxy_config.default_tier or "sonnet"
        model = proxy_config.tiers.get(tier)
        if not model:
            logger.debug(f"No model for tier {tier} in proxy {entry.proxy_id}, using default")
            return _default_context_limit()

        context_limit = get_context_window_tokens(model)
        logger.debug(f"Computed context limit {context_limit} for '{proxy_ref}' via proxy {entry.proxy_id}")
        return context_limit
    except Exception as e:
        logger.debug(f"Failed to compute context limit for '{proxy_ref}': {e}")
        return _default_context_limit()


def _format_relative_time(iso_timestamp: str) -> str:
    """Format an ISO timestamp as a human-readable relative time."""
    try:
        dt = parse_iso(iso_timestamp)
        now = datetime.now(UTC)
        delta = now - dt

        seconds = delta.total_seconds()
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} min{'s' if minutes != 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif seconds < 604800:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"
        else:
            weeks = int(seconds / 604800)
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    except (ValueError, TypeError):
        return "unknown"


def _get_session_type(
    is_fork: bool,
    is_incognito: bool,
    parent_session: str | None,
) -> str:
    """Get a human-readable session type string."""
    if is_incognito:
        if is_fork and parent_session:
            return f"fork of {parent_session} (incognito)"
        return "incognito"
    if is_fork and parent_session:
        return f"fork of {parent_session}"
    return "session"


def _get_effective_proxy_for_session(
    state: SessionState,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the best-known template/base_url/proxy_id for a session.

    Returns (template, base_url, proxy_id). The proxy_id (when available)
    enables deterministic context limit computation via exact registry
    lookup, avoiding active-only template resolution.
    """
    if state.confirmed.started_with_proxy:
        return (
            state.confirmed.started_with_proxy.template,
            state.confirmed.started_with_proxy.base_url,
            state.confirmed.started_with_proxy.proxy_id,
        )

    if state.intent.proxy:
        return state.intent.proxy.template, state.intent.proxy.base_url, None

    return None, None, None


def _template_display_label(template: str | None) -> str:
    """Return a user-facing routing label for list/detail views."""
    return template or "direct"


def _print_routing_summary(*, template: str | None, base_url: str | None) -> None:
    """Print routing details for a session launch summary."""
    if base_url is None:
        console.print("  Routing: direct")
        console.print("  Base URL: default Anthropic")
        return

    if template is None:
        console.print("  Routing: custom base URL")
        console.print(f"  Base URL: {base_url}")
        return

    console.print(f"  Template: {template}")
    console.print(f"  Base URL: {base_url}")


def _build_session_env(
    *,
    session_name: str,
    context_limit: int,
    template: str | None,
    base_url: str | None,
    fork_name: str | None = None,
    parent_session: str | None = None,
    forge_root: str | None = None,
    subprocess_proxy: str | None = None,
    sidecar: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Build Claude env vars plus explicit unsets for a session launch."""
    env_vars: dict[str, str] = {
        "FORGE_SESSION": session_name,
    }
    if forge_root:
        env_vars["FORGE_FORGE_ROOT"] = forge_root
    unset_env_vars: list[str] = []

    if base_url is None:
        # Direct mode: don't touch CLAUDE_CODE_AUTO_COMPACT_WINDOW -- it's a
        # native CC env var the user may have set. Only scrub Forge-managed vars.
        unset_env_vars.append("ANTHROPIC_BASE_URL")
        unset_env_vars.append("ACTIVE_TEMPLATE")
    else:
        # Proxy mode: set compaction window to match the routed model's context.
        env_vars["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context_limit)
        env_vars["ANTHROPIC_BASE_URL"] = base_url
        if template is None:
            unset_env_vars.append("ACTIVE_TEMPLATE")
        else:
            env_vars["ACTIVE_TEMPLATE"] = template

    if subprocess_proxy:
        env_vars[FORGE_SUBPROCESS_PROXY_VAR] = subprocess_proxy
        env_vars.update(_resolve_subprocess_proxy_launch_metadata(subprocess_proxy, sidecar=sidecar))

    if fork_name is not None:
        env_vars["FORGE_FORK_NAME"] = fork_name
    if parent_session is not None:
        env_vars["FORGE_PARENT_SESSION"] = parent_session

    return env_vars, unset_env_vars


def _resolve_subprocess_proxy_launch_metadata(proxy_id: str, *, sidecar: bool = False) -> dict[str, str]:
    """Resolve subprocess proxy metadata to inject into launched sessions."""
    try:
        from forge.proxy.proxies import ProxyRegistryStore, resolve_proxy_optional

        registry = ProxyRegistryStore().read()
        entry = resolve_proxy_optional(registry, proxy_id)
        if entry is None:
            return {}

        base_url = _container_reachable_url(entry.base_url) if sidecar else entry.base_url
        return {
            FORGE_SUBPROCESS_BASE_URL_VAR: base_url,
            FORGE_SUBPROCESS_PROXY_ID_VAR: entry.proxy_id,
            FORGE_SUBPROCESS_TEMPLATE_VAR: entry.template,
        }
    except Exception as e:
        logger.debug("Could not resolve subprocess proxy metadata for %s: %s", proxy_id, e)
        return {}


def _container_reachable_url(base_url: str) -> str:
    """Map host loopback proxy URLs to Docker's host gateway name."""
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    host = "host.docker.internal"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _resolve_extension_detection_root(cwd: Path) -> Path:
    """Return the Forge project root to use for extension inheritance lookup."""
    from forge.core.ops.context import find_forge_root
    from forge.session.claude.paths import find_project_root

    forge_root = find_forge_root(cwd)
    if forge_root is not None:
        return forge_root
    try:
        return find_project_root(str(cwd))
    except FileNotFoundError:
        return cwd.resolve()


def _resolve_worktree_extension_root(manifest: SessionState) -> Path | None:
    """Return where extensions should be installed inside a target worktree.

    Session state may stay anchored at the parent's forge_root for root-level
    worktree sessions, but extensions must still land inside the new checkout.
    Nested Forge projects instead install at the equivalent nested forge_root
    within the worktree.
    """
    if not manifest.worktree or not manifest.worktree.is_worktree:
        return None

    worktree_root = Path(manifest.worktree.path)
    if manifest.forge_root:
        forge_root = Path(manifest.forge_root)
        try:
            forge_root.relative_to(worktree_root)
            return forge_root
        except ValueError:
            pass
    return worktree_root


def _detect_parent_extensions(parent_project_root: Path) -> tuple[str, str] | None:
    """Detect parent's installed extensions for worktree inheritance.

    Returns (profile, mode) or None if no extensions found.
    Checks: LOCAL install at parent root -> USER install -> hook file detection fallback.
    """
    from forge.install.hooks import has_forge_hooks
    from forge.install.tracking import TrackingStore

    # Tiers 1-2: tracking store lookup (may fail if store is corrupt)
    try:
        store = TrackingStore()

        # Tier 1: LOCAL installation at parent project root
        local_install = store.get_installation("local", str(parent_project_root))
        if local_install is not None:
            return (local_install.profile, local_install.mode)

        # Tier 2: USER-scope (global) installation
        user_install = store.get_installation("user")
        if user_install is not None:
            return (user_install.profile, user_install.mode)

    except Exception:
        logger.debug(
            "Tracking store lookup failed, falling through to hook detection",
            exc_info=True,
        )

    # Tier 3: hook file detection fallback (independent of tracking store)
    try:
        if has_forge_hooks(parent_project_root):
            return ("standard", "copy")
    except Exception:
        logger.debug("Hook detection failed", exc_info=True)

    return None


def _prepare_sidecar_prompt_file(
    *,
    worktree_path: Path,
    system_prompt_file: str | None,
) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Map a host-side prompt file to a path visible inside the sidecar."""
    if system_prompt_file is None:
        return None, []

    prompt_path = Path(system_prompt_file).resolve()
    worktree_root = worktree_path.resolve()

    try:
        relative_prompt = prompt_path.relative_to(worktree_root)
    except ValueError:
        container_prompt = f"/tmp/{prompt_path.name}"
        return container_prompt, [(str(prompt_path), container_prompt, "ro")]

    return str(Path("/workspace") / relative_prompt), []


def _auto_install_extensions(
    install_root: Path,
    parent_project_root: Path,
    *,
    force_extensions: bool | None = None,
) -> bool:
    """Auto-install Forge extensions in a new worktree.

    Args:
        install_root: Root inside the target worktree where ``.claude/`` lives.
            For root-level worktrees this is the checkout root; for nested Forge
            projects it is the nested project root within that checkout.
        force_extensions: True=force install, False=skip, None=auto-detect from parent.

    Returns True if extensions were installed.
    Non-blocking: catches all exceptions and warns on failure.
    """
    try:
        if force_extensions is False:
            return False

        if force_extensions is True:
            profile, mode = "standard", "copy"
        else:
            detected = _detect_parent_extensions(parent_project_root)
            if detected is None:
                console.print("[dim]  Extensions: skipped (no parent extensions detected)[/dim]")
                return False
            profile, mode = detected

        from forge.install.installer import Installer
        from forge.install.models import InstallMode, InstallProfile, InstallScope

        installer = Installer(
            scope=InstallScope.LOCAL,
            project_root=install_root,
        )
        plan = installer.init(
            profile=InstallProfile(profile),
            mode=InstallMode(mode),
        )
        if plan.has_conflicts:
            console.print("[dim]  Extensions: skipped (conflicts with existing files)[/dim]")
            return False
        n_modules = len(plan.modules)
        console.print(f"[dim]  Extensions: inherited ({profile} profile, {n_modules} modules)[/dim]")
        return True

    except Exception as e:
        logger.debug("Extension auto-install failed", exc_info=True)
        console.print(f"[dim]  Extensions: failed to install ({e})[/dim]")
        return False


def _get_active_session_entry(session_name: str, forge_root: str | None = None) -> ActiveSessionEntry | None:
    """Return live runtime state for a session, if available."""
    try:
        from forge.session.active import ActiveSessionStore

        return ActiveSessionStore().get_session(session_name, forge_root=forge_root)
    except Exception:
        logger.debug(
            "Failed to read active-session registry for '%s'",
            session_name,
            exc_info=True,
        )
        return None


def _print_active_delete_warning(session_name: str, active_entry: ActiveSessionEntry) -> None:
    """Print a warning before deleting a session that still appears live."""
    console.print(
        "[yellow]Warning:[/yellow] "
        f"Session [bold]{session_name}[/bold] appears to still be active in a running Claude Code launch."
    )
    console.print("  Deleting it will remove Forge state while the Claude session keeps running until it exits.")
    console.print(f"  Launch mode: {active_entry.launch_mode}")
    if active_entry.launcher_pid is not None:
        console.print(f"  Launcher PID: {active_entry.launcher_pid}")
    if active_entry.container_name:
        console.print(f"  Container: {active_entry.container_name}")
    console.print()


def _resolve_launch_mode(*, sidecar: bool, host_proxy: bool) -> str:
    """Resolve host vs sidecar launch mode from CLI flags and runtime config."""
    if sidecar:
        return LAUNCH_MODE_SIDECAR
    if host_proxy:
        return LAUNCH_MODE_HOST

    from forge.runtime_config import get_runtime_config

    return LAUNCH_MODE_SIDECAR if get_runtime_config().proxy_mode == LAUNCH_MODE_SIDECAR else LAUNCH_MODE_HOST


def _get_runtime_base_url(*, use_sidecar: bool, effective_url: str | None) -> str | None:
    """Return the base URL Claude should see for this launch."""
    from forge.session import SIDECAR_RUNTIME_BASE_URL

    return SIDECAR_RUNTIME_BASE_URL if use_sidecar else effective_url


def _get_launch_preferences(
    state: SessionState,
) -> tuple[bool, tuple[str, ...], str | None]:
    """Return relaunch mode plus persisted sidecar options for a session."""
    launch = state.intent.launch
    if launch is None:
        return state.confirmed.is_sandboxed, (), None

    use_sidecar = launch.mode == LAUNCH_MODE_SIDECAR
    if not use_sidecar or launch.sidecar is None:
        return use_sidecar, (), None

    return use_sidecar, tuple(launch.sidecar.mounts), launch.sidecar.image


def _combine_prompt_files(*, worktree_path: Path, session_name: str, prompt_files: list[Path]) -> str | None:
    """Combine multiple prompt/context files into one appendable prompt file."""
    existing = [path.resolve() for path in prompt_files if path.is_file()]
    if not existing:
        return None
    if len(existing) == 1:
        return str(existing[0])

    launch_context_dir = worktree_path / ".forge" / "launch-context"
    launch_context_dir.mkdir(parents=True, exist_ok=True)
    combined_path = launch_context_dir / f"{session_name}.md"

    sections: list[str] = []
    for path in existing:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        if not content:
            continue
        sections.append(f"<!-- Source: {path.name} -->\n{content}")

    combined_path.write_text("\n\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return str(combined_path.resolve())


def _resolve_session_artifact_root(*, manager: SessionManager, state: SessionState) -> Path:
    """Return the root used for forge-root-relative artifacts for a session."""
    if state.forge_root:
        return Path(state.forge_root)

    worktree_path = Path(state.worktree.path) if state.worktree else Path.cwd()
    return Path(manager.resolve_project_root(worktree_path))


def _generate_parent_handoff_context(
    *,
    manager: SessionManager,
    manifest: SessionState,
    parent_state: SessionState | None = None,
    strategy: str = "structured",
    inline_plan: bool = False,
) -> tuple[Path | None, list[str]]:
    """Generate a fresh parent-context handoff file for a forked session."""
    if not manifest.is_fork or not manifest.parent_session:
        return None, []

    fork_worktree = Path(manifest.worktree.path) if manifest.worktree else Path.cwd()
    context_path = fork_worktree / ".forge" / "prev_sessions" / f"{manifest.parent_session}.md"

    if parent_state is None:
        parent_entry = None
        current_project_root = None
        if manifest.worktree:
            try:
                current_project_root = manager.resolve_project_root(Path(manifest.worktree.path))
            except Exception:
                current_project_root = None

        try:
            if current_project_root is not None:
                try:
                    siblings = [
                        entry
                        for name, entry in manager.list_sessions(
                            project_root_filter=current_project_root,
                            include_incognito=True,
                        )
                        if name == manifest.parent_session
                    ]
                except Exception:
                    siblings = []
                if len(siblings) == 1:
                    parent_entry = siblings[0]

            if parent_entry is None:
                parent_entry = manager.get_session_entry(manifest.parent_session)

            parent_scope = parent_entry.forge_root or parent_entry.worktree_path
            parent_state = manager.get_session(manifest.parent_session, forge_root=parent_scope)
        except ForgeSessionError:
            if context_path.is_file():
                return context_path.resolve(), []
            return None, []
        except Exception:
            if context_path.is_file():
                return context_path.resolve(), []
            return None, []

    parent_worktree = Path(parent_state.worktree.path) if parent_state.worktree else Path.cwd()

    project_root = _resolve_session_artifact_root(manager=manager, state=parent_state)

    from forge.session.handoff import ResumeStrategy, process_handoff

    try:
        resume_strategy = ResumeStrategy(strategy)
    except ValueError:
        resume_strategy = ResumeStrategy.STRUCTURED

    _parent_fr = parent_state.forge_root

    def _get_session_safe(session_name: str) -> SessionState | None:
        try:
            return manager.get_session(session_name, forge_root=_parent_fr)
        except ForgeSessionError:
            return None

    handoff_result = process_handoff(
        parent_name=manifest.parent_session,
        parent_state=parent_state,
        forge_root=project_root,
        parent_worktree_root=parent_worktree,
        output_root=fork_worktree if fork_worktree != parent_worktree else None,
        strategy=resume_strategy,
        depth=1,
        get_session=_get_session_safe,
        inline_plan=inline_plan,
    )
    if handoff_result.context_file is None:
        return None, handoff_result.warnings
    return handoff_result.context_file.resolve(), handoff_result.warnings


def _handle_error(e: ForgeSessionError) -> None:
    """Handle a ForgeSessionError and exit."""
    console.print(f"[red]Error:[/red] {e}", style="red")
    sys.exit(1)


def _hint_cross_project_session(name: str, forge_root: str | None) -> bool:
    """Print a hint if a session exists in another forge_root.

    Handles both unique and ambiguous (duplicate-name) cases.
    Returns True if a cross-project hint was printed, False otherwise.
    """
    from rich.text import Text

    from forge.session import IndexStore

    if not forge_root:
        return False
    try:
        entry = IndexStore().get_session(name, forge_root=None)
        other_root = entry.forge_root or entry.worktree_path
        if other_root and other_root != forge_root:
            console.print(f"[red]Error:[/red] session '{name}' not found in current project")
            console.print(f"\n[dim]Tip: Session '{name}' exists in:[/dim]")
            console.print(
                Text(display_path(other_root), style="dim", no_wrap=True),
                soft_wrap=True,
            )
            console.print("[dim]Run the command from that directory instead.[/dim]")
            return True
    except AmbiguousSessionError as e:
        console.print(f"[red]Error:[/red] session '{name}' not found in current project")
        console.print(f"\n[dim]Tip: Session '{name}' exists in multiple projects:[/dim]")
        for root in e.forge_roots:
            console.print(
                Text(f"  - {display_path(root)}", style="dim", no_wrap=True),
                soft_wrap=True,
            )
        console.print("[dim]Run the command from the target project directory.[/dim]")
        return True
    except (SessionNotFoundError, OSError):
        # SessionNotFoundError: not in any project. OSError: index file unreadable.
        pass
    return False


# --- Click group ---


@click.group()
def session() -> None:
    """Manage Claude Code sessions.

    \b
    Examples:
        forge session start my-feature         # Create a new session
        forge session resume my-feature        # Resume existing session
        forge session list                     # List all sessions
    """
    pass


# sys is imported by _handle_error above; keep it available for the re-exported modules
import sys  # noqa: E402

# Re-export names that tests patch on forge.cli.session (originally top-level imports).
# These must be in this module's namespace for patch("forge.cli.session.XXX") to work.
from forge.core.naming import generate_unique_name as generate_unique_name  # noqa: E402,F401
from forge.session import run_with_active_session as run_with_active_session  # noqa: E402,F401
from forge.session.claude import invoke_claude as invoke_claude  # noqa: E402,F401

# Re-export for backward compatibility (204 test references patch "forge.cli.session.XXX")
from .session_fork import *  # noqa: E402,F401,F403
from .session_lifecycle import *  # noqa: E402,F401,F403
from .session_manage import *  # noqa: E402,F401,F403
