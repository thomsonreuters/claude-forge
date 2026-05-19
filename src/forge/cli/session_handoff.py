"""``forge session handoff show`` -- inspect handoff-agent review reports.

The handoff agent (``handoff_agent.py``) runs detached from the Stop work
queue, so its stdout vanishes to ``DEVNULL``. To make the agent's proposed
or applied changes visible, ``run_handoff_agent`` persists each run's output
to ``<forge_root>/.forge/artifacts/<session>/handoff/review-<timestamp>.md``.
This module surfaces that file via ``forge session handoff show``.

Note: this is the memory-doc agent's review surface, not the resume-context
file. See ``forge.session.handoff`` for the resume-context generator.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich.syntax import Syntax

from forge.cli.session import _cwd_forge_root, _handle_error, console
from forge.session import ForgeSessionError, SessionManager
from forge.session.handoff_agent import review_dir


@click.group("handoff")
def handoff_group() -> None:
    """Inspect handoff-agent review reports for a session.

    \b
    Examples:
        forge session handoff show                # Latest report for current session
        forge session handoff show my-session     # Latest report for named session
        forge session handoff show --all          # List all reports
    """


def _resolve_session_forge_root(session_name: str | None) -> tuple[str, Path]:
    """Resolve (session_name, forge_root) using current cwd if no name given.

    Resolution rules:
    - If session_name is provided, look it up by name (with current forge_root
      as scope hint to disambiguate cross-project name reuse).
    - If session_name is None, derive from the current Forge project: use the
      single active session, or error if zero/multiple.
    """
    manager = SessionManager()
    current_root = _cwd_forge_root()

    if session_name is None:
        env_name = os.environ.get("FORGE_SESSION")
        if env_name:
            env_root = os.environ.get("FORGE_FORGE_ROOT") or (str(current_root) if current_root else None)
            try:
                env_entry = manager.index_store.get_session(env_name, forge_root=env_root)
            except ForgeSessionError:
                pass
            else:
                return env_name, Path(env_entry.forge_root or env_entry.worktree_path)

        if current_root is None:
            console.print(
                "[red]Error:[/red] Not inside a Forge project, and no session name given.\n"
                "[dim]Tip: run from a directory with .forge/ or pass an explicit session name.[/dim]"
            )
            sys.exit(1)
        sessions = manager.list_sessions(
            include_incognito=True,
            forge_root_filter=str(current_root),
        )
        if len(sessions) == 0:
            console.print(f"[red]Error:[/red] No sessions found under {current_root}.")
            sys.exit(1)
        if len(sessions) > 1:
            console.print("[red]Error:[/red] Multiple sessions in this Forge project; " "specify which one explicitly.")
            for name, _entry in sessions:
                console.print(f"  - {name}")
            sys.exit(1)
        return sessions[0][0], Path(sessions[0][1].forge_root or sessions[0][1].worktree_path)

    try:
        resolved_entry = manager.index_store.get_session(
            session_name,
            forge_root=str(current_root) if current_root else None,
        )
    except ForgeSessionError as e:
        _handle_error(e)
        raise  # for mypy; _handle_error sys.exits

    return session_name, Path(resolved_entry.forge_root or resolved_entry.worktree_path)


def _list_reports(forge_root: Path, session_name: str) -> list[Path]:
    """Return review-*.md files sorted oldest -> newest."""
    target = review_dir(forge_root, session_name)
    if not target.is_dir():
        return []
    return sorted(
        (p for p in target.iterdir() if p.is_file() and p.name.startswith("review-") and p.suffix == ".md"),
        key=lambda p: p.name,
    )


@handoff_group.command("show")
@click.argument("session_name", required=False)
@click.option("--latest", "show_latest", is_flag=True, default=False, help="Show the most recent report (default).")
@click.option("--all", "show_all", is_flag=True, default=False, help="List all reports with timestamps.")
def show_cmd(session_name: str | None, show_latest: bool, show_all: bool) -> None:
    """Show a handoff-agent review report for a session.

    With no flags, prints the most recent report. ``--all`` lists every
    report (paths + timestamps).
    """
    if show_latest and show_all:
        console.print("[red]Error:[/red] --latest and --all are mutually exclusive.")
        sys.exit(1)

    resolved_name, forge_root = _resolve_session_forge_root(session_name)
    reports = _list_reports(forge_root, resolved_name)

    if not reports:
        console.print(f"[dim]No handoff reports found for session [cyan]{resolved_name}[/cyan].[/dim]")
        console.print(
            f"[dim]Tip: the handoff agent writes reports to " f"{review_dir(forge_root, resolved_name)}[/dim]"
        )
        return

    if show_all:
        console.print(f"[bold]Handoff reports for [cyan]{resolved_name}[/cyan][/bold]")
        for path in reports:
            console.print(f"  {path.name}  [dim]{path}[/dim]")
        return

    # Default: show latest
    latest = reports[-1]
    console.print(f"[bold]Handoff report:[/bold] [dim]{latest}[/dim]")
    console.print()
    content = latest.read_text(encoding="utf-8")
    syntax = Syntax(content, "markdown", theme="monokai", word_wrap=True)
    console.print(syntax)
