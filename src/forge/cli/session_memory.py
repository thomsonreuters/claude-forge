"""``forge session memory`` -- manage designated memory docs.

Each verb operates on ``intent.memory.designated_docs`` (the list the
handoff agent consults at Stop time). The override engine treats lists as
replace-only, so these verbs do read-modify-write through the same
``set_session_override`` path used elsewhere, but with single-item ergonomics.

Validation mirrors the runtime checks in ``handoff_agent._validate_designated_docs``
so the CLI rejects the same docs the agent would silently skip.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from forge.cli.session import console
from forge.core.ops.context import ExecutionContext
from forge.core.ops.session import ForgeOpError, resolve_session, set_session_override
from forge.session.handoff_agent import is_safe_designated_doc_path
from forge.session.models import DesignatedDoc

VALID_STRATEGIES = {
    "project-state",
    "checklist",
    "changelog",
    "debugging",
    "patterns",
    "suggested",
    "generic",
}


@click.group("memory")
def memory_group() -> None:
    """Manage designated memory docs the handoff agent maintains.

    \b
    Examples:
        forge session memory list-docs
        forge session memory add-doc docs/checklist.md --strategy checklist
        forge session memory add-doc .forge/memory/suggested.md \\
            --strategy suggested --shadows docs/coding-standards.md
        forge session memory remove-doc docs/checklist.md
    """


def _validate_single_doc(path: str, strategy: str, shadows: str | None, base: Path) -> None:
    """Raise ``click.ClickException`` if the doc is invalid."""
    resolved_base = base.resolve()
    reason = is_safe_designated_doc_path(path, base, resolved_base)
    if reason:
        raise click.ClickException(f"Invalid path: {reason}")

    if shadows is not None:
        reason = is_safe_designated_doc_path(shadows, base, resolved_base)
        if reason:
            raise click.ClickException(f"Invalid --shadows path: {reason}")
        if shadows == path:
            raise click.ClickException("--shadows path must differ from doc path")

    if strategy not in VALID_STRATEGIES:
        raise click.ClickException(f"Unknown strategy {strategy!r}. Valid: {', '.join(sorted(VALID_STRATEGIES))}")

    if strategy == "suggested" and not shadows:
        raise click.ClickException("strategy 'suggested' requires --shadows <official-path>")
    if shadows is not None and strategy != "suggested":
        raise click.ClickException("--shadows requires --strategy suggested")
    if not (base / path).resolve().is_file():
        raise click.ClickException(f"Designated doc does not exist: {path}")
    if shadows is not None and not (base / shadows).resolve().is_file():
        raise click.ClickException(f"--shadows doc does not exist: {shadows}")


def _current_docs(*, ctx: ExecutionContext, session_name: str | None) -> tuple[list[DesignatedDoc], Path]:
    """Return (effective designated_docs, forge_root) for the resolved session."""
    resolved = resolve_session(ctx=ctx, session_name=session_name)
    state = resolved.state
    from forge.session.effective import compute_effective_intent

    effective = compute_effective_intent(state)
    docs = list(effective.memory.designated_docs) if effective.memory else []
    forge_root_str = state.forge_root or (state.worktree.path if state.worktree else None)
    if forge_root_str is None:
        raise ForgeOpError("Could not resolve forge_root for session.")
    return docs, Path(forge_root_str)


def _write_docs(*, ctx: ExecutionContext, session_name: str | None, docs: list[DesignatedDoc]) -> None:
    """Persist docs as an override on ``memory.designated_docs``."""
    payload = [{"path": d.path, "strategy": d.strategy, "shadows": d.shadows} for d in docs]
    set_session_override(
        ctx=ctx,
        session_name=session_name,
        key="memory.designated_docs",
        value_str=json.dumps(payload),
    )


@memory_group.command("list-docs")
@click.option("--session", "-s", "session_name", default=None, help="Target session (default: current).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def list_docs_cmd(session_name: str | None, as_json: bool) -> None:
    """List designated memory docs for the session."""
    try:
        ctx = ExecutionContext.from_cwd()
        docs, _ = _current_docs(ctx=ctx, session_name=session_name)
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if as_json:
        click.echo(
            json.dumps(
                [{"path": d.path, "strategy": d.strategy, "shadows": d.shadows} for d in docs],
                indent=2,
            )
        )
        return

    if not docs:
        console.print("[dim]No designated memory docs configured for this session.[/dim]")
        console.print("[dim]Tip: forge session memory add-doc <path> --strategy <s> [--shadows <official>][/dim]")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("Path", style="cyan")
    table.add_column("Strategy")
    table.add_column("Shadows")
    for doc in docs:
        table.add_row(doc.path, doc.strategy, doc.shadows or "")
    console.print(table)


@memory_group.command("add-doc")
@click.argument("path")
@click.option(
    "--strategy",
    type=click.Choice(sorted(VALID_STRATEGIES)),
    default="generic",
    show_default=True,
)
@click.option("--shadows", default=None, help="Official doc this proposes changes for (suggested strategy).")
@click.option("--session", "-s", "session_name", default=None, help="Target session (default: current).")
def add_doc_cmd(path: str, strategy: str, shadows: str | None, session_name: str | None) -> None:
    """Add a designated memory doc to the session intent.

    Validates path safety and strategy/shadows consistency before persisting.
    """
    try:
        ctx = ExecutionContext.from_cwd()
        docs, forge_root = _current_docs(ctx=ctx, session_name=session_name)
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    _validate_single_doc(path, strategy, shadows, forge_root)

    if any(d.path == path for d in docs):
        raise click.ClickException(f"A designated doc with path {path!r} is already configured. Remove it first.")

    docs.append(DesignatedDoc(path=path, strategy=strategy, shadows=shadows))

    try:
        _write_docs(ctx=ctx, session_name=session_name, docs=docs)
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"Added designated doc [cyan]{path}[/cyan] [dim](strategy={strategy})[/dim]")


@memory_group.command("remove-doc")
@click.argument("path")
@click.option("--session", "-s", "session_name", default=None, help="Target session (default: current).")
def remove_doc_cmd(path: str, session_name: str | None) -> None:
    """Remove a designated memory doc from the session intent."""
    try:
        ctx = ExecutionContext.from_cwd()
        docs, _ = _current_docs(ctx=ctx, session_name=session_name)
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    before = len(docs)
    docs = [d for d in docs if d.path != path]
    if len(docs) == before:
        raise click.ClickException(f"No designated doc with path {path!r}")

    try:
        _write_docs(ctx=ctx, session_name=session_name, docs=docs)
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"Removed designated doc [cyan]{path}[/cyan]")
