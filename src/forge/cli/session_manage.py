"""Session management commands: delete, list, clean, show, context, shell, set, reset.

Split from session.py for file-size compliance. All public and private
names are re-exported by session.py so that ``patch("forge.cli.session.XXX")``
continues to work.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import click
from rich.table import Table

from forge.core.ops.session_context import SessionContext
from forge.core.paths import display_path
from forge.core.state import parse_iso
from forge.session import (
    ForgeSessionError,
    IndexStore,
    SessionIndexEntry,
    SessionManager,
    SessionState,
)


def _sess():  # type: ignore[return]
    """Access forge.cli.session at runtime to respect test patches."""
    return sys.modules["forge.cli.session"]


from forge.cli.session import (  # noqa: E402
    _cwd_forge_root,
    _format_relative_time,
    _get_active_session_entry,
    _get_session_type,
    _handle_error,
    _hint_cross_project_session,
    _print_active_delete_warning,
    _session_list_location,
    _session_scope_key,
    _template_display_label,
    console,
    logger,
)
from forge.cli.session import session as _session_untyped  # noqa: E402

session = cast(click.Group, _session_untyped)  # type: ignore[has-type]  # circular re-export

from forge.session.exceptions import (  # noqa: E402
    AmbiguousSessionError,
    DirtyWorktreeError,
    SessionNotFoundError,
)
from forge.session.plan_resolution import (  # noqa: E402
    PlanInfo,
    latest_snapshot_path,
    preferred_plan_path,
    resolve_displayed_plan_path,
    resolve_path_against,
    resolve_plan_info,
    resolve_plan_launch_root,
)

__all__ = [
    # Click commands
    "delete",
    "list_sessions",
    "clean",
    "show",
    "context_cmd",
    "shell",
    "set_override",
    "reset",
    # Private helpers (needed for re-export to forge.cli.session namespace)
    "_delete_single_session",
    "_print_session_list_tips",
    "_clean_sessions_dry_run",
    "_build_show_json",
    "_empty_show_plan_json",
    "_build_show_plan_json",
    "_print_session_context",
    "_print_session_summary",
    "_print_plan_info",
    "_print_session_detail",
    "_flatten_overrides",
    "_format_value",
]


@session.command()
@click.argument("names", nargs=-1)
@click.option("--all", "-a", "delete_all", is_flag=True, help="Delete all sessions")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--force", "-f", is_flag=True, help="Override dirty-worktree and corruption guards")
@click.option("--keep-transcripts", "-k", is_flag=True, help="Keep transcript files")
@click.option("--keep-worktree", "-K", is_flag=True, help="Preserve worktree directory")
@click.option("--delete-branch", "-d", is_flag=True, help="Also delete git branch")
def delete(
    names: tuple[str, ...],
    delete_all: bool,
    yes: bool,
    force: bool,
    keep_transcripts: bool,
    keep_worktree: bool,
    delete_branch: bool,
) -> None:
    """Delete one or more sessions and their data.

    \b
    Examples:
      forge session delete my-session
      forge session delete my-session --yes             # Skip confirmation
      forge session delete my-session --yes --force     # Skip confirmation + override dirty worktree
      forge session delete --all --yes

    By default, removes the worktree directory but keeps the git branch.
    Use --delete-branch to also delete the branch.
    Use --keep-worktree to preserve the worktree directory.
    """
    if delete_all and names:
        console.print("[red]Error:[/red] Cannot combine --all with explicit session names")
        sys.exit(1)

    if not delete_all and not names:
        console.print("[red]Error:[/red] Provide session name(s) or use --all")
        sys.exit(1)

    manager = _sess().SessionManager()
    _fr = _cwd_forge_root()

    if delete_all:
        if _fr is None:
            console.print("[red]Error:[/red] --all requires being inside a Forge project (directory with .forge/)")
            console.print("[dim]Tip: Use explicit session names instead, or cd into a Forge project.[/dim]")
            sys.exit(1)
        all_sessions = manager.list_sessions(include_incognito=True, forge_root_filter=_fr)
        if not all_sessions:
            console.print("[dim]No sessions to delete.[/dim]")
            return
        targets = [name for name, _ in all_sessions]

        active_targets = [
            (target, active_entry)
            for target in targets
            if (active_entry := _get_active_session_entry(target, forge_root=_fr)) is not None
        ]
        console.print(f"About to delete [bold]all {len(targets)} session(s)[/bold]:")
        for t in targets:
            console.print(f"  - {t}")
        if active_targets:
            console.print()
            console.print(
                "[yellow]Warning:[/yellow] "
                "The following sessions appear to still be active in running Claude Code launches:"
            )
            for target, active_entry in active_targets:
                details = [active_entry.launch_mode]
                if active_entry.container_name:
                    details.append(active_entry.container_name)
                elif active_entry.launcher_pid is not None:
                    details.append(f"pid {active_entry.launcher_pid}")
                console.print(f"  - {target} ({', '.join(details)})")
            console.print(
                "  Deleting them will remove Forge state while Claude keeps running until those launches exit."
            )
        console.print()
        if not yes:
            if not click.confirm("Are you sure you want to delete all sessions?"):
                console.print("[dim]Cancelled[/dim]")
                sys.exit(0)
    else:
        targets = list(dict.fromkeys(names))

    deleted = 0
    failed = 0

    for name in targets:
        # Resolve across forge_roots within the repo (named deletes only)
        actual_fr = _fr
        if not delete_all:
            try:
                from forge.core.ops.resolution import resolve_session_repo_wide

                resolved = resolve_session_repo_wide(name, _fr, manager=manager)
                actual_fr = resolved.forge_root
                if resolved.is_cross_project:
                    console.print(f"[dim]Deleting session from {display_path(actual_fr)}[/dim]")
            except AmbiguousSessionError as e:
                console.print(f"[red]Error:[/red] {e}")
                failed += 1
                continue
            except SessionNotFoundError:
                pass  # Fall through to _delete_single_session for orphan handling
            except ForgeSessionError:
                # Manifest corrupt but session exists in index -- resolve the
                # forge_root from the index so force-delete can clean it up.
                try:
                    entry = IndexStore().get_session(name, forge_root=None)
                    idx_fr = entry.root
                    if idx_fr:
                        actual_fr = idx_fr
                except (SessionNotFoundError, AmbiguousSessionError, ForgeSessionError):
                    pass

        try:
            _sess()._delete_single_session(
                manager=manager,
                name=name,
                yes=yes or delete_all,
                force=force,
                keep_transcripts=keep_transcripts,
                keep_worktree=keep_worktree,
                delete_branch=delete_branch,
                forge_root=actual_fr,
            )
            console.print(f"Deleted session [green]{name}[/green]")
            deleted += 1
        except SystemExit as e:
            if len(targets) == 1:
                raise
            if e.code not in (0, None):
                failed += 1
        except DirtyWorktreeError as e:
            if len(targets) == 1:
                console.print(f"[red]Error:[/red] {e}")
                console.print("\n[dim]Tip: Use --force to remove anyway, or commit/stash your changes first.[/dim]")
                raise SystemExit(1)
            console.print(f"[red]Error:[/red] {name}: {e}")
            failed += 1
        except ForgeSessionError as e:
            if len(targets) == 1:
                _handle_error(e)
            else:
                console.print(f"[red]Error:[/red] {name}: {e}")
                failed += 1
        except Exception as e:
            console.print(f"[red]Error:[/red] {name}: {e}")
            failed += 1

    if len(targets) > 1:
        parts = [f"{deleted} deleted"]
        if failed:
            parts.append(f"{failed} failed")
        console.print(f"\n[dim]Summary: {', '.join(parts)}[/dim]")

    if failed:
        sys.exit(1)


def _delete_single_session(
    *,
    manager: SessionManager,
    name: str,
    yes: bool,
    force: bool,
    keep_transcripts: bool,
    keep_worktree: bool,
    delete_branch: bool,
    forge_root: str | None = None,
) -> None:
    """Delete a single session, handling orphans and confirmation.

    Args:
        yes: Skip confirmation prompts (informational output stays visible).
        force: Override dirty-worktree and corruption guards.

    Raises:
        SystemExit: If user cancels or session not found.
        DirtyWorktreeError: If worktree has uncommitted changes and not force.
        ForgeSessionError: On other session errors.
    """
    if not manager.session_exists(name, forge_root=forge_root):
        from forge.session.store import SessionStore

        orphan_store = SessionStore(str(Path.cwd()), name)
        if orphan_store.session_dir.is_dir():
            import shutil

            console.print(
                f"Found orphaned session directory [bold]{name}[/bold] " "(exists on disk but not in session index)"
            )
            console.print(f"  Path: {display_path(orphan_store.session_dir)}")
            if not yes:
                if not click.confirm("Delete this orphaned session directory?"):
                    console.print("[dim]Cancelled[/dim]")
                    raise SystemExit(0)

            shutil.rmtree(orphan_store.session_dir)
            try:
                from forge.session.active import ActiveSessionStore

                ActiveSessionStore().clear_session(name, forge_root=forge_root)
            except Exception:
                logger.debug(
                    "Failed to clear active-session entry for orphan '%s'",
                    name,
                    exc_info=True,
                )
            console.print(f"Cleaned up orphaned session directory [green]{name}[/green]")
            return
        console.print(f"[red]Error:[/red] session '{name}' not found")
        raise SystemExit(1)

    # Informational output -- always visible (--yes only skips prompts, not info)
    active_entry = _get_active_session_entry(name, forge_root=forge_root)
    if active_entry is not None:
        _print_active_delete_warning(name, active_entry)
    try:
        manifest = manager.get_session(name, forge_root=forge_root)

        console.print(f"About to delete session [bold]{name}[/bold]")

        if manifest.confirmed.claude_session_id:
            console.print(f"  UUID: {manifest.confirmed.claude_session_id}")

        if manifest.worktree and manifest.worktree.is_worktree:
            if keep_worktree:
                console.print(f"  [dim]Worktree will be kept: {display_path(manifest.worktree.path)}[/dim]")
            else:
                console.print(f"  Worktree will be removed: {display_path(manifest.worktree.path)}")
            if delete_branch:
                console.print(f"  Branch will be deleted: {manifest.worktree.branch}")
            else:
                console.print(f"  [dim]Branch will be kept: {manifest.worktree.branch}[/dim]")

        if not keep_transcripts:
            console.print("  [dim]Transcript files will also be deleted[/dim]")
        else:
            console.print("  [dim]Transcript files will be kept[/dim]")

        console.print()
    except ForgeSessionError:
        pass

    if not yes:
        if not click.confirm("Are you sure you want to delete this session?"):
            console.print("[dim]Cancelled[/dim]")
            raise SystemExit(0)

    manager.delete_session(
        name,
        delete_transcripts=not keep_transcripts,
        delete_worktree=not keep_worktree,
        delete_branch=delete_branch,
        force=force,
        forge_root=forge_root,
    )


@session.command("list")
@click.option(
    "--include-incognito/--no-incognito",
    "-i/-I",
    default=True,
    help="Include incognito sessions",
)
@click.option(
    "--older-than",
    type=int,
    default=None,
    metavar="DAYS",
    help="Only show sessions not accessed in DAYS days",
)
@click.option(
    "--scope",
    type=click.Choice(["repo", "project", "all"], case_sensitive=False),
    default="repo",
    help="Scope: repo (default, same logical repo), project (same forge_root), all (global)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_sessions(include_incognito: bool, older_than: int | None, scope: str, as_json: bool) -> None:
    """List sessions.

    \b
    Examples:
        forge session list                  # Sessions in current repo
        forge session list --scope all      # All sessions globally
        forge session list --older-than 30  # Old sessions in current repo
    """
    if older_than is not None and older_than < 1:
        console.print("[red]Error:[/red] --older-than must be >= 1")
        sys.exit(1)

    from forge.core.ops.context import ExecutionContext
    from forge.core.ops.session import ForgeOpError
    from forge.core.ops.session import list_sessions as list_sessions_op

    ctx = ExecutionContext.from_cwd()

    if older_than is not None:
        from forge.core.ops.session import _scope_filters, list_sessions_older_than

        pr_filter, fr_filter = _scope_filters(ctx, scope)
        old_sessions = list_sessions_older_than(
            older_than_days=older_than,
            include_incognito=include_incognito,
            project_root_filter=pr_filter,
            forge_root_filter=fr_filter,
        )
        old_scope_keys = {_session_scope_key(name, entry) for name, entry in old_sessions}
    else:
        old_scope_keys = None

    try:
        result = list_sessions_op(ctx=ctx, include_incognito=include_incognito, scope=scope)
    except ForgeOpError as e:
        if as_json:
            import json

            click.echo(json.dumps({"error": str(e)}, indent=2), err=True)
        else:
            console.print(f"[red]Error:[/red] {e}", style="red")
        sys.exit(1)

    items = result.sessions
    if old_scope_keys is not None:
        items = [item for item in items if _session_scope_key(item.name, item.entry) in old_scope_keys]

    if as_json:
        import json

        data = []
        for item in items:
            data.append(
                {
                    "name": item.name,
                    "proxy_template": item.proxy_template,
                    "last_accessed_at": item.entry.last_accessed_at,
                    "is_active": item.is_active,
                    "worktree_path": item.entry.worktree_path,
                    "forge_root": item.entry.forge_root,
                    "checkout_root": item.entry.checkout_root,
                    "relative_path": item.entry.relative_path,
                    "is_fork": item.entry.is_fork,
                    "is_incognito": item.entry.is_incognito,
                    "parent_session": item.entry.parent_session,
                }
            )
        click.echo(json.dumps(data, indent=2, default=str))
        return

    if not items:
        if older_than is not None:
            console.print(f"[dim]No sessions older than {older_than} days.[/dim]")
        else:
            console.print("[dim]No sessions found.[/dim]")
            console.print("\n[dim]Tip: Run 'forge session start <name>'.[/dim]")
        return

    duplicate_names = {item.name for item in items if sum(1 for other in items if other.name == item.name) > 1}

    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    if duplicate_names:
        table.add_column("LOCATION")
    table.add_column("TEMPLATE")
    table.add_column("LAST USED")

    for item in items:
        entry = item.entry
        proxy_template = item.proxy_template or "direct"
        last_used = _format_relative_time(entry.last_accessed_at)
        row = [item.name]
        if duplicate_names:
            row.append(_session_list_location(entry))
        row.extend([proxy_template, last_used])
        table.add_row(*row)

    console.print(table)

    if older_than is None:
        _print_session_list_tips(items)


def _print_session_list_tips(items: list) -> None:
    """Print contextual tips after session list output."""
    count = len(items)

    if count == 1:
        name = items[0].name if hasattr(items[0], "name") else "name"
        console.print("\n[dim]Tip: Resume or start a session:[/dim]")
        console.print(f"[dim]  forge session resume {name}                  # resume this session[/dim]")
        console.print("[dim]  forge session start <name>                    # start a new session[/dim]")
    elif count > 0:
        console.print("\n[dim]Tip: Work with sessions:[/dim]")
        console.print("[dim]  forge session resume <name>                   # resume a session[/dim]")
        console.print("[dim]  forge session show <name>                     # inspect session details[/dim]")

    console.print("\n[dim]Tip: Clean up sessions:[/dim]")
    console.print("[dim]  forge session delete <name>                   # delete a specific session[/dim]")
    console.print("[dim]  forge session clean --older-than 30           # bulk clean old sessions[/dim]")
    console.print("[dim]  forge config set session_retention_days=90    # auto-cleanup on startup[/dim]")


@session.command("clean")
@click.option(
    "--older-than",
    type=int,
    required=True,
    metavar="DAYS",
    help="Delete sessions not accessed in DAYS days",
)
@click.option("--dry-run", "-n", is_flag=True, help="Show what would be deleted without deleting")
@click.option("--force", "-f", is_flag=True, help="Bypass dirty-worktree protection")
@click.option(
    "--keep-transcripts",
    "-k",
    is_flag=True,
    help="Keep Claude transcript files (~/.claude/projects/*.jsonl). Forge artifact snapshots (.forge/artifacts/) are always preserved",
)
@click.option(
    "--delete-worktree",
    is_flag=True,
    help="Also remove worktree directories (default: keep)",
)
@click.option(
    "--delete-branch",
    "-d",
    is_flag=True,
    help="Also delete git branches (requires --delete-worktree)",
)
def clean(
    older_than: int,
    dry_run: bool,
    force: bool,
    keep_transcripts: bool,
    delete_worktree: bool,
    delete_branch: bool,
) -> None:
    """Delete sessions older than a given age.

    \b
    Examples:
        forge session clean --older-than 30          # Delete sessions > 30 days old
        forge session clean --older-than 30 --dry-run # Preview what would be cleaned
        forge session clean --older-than 90 -k       # Keep transcript files

    Active sessions are always skipped. Worktrees are preserved by default
    (use --delete-worktree to remove them).
    """
    if older_than < 1:
        console.print("[red]Error:[/red] --older-than must be >= 1")
        sys.exit(1)

    if delete_branch and not delete_worktree:
        console.print("[red]Error:[/red] --delete-branch requires --delete-worktree")
        sys.exit(1)

    if dry_run:
        _clean_sessions_dry_run(older_than)
        return

    from forge.session.cleanup import clean_old_sessions

    result = clean_old_sessions(
        older_than_days=older_than,
        delete_transcripts=not keep_transcripts,
        delete_worktree=delete_worktree,
        delete_branch=delete_branch,
        force=force,
    )

    if result.is_empty:
        console.print(f"[dim]No sessions older than {older_than} days found.[/dim]")
        return

    if result.aborted:
        console.print("[red]Error:[/red] Session cleanup aborted before evaluation completed.")
        console.print(f"  [dim]{result.aborted_error}[/dim]")
    elif result.has_only_skips:
        console.print("[dim]No sessions cleaned.[/dim]")

    if result.deleted:
        console.print(
            f"Cleaned {len(result.deleted)} session{'s' if len(result.deleted) != 1 else ''}"
            f" older than {older_than} days."
        )
    elif not result.aborted:
        console.print("[dim]No sessions cleaned.[/dim]")

    if result.skipped_active:
        console.print(
            f"[dim]Kept {len(result.skipped_active)} active session{'s' if len(result.skipped_active) != 1 else ''}.[/dim]"
        )

    if result.skipped_unparseable:
        console.print(
            f"[dim]Skipped {len(result.skipped_unparseable)} session{'s' if len(result.skipped_unparseable) != 1 else ''}"
            f" with unparseable timestamps.[/dim]"
        )

    if result.should_exit_nonzero:
        console.print(
            f"[yellow]Encountered {result.summary_failed_count} cleanup {result.summary_failed_label}.[/yellow]"
        )
        for name, err in result.failure_items():
            console.print(f"  [dim]{name}: {err}[/dim]")
        sys.exit(1)


def _clean_sessions_dry_run(older_than_days: int) -> None:
    """Preview which sessions would be cleaned.

    Iterates all sessions directly (same path as clean_old_sessions) so that
    unparseable timestamps and active-registry errors are visible in the preview.
    """
    from forge.session.active import ActiveSessionStore

    manager = _sess().SessionManager()
    all_sessions = manager.list_sessions(include_incognito=True)

    # One-pass active lookup -- fail-closed matches cleanup behavior
    active_store = ActiveSessionStore()
    registry_error = False
    try:
        active_entries = active_store.list_sessions()
        active_identities = {(name, ae.forge_root or ae.worktree_path) for name, ae in active_entries}
    except Exception:
        active_identities = set()
        registry_error = True

    table = Table(show_header=True, header_style="bold")
    table.add_column("SESSION")
    table.add_column("AGE")
    table.add_column("STATUS")

    deletable = 0
    skipped = 0
    any_old = False
    for name, entry in all_sessions:
        try:
            dt = parse_iso(entry.last_accessed_at)
            age_days = int((datetime.now(UTC) - dt).total_seconds() / 86400)
        except (ValueError, TypeError, AttributeError):
            table.add_row(name, "?", "[dim]unparseable timestamp (skip)[/dim]")
            skipped += 1
            any_old = True
            continue

        if age_days <= older_than_days:
            continue

        any_old = True
        age_str = f"{age_days}d"
        if (name, entry.forge_root or entry.worktree_path) in active_identities:
            table.add_row(name, age_str, "[yellow]active (skip)[/yellow]")
            skipped += 1
        else:
            table.add_row(name, age_str, "[green]will delete[/green]")
            deletable += 1

    if not any_old:
        console.print(f"[dim]No sessions older than {older_than_days} days found.[/dim]")
        return

    console.print(table)

    if registry_error:
        console.print(
            "[yellow]Warning:[/yellow] Could not read active session registry."
            " Actual cleanup would abort to protect running sessions."
        )

    console.print(
        f"\n[dim]Would delete {deletable} session{'s' if deletable != 1 else ''}"
        + (f", skip {skipped}" if skipped else "")
        + ".[/dim]"
    )


@session.command()
@click.argument("session_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--field",
    "field_path",
    help="Extract a single dotted field (e.g., model_family, proxy.template). Missing path exits 1; null value prints empty.",
)
def show(session_id: str | None, as_json: bool, field_path: str | None) -> None:
    """Show session details.

    SESSION_ID can be a Forge session name or a Claude session UUID.
    Without SESSION_ID, resolves from $FORGE_SESSION.

    \b
    Examples:
        forge session show my-session                     # Full details
        forge session show                                # Current session
        forge session show my-session --json              # JSON output
        forge session show my-session --field model_family  # Extract field
    """
    import json

    from forge.core.ops.session_context import (
        SessionContextError,
        extract_field,
        get_session_context,
    )

    # When no argument and no env var: for human mode, show a helpful message.
    # For --json/--field, fall through to get_session_context() which builds
    # env-derived context (backward compat with old `session context --json`).
    if session_id is None and not os.environ.get("FORGE_SESSION") and not (as_json or field_path):
        console.print("[dim]No session specified. Use a name or launch through Forge.[/dim]")
        return

    try:
        ctx = get_session_context(session_id)
    except AmbiguousSessionError as e:
        if as_json:
            click.echo(json.dumps({"error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except SessionContextError as e:
        if as_json:
            click.echo(json.dumps({"error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # Resolve the forge_root once -- either from get_session_context's prior
    # UUID/name lookup (preserves exact scope for UUIDs) or via the two-tier
    # repo-wide resolver as fallback.
    from forge.core.ops.resolution import resolve_session_repo_wide
    from forge.core.ops.session_context import resolve_session_identifier

    manager = _sess().SessionManager()
    _fr = _cwd_forge_root()

    # get_session_context already resolved the identifier (UUID or name) to
    # an exact (name, forge_root). Reuse that forge_root so UUID lookups
    # don't get re-resolved by name (which could pick the wrong duplicate).
    resolved_fr: str | None = None
    try:
        _, id_forge_root = resolve_session_identifier(session_id)
        resolved_fr = id_forge_root
    except Exception:
        pass

    def _load_state_and_entry() -> tuple[SessionState | None, SessionIndexEntry | None, bool]:
        """Load manifest + entry, returning (state, entry, is_cross_project)."""
        if resolved_fr is not None:
            try:
                st = manager.get_session(ctx.session_name, forge_root=resolved_fr)
                ent = manager.get_session_entry(ctx.session_name, forge_root=resolved_fr)
                is_cross = resolved_fr != _fr if _fr else False
                return st, ent, is_cross
            except ForgeSessionError:
                pass
        # Fallback: two-tier repo-wide resolution
        try:
            res = resolve_session_repo_wide(ctx.session_name, _fr, manager=manager)
            return res.state, res.entry, res.is_cross_project
        except (SessionNotFoundError, AmbiguousSessionError, ForgeSessionError):
            return None, None, False

    if as_json or field_path:
        state, _, _ = _load_state_and_entry()
        data = _build_show_json(state, ctx)

        if field_path:
            try:
                value = extract_field(data, field_path)
            except KeyError:
                console.print(f"[red]Error:[/red] Field '{field_path}' not found")
                sys.exit(1)
            if value is None:
                click.echo("")
            elif isinstance(value, str):
                click.echo(value)
            else:
                click.echo(json.dumps(value))
            return

        click.echo(json.dumps(data, indent=2, default=str))
        return

    state, entry, is_cross_project = _load_state_and_entry()
    if state is None or entry is None:
        console.print(f"[red]Error:[/red] session '{ctx.session_name}' not found")
        sys.exit(1)

    if is_cross_project:
        console.print(f"[dim]Showing session from {display_path(resolved_fr or '')}[/dim]\n")

    _print_session_detail(state, entry, ctx)


@session.command("context", hidden=True)
@click.argument("session_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--field",
    "field_path",
    help="Extract a single dotted field (e.g., model_family, proxy.template). Missing path exits 1; null value prints empty.",
)
def context_cmd(session_id: str | None, as_json: bool, field_path: str | None) -> None:
    """Show session context (metadata, proxy, model family).

    Deprecated: use ``forge session show`` instead.

    SESSION_ID can be a Forge session name or a Claude session UUID.
    Without SESSION_ID, resolves from $FORGE_SESSION.

    \b
    Examples:
        forge session context                        # current session
        forge session context --json                 # full JSON
        forge session context --field model_family   # just the family
        forge session context abc-123-uuid --json    # by Claude UUID
    """
    import json

    from forge.core.ops.session_context import (
        SessionContextError,
        extract_field,
        get_session_context,
    )

    try:
        ctx = get_session_context(session_id)
    except SessionContextError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None

    data = ctx.to_dict()

    if field_path:
        try:
            value = extract_field(data, field_path)
        except KeyError:
            console.print(f"[red]Error:[/red] Field '{field_path}' not found")
            raise SystemExit(1) from None
        # Raw value output for scripting -- no JSON wrapper, no quotes for strings.
        # None prints empty (jq -r convention) so callers can tell "field exists but unset".
        if value is None:
            click.echo("")
        elif isinstance(value, str):
            click.echo(value)
        else:
            click.echo(json.dumps(value))
        return

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    _print_session_context(ctx)


def _build_show_json(
    state: SessionState | None,
    ctx: SessionContext,
) -> dict[str, Any]:
    """Build merged JSON for ``session show --json``.

    Manifest data at the top level, computed context nested under ``context``.
    """
    data: dict[str, Any] = {
        "session_name": ctx.session_name,
        "claude_session_id": ctx.claude_session_id,
        "created_at": ctx.created_at,
        "is_fork": ctx.is_fork,
        "is_incognito": ctx.is_incognito,
        "parent_session": ctx.parent_session,
    }

    if state:
        data["last_accessed_at"] = state.last_accessed_at
        data["intent"] = {
            "agent": state.intent.agent,
            "proxy": (
                {
                    "template": state.intent.proxy.template,
                    "base_url": state.intent.proxy.base_url,
                }
                if state.intent.proxy
                else None
            ),
        }
        data["confirmed"] = {
            "claude_session_id": state.confirmed.claude_session_id,
            "transcript_path": state.confirmed.transcript_path,
            "confirmed_at": state.confirmed.confirmed_at,
            "confirmed_by": state.confirmed.confirmed_by,
            "latest_plan_path": state.confirmed.latest_plan_path,
            "artifacts": dict(state.confirmed.artifacts),
            "derivation": (dataclasses.asdict(state.confirmed.derivation) if state.confirmed.derivation else None),
            "is_sandboxed": state.confirmed.is_sandboxed,
            "claude_project_root": state.confirmed.claude_project_root,
            "policy": (dataclasses.asdict(state.confirmed.policy) if state.confirmed.policy else None),
        }
        data["overrides"] = dict(state.overrides)
        data["worktree"] = {"path": state.worktree.path, "branch": state.worktree.branch} if state.worktree else None
    else:
        data["last_accessed_at"] = None
        data["intent"] = None
        data["confirmed"] = None
        data["overrides"] = {}
        data["worktree"] = {"path": ctx.worktree_path} if ctx.worktree_path else None

    data["plan"] = _build_show_plan_json(state)
    data["project_root"] = ctx.project_root

    data["context"] = {
        "model_family": ctx.model_family,
        "models": dict(ctx.models),
        "proxy": {
            "template": ctx.proxy.template,
            "base_url": ctx.proxy.base_url,
            "proxy_id": ctx.proxy.proxy_id,
            "is_direct": ctx.proxy.is_direct,
        },
        "policy": {
            "enabled": ctx.policy.enabled,
            "fail_mode": ctx.policy.fail_mode,
            "bundles": list(ctx.policy.bundles),
            "supervisor_resume_id": ctx.policy.supervisor_resume_id,
        },
    }

    # Top-level aliases for backward compat with old `session context --field`
    data["model_family"] = ctx.model_family
    data["models"] = dict(ctx.models)
    data["proxy"] = data["context"]["proxy"]
    data["policy"] = data["context"]["policy"]

    return data


def _empty_show_plan_json() -> dict[str, Any]:
    """Return the resolved plan shape used by `session show --json`."""
    return {
        "source": None,
        "parent_session": None,
        "draft_path": None,
        "approved_snapshots": [],
        "preferred_path": None,
        "display_path": None,
        "exists": None,
        "kind": None,
    }


def _build_show_plan_json(state: SessionState | None) -> dict[str, Any]:
    """Build the resolved/inherited plan view for machine-readable output."""
    if state is None:
        return _empty_show_plan_json()

    current_forge_root = state.forge_root or (state.worktree.path if state.worktree else None)
    if current_forge_root is None:
        return _empty_show_plan_json()

    plan_info = resolve_plan_info(state, current_forge_root=current_forge_root)
    displayed = resolve_displayed_plan_path(
        plan_info,
        current_forge_root=current_forge_root,
        current_launch_root=resolve_plan_launch_root(state),
    )

    if plan_info.approved_snapshots:
        kind = "approved"
    elif plan_info.draft_path:
        kind = "draft"
    else:
        kind = None

    return {
        "source": plan_info.source,
        "parent_session": plan_info.parent_session,
        "draft_path": plan_info.draft_path,
        "approved_snapshots": list(plan_info.approved_snapshots),
        "preferred_path": preferred_plan_path(plan_info),
        "display_path": displayed.path if displayed else None,
        "exists": displayed.exists if displayed else None,
        "kind": kind,
    }


def _print_session_context(ctx: SessionContext) -> None:
    """Print session context in human-readable format."""

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Session", ctx.session_name)
    if ctx.claude_session_id:
        table.add_row("Claude UUID", ctx.claude_session_id)
    table.add_row("Model Family", f"[cyan]{ctx.model_family}[/cyan]")

    if ctx.proxy.is_direct:
        table.add_row("Proxy", "[dim]direct (no proxy)[/dim]")
    else:
        proxy_parts = []
        if ctx.proxy.template:
            proxy_parts.append(ctx.proxy.template)
        if ctx.proxy.base_url:
            proxy_parts.append(ctx.proxy.base_url)
        table.add_row("Proxy", " | ".join(proxy_parts))

    if ctx.models:
        model_str = ", ".join(f"{t}={m}" for t, m in ctx.models.items())
        table.add_row("Models", model_str)

    if ctx.worktree_path:
        table.add_row("Worktree", ctx.worktree_path)

    if ctx.parent_session:
        table.add_row("Parent", ctx.parent_session)

    if ctx.is_fork:
        table.add_row("Fork", "yes")

    if ctx.policy.enabled:
        table.add_row("Policy", f"enabled (bundles: {', '.join(ctx.policy.bundles) or 'none'})")

    console.print(table)


@session.command()
@click.argument("name", required=False)
def shell(name: str | None) -> None:
    """Open a shell in a sidecar session container.

    Without NAME, resolves from $FORGE_SESSION.
    Only works for sessions started with --sidecar.
    """
    from forge.sidecar import exec_in_container, is_container_running

    manager = _sess().SessionManager()

    if name is None:
        env_name = os.environ.get("FORGE_SESSION")
        if env_name:
            name = env_name
        else:
            console.print("[red]Error:[/red] No session specified. Use a name or launch through Forge.")
            console.print("\n[dim]Tip: Run 'forge session start <name> --sidecar'.[/dim]")
            sys.exit(1)

    _fr = _cwd_forge_root()
    if not manager.session_exists(name, forge_root=_fr):
        if not _hint_cross_project_session(name, _fr):
            console.print(f"[red]Error:[/red] Session '{name}' not found")
        sys.exit(1)

    try:
        manifest = manager.get_session(name, forge_root=_fr)
    except ForgeSessionError as e:
        _handle_error(e)
        return

    if not manifest.confirmed.is_sandboxed:
        console.print(f"[red]Error:[/red] Session '{name}' is not a sidecar session")
        console.print("\nOnly sessions started with --sidecar can use shell.")
        console.print("Start a sidecar session with: [cyan]forge session start <name> --sidecar[/cyan]")
        sys.exit(1)

    # Check if container is running (deterministic naming)
    container_name = f"forge-{name}"
    if not is_container_running(container_name):
        console.print(f"[red]Error:[/red] Container '{container_name}' is not running")
        console.print("\nThe sidecar session may have exited.")
        sys.exit(1)

    console.print(f"Opening shell in container [cyan]{container_name}[/cyan]...")
    exit_code = exec_in_container(container_name, ["/bin/bash"])
    sys.exit(exit_code)


@session.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--session", "-s", "session_name", help="Target session (default: current from cwd)")
def set_override(key: str, value: str, session_name: str | None) -> None:
    """Set a mid-session override.

    KEY is a dot-notation path relative to intent (e.g., agent, proxy.template).
    VALUE is parsed as JSON first, then as string.

    \b
    Examples:
        forge session set agent custom-agent
        forge session set memory.tags '["tag1","tag2"]'
        forge session set proxy.* null  # Clear all proxy fields
    """
    from forge.core.ops.context import ExecutionContext
    from forge.core.ops.session import ForgeOpError
    from forge.core.ops.session import set_session_override as set_override_op

    try:
        ctx = ExecutionContext.from_cwd()
        result = set_override_op(ctx=ctx, session_name=session_name, key=key, value_str=value)
        display_value = _format_value(result.value)
        console.print(f"Set [cyan]{result.key}[/cyan] = {display_value} [dim](override)[/dim]")

        if key.startswith("verification"):
            from forge.install.hooks import has_forge_hook

            if not has_forge_hook(ctx.worktree_root, "Stop"):
                console.print(
                    "[yellow]Warning:[/yellow] Verification configured but Stop hook is not installed. "
                    "Enforcement will not be active."
                )
                console.print("[dim]Tip: Run 'forge extension enable' to install hooks.[/dim]")
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@session.command()
@click.argument("key", required=False)
@click.option("--all", "-a", "clear_all", is_flag=True, help="Clear all overrides")
@click.option("--session", "-s", "session_name", help="Target session (default: current from cwd)")
def reset(key: str | None, clear_all: bool, session_name: str | None) -> None:
    """Reset overrides, reverting to intent values.

    If KEY is provided, resets only that key.
    If --all or no key, clears all overrides.

    Examples:

        forge session reset agent          # Reset single key

        forge session reset               # Clear all overrides

        forge session reset --all         # Clear all overrides (explicit)

        forge session reset memory.*      # Reset all memory.* overrides
    """
    from forge.core.ops.context import ExecutionContext
    from forge.core.ops.session import ForgeOpError
    from forge.core.ops.session import reset_session_overrides as reset_overrides_op

    if key and clear_all:
        console.print("[red]Error:[/red] Cannot specify both KEY and --all")
        sys.exit(1)

    try:
        ctx = ExecutionContext.from_cwd()
        result = reset_overrides_op(ctx=ctx, session_name=session_name, key=key)

        if result.cleared_all:
            if result.was_present:
                console.print("[green]Cleared all overrides[/green]")
            else:
                console.print("[dim]No overrides to clear[/dim]")
        else:
            if result.was_present:
                console.print(f"Reset [cyan]{result.key}[/cyan] [dim](now using intent value)[/dim]")
            else:
                console.print(f"[dim]No override for {result.key} (no-op)[/dim]")
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


def _print_session_summary(state: SessionState) -> None:
    """Print a brief session summary."""
    console.print(f"[green]{state.name}[/green]", end="")

    parts = [_template_display_label(state.intent.proxy.template) if state.intent.proxy else "direct"]
    console.print(f" ({', '.join(parts)})")

    if state.worktree:
        console.print(f"  [dim]{display_path(state.worktree.path)}[/dim]")


def _print_plan_info(plan_info: PlanInfo, *, current_forge_root: str, current_launch_root: str | None) -> None:
    """Print a Plan subsection for `forge session show`, if any plan info applies.

    Parent (inherited): one line, approved snapshot preferred.
    Self: approved snapshot line AND draft line when both exist (the draft is
    the live pointer for in-progress edits; the snapshot is the last approval).
    Paths are absolute, with ``(file missing)`` when not on disk.
    """
    if plan_info.source is None:
        return

    if plan_info.source == "parent":
        displayed = resolve_displayed_plan_path(
            plan_info,
            current_forge_root=current_forge_root,
            current_launch_root=current_launch_root,
        )
        if displayed is None:
            return
        kind = "approved snapshot" if plan_info.approved_snapshots else "draft"
        missing = "" if displayed.exists else " [dim](file missing)[/dim]"
        console.print(
            f"  Plan (inherited from {plan_info.parent_session}, {kind}): {display_path(displayed.path)}{missing}"
        )
        return

    if plan_info.approved_snapshots:
        snap_rel = latest_snapshot_path(plan_info.approved_snapshots)
        if snap_rel is not None:
            d = resolve_path_against(snap_rel, current_forge_root)
            missing = "" if d.exists else " [dim](file missing)[/dim]"
            count = len(plan_info.approved_snapshots)
            console.print(f"  Plans approved: {count} (latest: {display_path(d.path)}){missing}")
    if plan_info.draft_path:
        d = resolve_path_against(plan_info.draft_path, current_launch_root)
        missing = "" if d.exists else " [dim](file missing)[/dim]"
        console.print(f"  Plan (draft): {display_path(d.path)}{missing}")


def _print_session_detail(
    state: SessionState,
    entry: SessionIndexEntry,
    ctx: SessionContext | None = None,
) -> None:
    """Print detailed session information with optional computed context."""

    console.print(f"Session: [bold]{state.name}[/bold]")
    console.print("=" * 50)
    console.print()

    console.print("[bold]Basic Info[/bold]")
    if state.confirmed.claude_session_id:
        console.print(f"  UUID:         {state.confirmed.claude_session_id}")
    console.print(f"  Created:      {state.created_at}")
    console.print(f"  Last Used:    {state.last_accessed_at}")

    session_type = _get_session_type(state.is_fork, state.is_incognito, state.parent_session)
    console.print(f"  Type:         {session_type}")
    console.print()

    console.print("[bold]Configuration (Intent)[/bold]")
    console.print(f"  Agent:        {state.intent.agent}")
    if state.intent.proxy:
        console.print(f"  Routing:      {_template_display_label(state.intent.proxy.template)}")
        console.print(f"  Base URL:     {state.intent.proxy.base_url}")
    else:
        console.print("  Routing:      direct")
        console.print("  Base URL:     default Anthropic")
    console.print()

    if state.worktree:
        console.print("[bold]Worktree[/bold]")
        console.print(f"  Path:         {display_path(state.worktree.path)}")
        console.print(f"  Branch:       {state.worktree.branch}")
        console.print()

    current_forge_root = (
        entry.forge_root or state.forge_root or (state.worktree.path if state.worktree else str(Path.cwd()))
    )
    plan_info = resolve_plan_info(state, current_forge_root=current_forge_root)
    current_launch_root = resolve_plan_launch_root(state)

    # Confirmed state (from hooks)
    has_confirmed = (
        state.confirmed.claude_session_id
        or state.confirmed.transcript_path
        or plan_info.source
        or (state.confirmed.policy and state.confirmed.policy.decisions)
    )
    if has_confirmed:
        console.print("[bold]Confirmed State[/bold]")
        if state.confirmed.transcript_path:
            console.print(f"  Transcript:   {display_path(state.confirmed.transcript_path)}")
        if state.confirmed.confirmed_at:
            console.print(f"  Confirmed At: {state.confirmed.confirmed_at}")
        if state.confirmed.confirmed_by:
            console.print(f"  Confirmed By: {state.confirmed.confirmed_by}")
        _print_plan_info(
            plan_info,
            current_forge_root=current_forge_root,
            current_launch_root=current_launch_root,
        )
        if state.confirmed.policy and state.confirmed.policy.decisions:
            pc = state.confirmed.policy
            n = len(pc.decisions)
            last = pc.decisions[-1] if pc.decisions else None
            last_label = ""
            if last and isinstance(last, dict):
                last_decision = last.get("final_decision", "?")
                last_context = last.get("context_summary", "")
                last_label = f", last: {last_decision}"
                if last_context:
                    last_label += f" ({last_context})"
            console.print(f"  Policy Evals: {n} evaluation{'s' if n != 1 else ''}{last_label}")

    # Active overrides
    if state.overrides:
        console.print()
        console.print("[bold]Active Overrides[/bold]")
        for key, value in _flatten_overrides(state.overrides):
            console.print(f"  {key}: {_format_value(value)}")

    if ctx:
        console.print()
        console.print("[bold]Computed Context[/bold]")
        console.print(f"  Model Family: [cyan]{ctx.model_family}[/cyan]")
        if ctx.models:
            model_str = ", ".join(f"{t}={m}" for t, m in ctx.models.items())
            console.print(f"  Models:       {model_str}")
        if ctx.policy.enabled:
            bundles_str = ", ".join(ctx.policy.bundles) or "none"
            console.print(f"  Policy:       enabled (bundles: {bundles_str})")


def _flatten_overrides(
    overrides: dict,
    prefix: str = "",
) -> list[tuple[str, object]]:
    """Flatten nested override dict to dot-notation key-value pairs."""
    result: list[tuple[str, object]] = []
    for key, value in overrides.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            result.extend(_flatten_overrides(value, f"{full_key}."))
        else:
            result.append((full_key, value))
    return result


def _format_value(value: object) -> str:
    """Format a value for display."""
    if value is None:
        return "[dim]null[/dim]"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return f'"{value}"'
    return repr(value)
