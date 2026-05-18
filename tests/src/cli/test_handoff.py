"""Tests for the hidden handoff CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from forge.cli.handoff import handoff
from forge.session.models import HandoffConfig, MemoryIntent, create_session_state
from forge.session.store import SessionStore


def _write_handoff_session(worktree: Path, *, subprocess_proxy: str | None = None) -> None:
    manifest = create_session_state("session")
    manifest.intent.subprocess_proxy = subprocess_proxy
    manifest.intent.memory = MemoryIntent(auto_update=HandoffConfig(enabled=True))
    SessionStore(str(worktree), "session").write(manifest)


def test_handoff_run_uses_manifest_subprocess_proxy(tmp_path: Path) -> None:
    """Detached handoff reads persisted subprocess proxy intent from the manifest."""
    _write_handoff_session(tmp_path, subprocess_proxy="openrouter-subprocess")

    with (
        patch("forge.session.handoff_agent.resolve_handoff_base_url", return_value="http://proxy") as mock_resolve,
        patch("forge.session.handoff_agent.run_handoff_agent", return_value=True),
    ):
        result = CliRunner().invoke(
            handoff,
            [
                "run",
                "--session-name",
                "session",
                "--worktree-path",
                str(tmp_path),
                "--transcript-rel",
                "transcript.jsonl",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_resolve.call_args.kwargs["subprocess_proxy"] == "openrouter-subprocess"


def test_handoff_run_prefers_marker_subprocess_proxy_snapshot(tmp_path: Path) -> None:
    """Stop-time marker proxy snapshot wins over later manifest edits."""
    _write_handoff_session(tmp_path, subprocess_proxy="manifest-proxy")

    with (
        patch("forge.session.handoff_agent.resolve_handoff_base_url", return_value="http://proxy") as mock_resolve,
        patch("forge.session.handoff_agent.run_handoff_agent", return_value=True),
    ):
        result = CliRunner().invoke(
            handoff,
            [
                "run",
                "--session-name",
                "session",
                "--worktree-path",
                str(tmp_path),
                "--transcript-rel",
                "transcript.jsonl",
                "--subprocess-proxy",
                "marker-proxy",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_resolve.call_args.kwargs["subprocess_proxy"] == "marker-proxy"
