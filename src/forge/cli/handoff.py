"""Handoff agent CLI commands.

Commands:
- forge handoff run: Execute the handoff agent for a session (background process)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

logger = logging.getLogger(__name__)


@click.group(hidden=True)
def handoff() -> None:
    """Manage handoff agent operations."""


@handoff.command("run")
@click.option("--session-name", required=True, help="Forge session name")
@click.option(
    "--worktree-path",
    required=True,
    type=click.Path(exists=True),
    help="Absolute path to the worktree",
)
@click.option(
    "--transcript-rel",
    required=True,
    help="Repo-relative path to transcript artifact",
)
@click.option("--timeout", default=None, type=int, help="Max seconds for agent to run")
@click.option("--subprocess-proxy", default=None, hidden=True, help="Stop-time subprocess proxy snapshot")
def run_cmd(
    session_name: str,
    worktree_path: str,
    transcript_rel: str,
    timeout: int | None,
    subprocess_proxy: str | None,
) -> None:
    """Run the handoff agent for a completed session.

    This is typically invoked by the work queue handler as a background process,
    not directly by users. It reads the session manifest, checks if handoff is
    enabled, and spawns claude -p to update project memory documents.
    """
    worktree = Path(worktree_path).resolve()

    # We use SessionStore directly (not resolve_session_store) because this
    # runs as a detached background process without FORGE_SESSION env var set.
    # The marker payload carries session_name explicitly.
    try:
        from forge.session.effective import compute_effective_intent
        from forge.session.store import SessionStore

        store = SessionStore(str(worktree), session_name)
        if not store.exists():
            logger.info("No session manifest for %s in %s", session_name, worktree)
            return

        manifest = store.read()
        effective = compute_effective_intent(manifest)
    except Exception as e:
        logger.warning("Failed to read session manifest for %s: %s", session_name, e)
        raise SystemExit(1)

    if not effective.memory or not effective.memory.auto_update:
        logger.info("Handoff not configured for session %s", session_name)
        return

    config = effective.memory.auto_update
    if not config.enabled:
        logger.info("Handoff disabled for session %s", session_name)
        return

    from forge.session.handoff_agent import resolve_handoff_base_url, run_handoff_agent

    confirmed_proxy_url = None
    if manifest.confirmed.started_with_proxy:
        confirmed_proxy_url = manifest.confirmed.started_with_proxy.base_url

    base_url = resolve_handoff_base_url(
        proxy_id=config.proxy,
        confirmed_proxy_base_url=confirmed_proxy_url,
        env_base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        direct=config.direct,
        subprocess_proxy=subprocess_proxy or effective.subprocess_proxy,
    )

    designated_docs = effective.memory.designated_docs if effective.memory else []

    success = run_handoff_agent(
        session_name=session_name,
        forge_root=worktree,
        transcript_snapshot_rel=transcript_rel,
        config=config,
        base_url=base_url,
        timeout_seconds=timeout,
        designated_docs=designated_docs,
    )

    if not success:
        raise SystemExit(1)
