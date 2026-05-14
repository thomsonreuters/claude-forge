"""Forge extensions commands (extensions lifecycle).

Commands:
- forge extension enable  - Enable Forge extensions
- forge extension sync    - Sync existing extensions
- forge extension disable - Disable extensions
- forge extension status  - Show extensions status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from forge.core.paths import display_path
from forge.install.exceptions import (
    ForgeInstallError,
    NoClaudeDirectoryError,
    NoForgeInstallationError,
    NotInstalledError,
    SettingsConflictError,
    TrackingCorruptedError,
)
from forge.install.installer import Installer, find_claude_root, find_forge_installation
from forge.install.models import (
    FILE_MODULES,
    InstallMode,
    InstallModule,
    InstallPlan,
    InstallProfile,
    InstallScope,
    get_gated_skills,
)
from forge.install.tracking import TrackingStore

console = Console()
_log = logging.getLogger(__name__)


def _find_git_root(start: Path) -> Path | None:
    """Walk up from *start* looking for ``.git``.

    Returns the directory containing ``.git``, or None if not in a git repo.
    Pure detector -- no side effects.
    """
    current = start.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    if (current / ".git").exists():
        return current
    return None


def _detect_git_project_root(start: Path | None = None) -> Path | None:
    """Find the git root suitable for auto-creating ``.claude/`` (Rule 4).

    Returns the resolved git root, or None if not in a git repo or the
    git root is the user's home directory.  Pure detector -- no side effects.
    """
    cwd = (start or Path.cwd()).resolve()
    git_root = _find_git_root(cwd)
    if git_root is None:
        return None

    home = Path.home().resolve()
    if git_root == home:
        return None

    return git_root.resolve()


def _create_claude_dir(root: Path) -> None:
    """Create ``.claude/`` at *root* and log the action."""
    claude_dir = root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    _log.info("Created %s for Forge project", claude_dir)
    console.print(f"[dim]Created {display_path(claude_dir)}[/dim]")


def _parse_modules(modules_str: str | None) -> set[InstallModule] | None:
    """Parse comma-separated module names.

    Args:
        modules_str: Comma-separated module names.

    Returns:
        Set of InstallModule, or None if input is None/empty.
    """
    if not modules_str:
        return None
    return {InstallModule(m.strip()) for m in modules_str.split(",")}


def _count_actions(plan: InstallPlan) -> tuple[int, int]:
    """Count non-skip actions in a plan.

    Returns:
        Tuple of (file_actions, settings_actions) that are not skips.
    """
    file_actions = sum(1 for f in plan.files if f.action != "skip")
    settings_actions = sum(1 for s in plan.settings if s.action != "skip")
    return file_actions, settings_actions


# Modules that are intentionally empty in the source tree (only .gitkeep).
# Checked by allowlist so a broken wheel that omits skills/ still warns.
_INTENTIONALLY_EMPTY_MODULES: set[InstallModule] = {
    InstallModule.AGENTS,
    InstallModule.COMMANDS,
}


def _warn_if_modules_have_no_files(
    plan: InstallPlan,
    scope: InstallScope,
    project_root: Path | None,
    tracking: TrackingStore,
) -> None:
    """Warn when a file-bearing module has no files anywhere (plan or tracking).

    A clean install with 0 files in the plan is normal IF the existing
    tracked install already has files for the module. But if neither plan
    nor tracking has files for an enabled file-bearing module, the install
    is broken — typically a wheel missing bundled extensions.
    """
    enabled = {InstallModule(m) for m in plan.modules if InstallModule(m) in FILE_MODULES}
    enabled -= _INTENTIONALLY_EMPTY_MODULES
    if not enabled:
        return

    project_str = None if scope == InstallScope.USER else (str(project_root) if project_root else None)
    existing = tracking.get_installation(scope.value, project_str)

    def _module_has_files(module: InstallModule, paths: list[str]) -> bool:
        sep = f"/{module.value}/"
        return any(sep in p for p in paths)

    plan_paths = [f.target_path for f in plan.files]
    existing_paths = [f.target_path for f in existing.files] if existing else []

    missing = {m for m in enabled if not _module_has_files(m, plan_paths) and not _module_has_files(m, existing_paths)}
    if not missing:
        return

    names = ", ".join(sorted(m.value for m in missing))
    console.print(
        f"\n[yellow]Warning:[/yellow] No files found for enabled module(s): {names}. "
        "Your Forge installation may be missing bundled extensions. "
        "Try reinstalling: 'pip install --force-reinstall <wheel>'."
    )


def _print_completion_message(
    plan: InstallPlan,
    scope: InstallScope,
    project_root: Path | None,
    tracking: TrackingStore,
) -> None:
    """Print appropriate completion message based on what was done."""
    file_actions, settings_actions = _count_actions(plan)
    total_actions = file_actions + settings_actions

    _warn_if_modules_have_no_files(plan, scope, project_root, tracking)

    if total_actions == 0:
        console.print("\n[dim]Already up to date.[/dim]")
    else:
        parts = []
        if file_actions > 0:
            parts.append(f"{file_actions} file{'s' if file_actions != 1 else ''}")
        if settings_actions > 0:
            parts.append(f"{settings_actions} setting{'s' if settings_actions != 1 else ''}")
        console.print(f"\n[green]Extensions enabled.[/green] ({', '.join(parts)} updated)")

    console.print("[dim]Tip: Customize permissions and env vars with 'forge claude preset edit'.[/dim]")

    if InstallModule.SKILLS.value in plan.modules:
        console.print(
            "[dim]Tip: Multi-model skills require proxy credentials. " "Run 'forge auth status' to check.[/dim]"
        )

    profile = InstallProfile(plan.profile)
    gated = get_gated_skills(profile)
    if gated:
        skill_list = ", ".join(f"/forge:{name}" for name, _ in gated)
        required = gated[0][1].value
        console.print(f"\n[dim]Tip: Additional skills available with --profile {required}: {skill_list}[/dim]")


def _validate_anchor(anchor: Path) -> None:
    """Reject anchors that point inside a ``.claude/`` directory.

    The ``.claude/`` creation in ``enable_cmd`` runs before the installer's
    ``get_target_root()`` guard, so an anchor like ``/repo/.claude`` would
    create ``/repo/.claude/.claude/`` before the guard fires.
    """
    resolved = anchor.expanduser().resolve()
    if ".claude" in resolved.parts:
        raise click.UsageError(
            f"--path points inside a .claude directory: {anchor}\n"
            "Provide the project root instead (the parent of .claude/)."
        )


def _resolve_project_root(
    scope: InstallScope,
    *,
    anchor: Path | None = None,
    auto_create: bool = False,
) -> Path | None:
    """Resolve canonical project root for a given scope.

    For user scope, returns None.
    For project/local scope, finds the .claude directory and returns
    the canonicalized project root.  When *auto_create* is True and no
    ``.claude/`` exists, creates it at the git root (Rule 4).

    When *anchor* is provided, skips the walk-up and uses that path directly.

    Args:
        scope: The installation scope.
        anchor: Explicit target directory (skips walk-up when set).
        auto_create: Whether to create ``.claude/`` if missing (Rule 4).

    Returns:
        Canonicalized project root path, or None for user scope.

    Raises:
        NoClaudeDirectoryError: If no .claude directory found and auto-create
            is disabled or not in a git repo.
    """
    if scope == InstallScope.USER:
        return None

    if anchor is not None:
        resolved = anchor.expanduser().resolve()
        if auto_create and not (resolved / ".claude").is_dir():
            _create_claude_dir(resolved)
        return resolved

    try:
        _detected_scope, project_root = find_claude_root()
    except NoClaudeDirectoryError:
        # find_claude_root raises when walk reaches FS root without home;
        # treat the same as "no .claude/ found" for auto-create purposes.
        project_root = None

    if project_root is None:
        # Rule 4: auto-create .claude/ at git root for project/local enable
        git_root = _detect_git_project_root()
        if git_root is not None:
            if auto_create:
                _create_claude_dir(git_root)
            return git_root
        raise NoClaudeDirectoryError(
            "No .claude directory found. Use '--scope user' for global install, "
            "or run from within a Claude Code project."
        )

    # Canonicalize to handle symlinks and ensure consistent keys
    return project_root.resolve()


def _print_plan(plan: InstallPlan, dry_run: bool = False) -> None:
    """Print installation plan using Rich.

    Args:
        plan: The plan to display.
        dry_run: If True, prefix output with "(dry-run)".
    """
    prefix = "[dim](dry-run)[/dim] " if dry_run else ""

    console.print(f"\n{prefix}[bold]Installation Plan[/bold]")
    console.print(f"  Scope:   {plan.scope}")
    console.print(f"  Mode:    {plan.mode}")
    console.print(f"  Profile: {plan.profile}")
    console.print(f"  Modules: {', '.join(plan.modules)}")

    if plan.files:
        console.print(f"\n{prefix}[bold]Files:[/bold]")
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("ACTION", style="dim")
        table.add_column("PATH")
        table.add_column("REASON", style="dim")

        for f in plan.files:
            style = {
                "install": "green",
                "update": "yellow",
                "skip": "dim",
                "conflict": "red",
            }.get(f.action, "")
            table.add_row(f.action, display_path(f.target_path), f.reason or "", style=style)

        console.print(table)

    if plan.settings:
        console.print(f"\n{prefix}[bold]Settings:[/bold]")
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("ACTION", style="dim")
        table.add_column("KEY")
        table.add_column("VALUE", style="dim")

        for s in plan.settings:
            style = "red" if s.action == "conflict" else ""
            value_str = str(s.value) if s.value else ""
            if s.action == "conflict":
                value_str = f"current={s.current_value!r}, forge={s.value!r}"
            table.add_row(s.action, s.key_path, value_str, style=style)

        console.print(table)

    if plan.has_conflicts:
        console.print(f"\n{prefix}[bold red]Conflicts detected:[/bold red]")
        for c in plan.conflicts:
            console.print(f"  [red]- {c}[/red]")
        console.print("\n[dim]Tip: Use --force to override, or resolve conflicts manually.[/dim]")


def _uninstall_all_installations(tracking: TrackingStore, yes: bool) -> None:
    """Uninstall all tracked installations.

    Args:
        tracking: TrackingStore instance.
        yes: If True, skip confirmation prompt.
    """
    installations = tracking.list_installations()

    if not installations:
        console.print("[dim]No Forge installations found.[/dim]")
        return

    console.print(f"[bold]Found {len(installations)} Forge installation(s):[/bold]\n")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("SCOPE", style="cyan")
    table.add_column("PROJECT PATH")
    table.add_column("PROFILE")
    table.add_column("FILES")

    for scope, project_path, installation in installations:
        scope_display = scope
        path_display = project_path or "(global)"
        if len(path_display) > 40:
            path_display = "…" + path_display[-37:]
        table.add_row(
            scope_display,
            path_display,
            installation.profile,
            str(len(installation.files)),
        )

    console.print(table)
    console.print()

    if not yes:
        if not click.confirm("Disable ALL of these?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    errors = []
    for scope, project_path, _installation in installations:
        try:
            console.print(f"\n[bold]Disabling {scope}[/bold]", end="")
            if project_path:
                console.print(f" [dim]({display_path(project_path)})[/dim]")
            else:
                console.print()

            install_scope = InstallScope(scope)
            project_root = Path(project_path) if project_path else None

            installer = Installer(scope=install_scope, project_root=project_root)
            installer.uninstall()
            console.print("  [green]✓ Done[/green]")

        except ForgeInstallError as e:
            console.print(f"  [red]✗ Failed: {e}[/red]")
            errors.append((scope, project_path, str(e)))

    console.print()
    if errors:
        console.print(f"[yellow]Completed with {len(errors)} error(s).[/yellow]")
        for scope, path, err in errors:
            console.print(f"  [red]- {scope} ({display_path(path) if path else 'global'}): {err}[/red]")
    else:
        console.print(f"[green]All {len(installations)} installation(s) disabled.[/green]")


def _can_resolve_project_root(scope: InstallScope, *, anchor: Path | None = None) -> bool:
    """Check if project root can be resolved without raising."""
    try:
        _resolve_project_root(scope, anchor=anchor)
        return True
    except NoClaudeDirectoryError:
        return False


# --- Commands ---


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def extensions() -> None:
    """Manage Forge extensions lifecycle.

    \b
    Examples:
        forge extension enable                  # Auto-detect scope, enable
        forge extension status                 # Show installation status
        forge extension sync                   # Sync to latest version
    """
    pass


@extensions.command("enable")
@click.option(
    "--scope",
    "-S",
    type=click.Choice(["local", "project", "user"]),
    default=None,
    help="Installation scope: local (gitignored), project (committed), user (global)",
)
@click.option(
    "--path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Target directory (default: walk up from cwd to find .claude/)",
)
@click.option(
    "--profile",
    "-p",
    type=click.Choice(["minimal", "standard", "full"]),
    default="standard",
    help="Installation profile",
)
@click.option(
    "--copy",
    "-c",
    "mode",
    flag_value="copy",
    default=True,
    help="Copy files (default)",
)
@click.option(
    "--symlink",
    "-s",
    "mode",
    flag_value="symlink",
    help="Symlink files (dev mode)",
)
@click.option(
    "--with",
    "-w",
    "with_modules",
    help="Add modules (comma-separated: commands,agents,skills,hooks,status-line,permissions)",
)
@click.option(
    "--without",
    "-W",
    "without_modules",
    help="Remove modules (comma-separated)",
)
@click.option("--force", "-f", is_flag=True, help="Override conflicts")
@click.option("--dry-run", "-n", is_flag=True, help="Show plan without executing")
def enable_cmd(
    scope: str | None,
    path: str | None,
    profile: str,
    mode: str,
    with_modules: str | None,
    without_modules: str | None,
    force: bool,
    dry_run: bool,
) -> None:
    """Enable Forge extensions.

    \b
    Scope Detection (when no --scope specified):
        Walks up from current directory looking for a .claude/ directory.
        - If found: enables local in that project's .claude/settings.local.json
        - If in a git repo: enables local at the git root
        - If reached ~: enables user in ~/.claude/settings.json
        - If not found: fails (use --scope user outside a project)

    \b
    Examples:
        forge extension enable                                # Auto-detect scope
        forge extension enable --scope local                  # Local at nearest .claude/
        forge extension enable --scope local --path /repo/api # Local at specific path
        forge extension enable --path /repo/api               # Same (defaults to local)
        forge extension enable --scope user                   # Global ~/.claude
        forge extension enable --profile minimal              # Commands only
        forge extension enable --dry-run                      # Preview changes
    """
    try:
        # Check Claude Code minimum version (hard-block: reject over warn)
        from forge.install.version import check_minimum_version

        version_check = check_minimum_version()
        if not version_check.ok:
            console.print(f"[red]Error:[/red] {version_check.reason}")
            console.print("\n[dim]Tip: Run 'claude update' to upgrade.[/dim]")
            sys.exit(1)

        anchor = Path(path) if path else None

        # Validate: --scope user + --path is contradictory
        if scope == "user" and anchor is not None:
            raise click.UsageError("--scope user is global; --path is not applicable.")

        # Validate: anchor must not point inside .claude/
        if anchor is not None:
            _validate_anchor(anchor)

        # Default: --path without --scope implies local
        if anchor is not None and scope is None:
            scope = "local"

        # --- Scope resolution (Rule 4: auto-create .claude/ in git repos) ---
        needs_create = False

        if scope is None:
            install_scope, project_root = find_claude_root()
            # P1 fix: auto-detect in a git repo should prefer LOCAL over USER
            if install_scope == InstallScope.USER:
                git_root = _detect_git_project_root()
                if git_root is not None:
                    install_scope = InstallScope.LOCAL
                    project_root = git_root
                    needs_create = not (git_root / ".claude").is_dir()
            console.print(f"[dim]Auto-detected scope: {install_scope.value}[/dim]")
        else:
            install_scope = InstallScope(scope)
            project_root = _resolve_project_root(install_scope, anchor=anchor, auto_create=False)
            if project_root is not None:
                needs_create = not (project_root / ".claude").is_dir()

        # Create .claude/ only when not dry-run
        if needs_create and project_root is not None:
            if dry_run:
                console.print(f"[dim]Would create {display_path(project_root / '.claude')}[/dim]")
            else:
                _create_claude_dir(project_root)

        # Rule 1 anchor: .forge/ is required for session start.
        # Preview in dry-run; actual creation deferred until installer succeeds.
        needs_forge = project_root is not None and not (project_root / ".forge").is_dir()
        if needs_forge and dry_run and project_root is not None:
            console.print(f"[dim]Would create {display_path(project_root / '.forge')}[/dim]")

        install_profile = InstallProfile(profile)
        install_mode = InstallMode(mode)

        installer = Installer(scope=install_scope, project_root=project_root)

        if dry_run:
            plan = installer.plan(
                profile=install_profile,
                mode=install_mode,
                with_modules=_parse_modules(with_modules),
                without_modules=_parse_modules(without_modules),
                force=force,
            )
            _print_plan(plan, dry_run=True)
            if plan.has_conflicts:
                sys.exit(1)
        else:
            plan = installer.init(
                profile=install_profile,
                mode=install_mode,
                with_modules=_parse_modules(with_modules),
                without_modules=_parse_modules(without_modules),
                force=force,
            )
            _print_plan(plan)
            if plan.has_conflicts:
                console.print("\n[red]Enable failed due to conflicts.[/red]")
                sys.exit(1)
            else:
                # Create .forge/ only after installer succeeds (avoids orphaned
                # directories if enable fails due to conflicts).
                if needs_forge and project_root is not None:
                    (project_root / ".forge").mkdir(exist_ok=True)
                    _log.info("Created %s for session state", project_root / ".forge")

                _print_completion_message(plan, install_scope, project_root, TrackingStore())

    except click.UsageError:
        raise
    except NoClaudeDirectoryError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print(
            "\n[dim]Tip: Use '--scope user' to enable globally, "
            "or '--path <dir>' to target a specific directory.[/dim]"
        )
        sys.exit(1)
    except SettingsConflictError as e:
        console.print(f"[red]Settings conflict:[/red] {e}")
        console.print("\n[dim]Tip: Use --force to override.[/dim]")
        sys.exit(1)
    except ForgeInstallError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@extensions.command("sync")
@click.option(
    "--scope",
    "-S",
    type=click.Choice(["local", "project", "user"]),
    default=None,
    help="Installation scope",
)
@click.option("--force", "-f", is_flag=True, help="Override conflicts")
def sync_cmd(scope: str | None, force: bool) -> None:
    """Sync existing Forge extensions.

    Re-runs the enable with the same profile and mode as originally
    configured, refreshing all files and settings from the current Forge
    source.

    \b
    Scope Detection (when no --scope specified):
        Walks up from current directory looking for existing Forge extensions
        (detected by .settings.*.json.forge.* files in .claude/).
        - Checks LOCAL first, then PROJECT, then USER
        - Fails if no extensions found

    \b
    Examples:
        forge extension sync                    # Sync Forge extensions
        forge extension sync --scope local      # Sync local scope
        forge extension sync --force            # Force re-sync
    """
    try:
        # Check Claude Code minimum version (same gate as enable)
        from forge.install.version import check_minimum_version

        version_check = check_minimum_version()
        if not version_check.ok:
            console.print(f"[red]Error:[/red] {version_check.reason}")
            console.print("\n[dim]Tip: Run 'claude update' to upgrade.[/dim]")
            sys.exit(1)

        if scope is None:
            install_scope, project_root = find_forge_installation()
            console.print(f"[dim]Auto-detected scope: {install_scope.value}[/dim]")
        else:
            install_scope = InstallScope(scope)
            # Use canonical project root (finds .claude/ and resolves symlinks)
            project_root = _resolve_project_root(install_scope)

        installer = Installer(scope=install_scope, project_root=project_root)
        plan = installer.update(force=force)

        _print_plan(plan)
        if plan.has_conflicts:
            console.print("\n[red]Sync failed due to conflicts.[/red]")
            sys.exit(1)
        else:
            file_actions, settings_actions = _count_actions(plan)
            total_actions = file_actions + settings_actions
            if total_actions == 0:
                console.print("\n[dim]Already up to date.[/dim]")
            else:
                parts = []
                if file_actions > 0:
                    parts.append(f"{file_actions} file{'s' if file_actions != 1 else ''}")
                if settings_actions > 0:
                    parts.append(f"{settings_actions} setting{'s' if settings_actions != 1 else ''}")
                console.print(f"\n[green]Sync complete.[/green] ({', '.join(parts)} updated)")

    except NoForgeInstallationError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except NotInstalledError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: Run 'forge extension enable' first.[/dim]")
        sys.exit(1)
    except ForgeInstallError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@extensions.command("disable")
@click.option(
    "--scope",
    "-S",
    type=click.Choice(["local", "project", "user"]),
    default=None,
    help="Installation scope",
)
@click.option(
    "--all",
    "-a",
    "uninstall_all",
    is_flag=True,
    help="Disable ALL tracked installations",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--force", "-f", is_flag=True, hidden=True, help="Deprecated alias for --yes")
def disable_cmd(scope: str | None, uninstall_all: bool, yes: bool, force: bool) -> None:
    """Disable Forge extensions.

    Removes only files and settings entries that were added by Forge.
    User modifications are preserved.

    \b
    Scope Detection (when no --scope/--all specified):
        Walks up from current directory looking for existing Forge extensions
        (detected by .settings.*.json.forge.* files in .claude/).
        - Checks LOCAL first, then PROJECT, then USER
        - Fails if no extensions found

    \b
    --all mode:
        Disables ALL tracked installations (user + all local/project).
        Uses ~/.forge/installed.json to find all installations.

    \b
    Examples:
        forge extension disable                   # Auto-detect scope
        forge extension disable --scope local     # Disable local scope
        forge extension disable --all --yes       # Disable everything
    """
    yes = yes or force

    if uninstall_all and scope is not None:
        raise click.UsageError("--all and --scope are mutually exclusive.")
    try:
        tracking = TrackingStore()

        if uninstall_all:
            _uninstall_all_installations(tracking, yes)
            return

        if scope is None:
            install_scope, project_root = find_forge_installation()
            console.print(f"[dim]Auto-detected scope: {install_scope.value}[/dim]")
        else:
            install_scope = InstallScope(scope)
            # Use canonical project root (finds .claude/ and resolves symlinks)
            project_root = _resolve_project_root(install_scope)

        project_path_str = str(project_root) if project_root else None
        existing = tracking.get_installation(install_scope.value, project_path_str)

        if existing is None:
            console.print(f"[dim]No Forge installation for scope '{install_scope.value}'.[/dim]")
            return

        console.print(f"[bold]Will disable Forge extensions ({install_scope.value}):[/bold]")
        console.print(f"  Profile:  {existing.profile}")
        console.print(f"  Mode:     {existing.mode}")
        console.print()

        if existing.files:
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
            table.add_column("ACTION", style="red")
            table.add_column("PATH")
            for f in existing.files:
                # Truncate long paths for display
                path_str = str(f.target_path)
                if len(path_str) > 60:
                    path_str = path_str[:57] + "…"
                table.add_row("remove", path_str)
            console.print("[bold]Files:[/bold]")
            console.print(table)
            console.print()

        if existing.settings_entries:
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
            table.add_column("ACTION", style="red")
            table.add_column("KEY")
            for entry in existing.settings_entries:
                table.add_row("unmerge", entry.key_path)
            console.print("[bold]Settings:[/bold]")
            console.print(table)

        if not (force or yes):
            if not click.confirm("\nProceed with disable?"):
                console.print("[dim]Cancelled.[/dim]")
                return

        installer = Installer(scope=install_scope, project_root=project_root)
        installer.uninstall()

        # Remove .forge/ anchor if it's empty (no sessions, artifacts, etc.).
        # .claude/ is NOT removed — it may contain user-authored content.
        if project_root is not None:
            forge_dir = project_root / ".forge"
            if forge_dir.is_dir():
                try:
                    forge_dir.rmdir()  # Only succeeds if empty
                    _log.info("Removed empty %s", forge_dir)
                except OSError:
                    pass  # Non-empty: sessions/artifacts still present

        console.print("\n[green]Extensions disabled.[/green]")

    except NoForgeInstallationError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except ForgeInstallError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except TrackingCorruptedError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


@extensions.command("status")
@click.option(
    "--scope",
    "-S",
    type=click.Choice(["local", "project", "user"]),
    default=None,
    help="Installation scope",
)
@click.option(
    "--path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Target directory to check (default: walk up from cwd)",
)
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all scopes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status_cmd(scope: str | None, path: str | None, show_all: bool, as_json: bool) -> None:
    """Show extensions status.

    Displays what Forge has enabled in the specified scope(s).

    \b
    Scope Detection (when no --scope/--all specified):
        Walks up from current directory looking for existing Forge installations
        (detected by .settings.*.json.forge.* files in .claude/).
        - Checks LOCAL first, then PROJECT, then USER
        - If no installation found, shows all scopes for informational purposes

    \b
    Examples:
        forge extension status                                # Auto-detect
        forge extension status --scope local --path /repo/api # Check specific install
        forge extension status --path /repo/api               # Auto-detect scope at path
        forge extension status --all                          # Show all scopes
    """
    import os

    anchor = Path(path) if path else None

    if show_all and scope is not None:
        raise click.UsageError("--all and --scope are mutually exclusive.")
    if show_all and anchor is not None:
        raise click.UsageError("--all and --path are mutually exclusive.")
    if scope == "user" and anchor is not None:
        raise click.UsageError("--scope user is global; --path is not applicable.")

    try:
        tracking = TrackingStore()
        tracking.read()
    except TrackingCorruptedError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise SystemExit(1) from None

    cwd = os.getcwd()

    # When auto-detect finds the real install root (which may differ from
    # anchor if --path points at a subdirectory), use it for tracking lookups.
    detected_root: Path | None = None

    detected_scope_name: str | None = None
    if show_all:
        scopes = [InstallScope.USER, InstallScope.PROJECT, InstallScope.LOCAL]
    elif scope is None and anchor is None:
        try:
            detected_scope, detected_root = find_forge_installation()
            detected_scope_name = detected_scope.value
            scopes = [detected_scope]
        except NoForgeInstallationError:
            scopes = [InstallScope.USER, InstallScope.PROJECT, InstallScope.LOCAL]
    elif scope is None and anchor is not None:
        # --path without --scope: auto-detect scope at that path
        try:
            detected_scope, detected_root = find_forge_installation(start=anchor)
            detected_scope_name = detected_scope.value
            scopes = [detected_scope]
        except NoForgeInstallationError:
            scopes = [InstallScope.USER, InstallScope.PROJECT, InstallScope.LOCAL]
    else:
        scopes = [InstallScope(scope)]

    # Use the detected root (from walk-up) over the raw anchor for lookups.
    effective_anchor = detected_root if detected_root is not None else anchor

    if as_json:
        import json

        data = []
        for s in scopes:
            try:
                project_root = _resolve_project_root(s, anchor=effective_anchor)
                project_path_str = str(project_root) if project_root else None
            except NoClaudeDirectoryError:
                project_path_str = None

            inst = tracking.get_installation(s.value, project_path_str)
            if inst is None:
                continue
            data.append(
                {
                    "scope": s.value,
                    "profile": inst.profile,
                    "mode": inst.mode,
                    "modules": list(inst.modules_enabled),
                    "files_count": len(inst.files),
                    "settings_count": len(inst.settings_entries),
                    "installed_at": inst.installed_at,
                    "updated_at": inst.updated_at,
                }
            )
        click.echo(json.dumps(data, indent=2, default=str))
        return

    if detected_scope_name:
        console.print(f"[dim]Auto-detected scope: {detected_scope_name}[/dim]")
    elif scope is None and not show_all:
        location = display_path(str(anchor)) if anchor else display_path(cwd)
        console.print(f"[dim]No extensions detected in {location}[/dim]")
        console.print("[dim]Showing all scopes for this location:[/dim]")

    for s in scopes:
        try:
            project_root = _resolve_project_root(s, anchor=effective_anchor)
            project_path_str = str(project_root) if project_root else None
        except NoClaudeDirectoryError:
            project_path_str = None

        installation = tracking.get_installation(s.value, project_path_str)

        console.print(f"\n[bold]Scope: {s.value}[/bold]")

        if installation is None:
            if s == InstallScope.USER:
                location = "~/.claude"
            elif project_path_str:
                location = project_path_str
            else:
                location = str(anchor) if anchor else cwd
            console.print(f"  [dim]Not enabled at {display_path(location)}[/dim]")
            continue

        console.print(f"  Profile:   {installation.profile}")
        console.print(f"  Mode:      {installation.mode}")
        console.print(f"  Modules:   {', '.join(installation.modules_enabled)}")
        console.print(f"  Files:     {len(installation.files)}")
        console.print(f"  Settings:  {len(installation.settings_entries)} entries")
        console.print(f"  Installed: {installation.installed_at}")
        console.print(f"  Updated:   {installation.updated_at}")

        try:
            inst_profile = InstallProfile(installation.profile)
            gated = get_gated_skills(inst_profile)
            if gated:
                skill_list = ", ".join(f"/forge:{name}" for name, _ in gated)
                required = gated[0][1].value
                console.print(f"  [dim]Gated:    {skill_list} (needs --profile {required})[/dim]")
        except ValueError:
            pass

        if installation.files and len(installation.files) <= 10:
            console.print("\n  [dim]Files:[/dim]")
            for f in installation.files:
                console.print(f"    - {display_path(f.target_path)}")

    if scope is None and not show_all and anchor is None:
        local_installed = any(
            tracking.get_installation(
                s.value,
                str(_resolve_project_root(s)) if s != InstallScope.USER else None,
            )
            for s in scopes
            if s == InstallScope.USER or _can_resolve_project_root(s)
        )
        if not local_installed:
            all_installations = tracking.list_installations()
            if all_installations:
                console.print(
                    f"\n[dim]Tip: {len(all_installations)} installation(s) exist elsewhere. "
                    "Use 'forge info' to see all.[/dim]"
                )
            else:
                console.print("\n[dim]Tip: Run 'forge extension enable' to set up Forge.[/dim]")
