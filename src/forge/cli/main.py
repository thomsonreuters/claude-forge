"""Root CLI command group for Forge.

This module defines the `forge` command group and registers subcommands.
"""

from __future__ import annotations

import logging

import click
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env early before any config access
load_dotenv()

from forge.install.cli import info_cmd  # noqa: E402

from .auth import auth  # noqa: E402
from .backend import backend  # noqa: E402
from .claude import claude  # noqa: E402
from .config_cmd import config as config_cmd  # noqa: E402
from .extensions import extensions  # noqa: E402
from .guard import guard  # noqa: E402
from .handoff import handoff  # noqa: E402
from .hooks import hooks  # noqa: E402
from .proxy import proxy  # noqa: E402
from .search import search_cmd  # noqa: E402
from .session import session  # noqa: E402
from .status_line import status_line  # noqa: E402
from .workflow import workflow_cmd  # noqa: E402

# Subcommands that should NOT trigger pending-work processing or auto file logging.
# Hooks and status-line are latency-sensitive; logs is exempt so it can inspect/clean
# log files without creating a fresh "logs.*.log" file as a side effect.
_EXEMPT_SUBCOMMANDS = frozenset({"hook", "status-line", "logs", "clean"})

# Session auto-cleanup is also exempt for session subcommands so that
# inspection commands (list, clean --dry-run, show) are side-effect-free.
# Auto-cleanup still fires on every other forge command.
_SESSION_CLEANUP_EXEMPT = frozenset({"hook", "status-line", "logs", "session", "clean"})

_ALIASES: dict[str, str] = {
    "auth": "authentication",
    "ext": "extension",
    "extensions": "extension",  # backward compat (renamed from plural)
    "sess": "session",
}
# Display aliases: canonical -> preferred short alias (shown in help)
_DISPLAY_ALIASES: dict[str, str] = {
    "authentication": "auth",
    "extension": "ext",
    "session": "sess",
}


class AliasGroup(click.Group):
    """Click group that resolves short aliases to canonical command names."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        canonical = _ALIASES.get(cmd_name)
        if canonical:
            return super().get_command(ctx, canonical)
        return None

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Show aliases inline with command help text."""
        commands: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            help_text = cmd.get_short_help_str(limit=150)
            alias = _DISPLAY_ALIASES.get(subcommand)
            if alias:
                subcommand = f"{subcommand} ({alias})"
            commands.append((subcommand, help_text))

        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


def _process_pending_work_best_effort() -> None:
    """Process pending-work queue opportunistically.

    Best-effort: swallows all exceptions to avoid breaking CLI commands.
    Fast path: no-op if queue is empty.

    Handlers are assembled here (CLI assembly layer) and passed explicitly
    to avoid global registry coupling.
    """
    try:
        from forge.core.workqueue import Marker, WorkHandler, process_pending_work

        def _noop_stop_handler(marker: Marker) -> None:
            """No-op stop handler. Marker is deleted on success by the processor."""

        def _index_handler(marker: Marker) -> None:
            """Index a transcript for search.

            Extracts content, decomposes into three stores (metadata, BM25 index,
            content), then marks as indexed. All store operations are idempotent
            upserts, so work queue retries produce correct state.
            """
            from pathlib import Path

            from forge.search.bm25_store import BM25IndexStore
            from forge.search.content_store import ContentStore
            from forge.search.extractor import decompose_document, extract_document
            from forge.search.index_state import IndexStateStore
            from forge.search.store import SearchDocumentStore
            from forge.session.artifacts import resolve_forge_root

            payload = marker.payload
            worktree_path = Path(payload["worktree_path"])
            transcript_rel = payload["transcript_snapshot_rel"]

            marker_forge_root = payload.get("forge_root")
            forge_root = Path(marker_forge_root) if marker_forge_root else resolve_forge_root(worktree_path)
            transcript_abs = (forge_root / transcript_rel).resolve()

            # Validate path containment to prevent path traversal
            if not transcript_abs.is_relative_to(forge_root.resolve()):
                raise ValueError(f"Transcript path escapes forge root: {transcript_rel}")

            if not transcript_abs.is_file():
                raise FileNotFoundError(f"Transcript not found: {transcript_abs}")

            doc = extract_document(
                transcript_path=transcript_abs,
                session_name=payload["session_name"],
                session_id=payload["session_id"],
                worktree_path=str(worktree_path),
            )

            meta, term_freq, doc_len, content = decompose_document(doc)

            # Three idempotent upserts — safe for retries
            doc_store = SearchDocumentStore(forge_root=forge_root)
            doc_store.add(meta)

            bm25_store = BM25IndexStore(forge_root=forge_root)
            bm25_store.upsert_document(doc.transcript_path, term_freq, doc_len)

            content_store = ContentStore(forge_root=forge_root)
            content_store.add(doc.transcript_path, content)

            index_store = IndexStateStore(forge_root=forge_root)
            index_store.mark_indexed(transcript_abs)

        def _handoff_handler(marker: Marker) -> None:
            """Spawn a detached background process to run the handoff agent.

            The handler returns immediately (fast path for CLI startup).
            The actual handoff work happens in the background subprocess.

            Fire-and-forget: if the background process fails, the marker is
            already deleted. This is intentionally weaker reliability than
            indexing — acceptable because project-state.md is a convenience
            doc and the next session creates a new marker with fresh data.
            """
            import subprocess

            payload = marker.payload
            cmd = [
                "forge",
                "handoff",
                "run",
                "--session-name",
                payload["session_name"],
                "--worktree-path",
                payload["worktree_path"],
                "--transcript-rel",
                payload["transcript_snapshot_rel"],
            ]
            subprocess_proxy = payload.get("subprocess_proxy")
            if subprocess_proxy:
                cmd.extend(["--subprocess-proxy", subprocess_proxy])
            marker_forge_root = payload.get("forge_root")
            if marker_forge_root:
                cmd.extend(["--root", marker_forge_root])

            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

        handlers: dict[str, WorkHandler] = {
            "stop": _noop_stop_handler,
            "index": _index_handler,
            "handoff": _handoff_handler,
        }

        # Limit to 5 items per startup to avoid blocking CLI when many
        # index markers are pending (each involves file I/O + JSON parsing)
        process_pending_work(max_items=5, timeout_s=0.05, handlers=handlers)
    except Exception as e:
        logger.debug("Queue processing error (non-fatal): %s", e)


def _auto_clean_logs_best_effort() -> None:
    """Auto-prune old logs based on log_retention_days config.

    Best-effort: swallows all exceptions to avoid breaking CLI commands.
    """
    try:
        from forge.cli.logs import auto_clean_old_logs

        auto_clean_old_logs()
    except Exception as e:
        logger.debug("Log auto-cleanup error (non-fatal): %s", e)


def _auto_clean_sessions_best_effort() -> None:
    """Auto-prune old sessions based on session_retention_days config.

    Best-effort: swallows all exceptions to avoid breaking CLI commands.
    """
    try:
        from forge.session.cleanup import auto_clean_old_sessions

        auto_clean_old_sessions()
    except Exception as e:
        logger.debug("Session auto-cleanup error (non-fatal): %s", e)


@click.group(
    cls=AliasGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(None, "-V", "--version", package_name="tr-claude-forge", prog_name="forge")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Claude Forge - Enhanced session management for Claude Code.

    Forge provides named sessions, model routing, and workflow tooling
    for Claude Code development.
    """
    # Configure file logging for non-exempt subcommands.
    # Hooks configure their own logging (hooks/ subdirectory).
    # Status-line is exempt to avoid log spam (runs on every poll cycle).
    if ctx.invoked_subcommand not in _EXEMPT_SUBCOMMANDS:
        from forge.core.logging import configure_debug_logging

        configure_debug_logging(component=ctx.invoked_subcommand or "forge", subdirectory="cli")

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        return

    # Process pending-work queue opportunistically on CLI startup
    # Skip for exempt subcommands (hooks, status-line) to preserve low latency
    if ctx.invoked_subcommand not in _EXEMPT_SUBCOMMANDS:
        _process_pending_work_best_effort()
        _auto_clean_logs_best_effort()
    if ctx.invoked_subcommand not in _SESSION_CLEANUP_EXEMPT:
        _auto_clean_sessions_best_effort()


main.add_command(auth, name="authentication")
main.add_command(backend)
main.add_command(session)
main.add_command(proxy)
main.add_command(guard)
main.add_command(handoff)
main.add_command(claude)
main.add_command(config_cmd, name="config")
main.add_command(hooks)
main.add_command(extensions, name="extension")
main.add_command(status_line)
main.add_command(info_cmd, name="info")
main.add_command(workflow_cmd, name="workflow")
main.add_command(search_cmd, name="search")

from forge.cli.gc import clean_cmd  # noqa: E402
from forge.cli.logs import logs_cmd  # noqa: E402

main.add_command(clean_cmd, name="clean")
main.add_command(logs_cmd, name="logs")


if __name__ == "__main__":
    main()
