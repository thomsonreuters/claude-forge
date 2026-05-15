"""Hook command entry points invoked by Claude Code.

Each function is a Click command registered on the ``hooks`` group.
Heavy logic is delegated to submodules (verification, direct_commands, policy).

CRITICAL: Always exit 0 on errors — don't break Claude.
Exception: WorktreeCreate exits 1 on failure (replaces Claude's default git behavior).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import click

from forge.core.state import FileLockTimeoutError, now_iso
from forge.core.workqueue import (
    enqueue_handoff_marker,
    enqueue_index_marker,
    enqueue_stop_marker,
)
from forge.session.artifacts import (
    get_artifact_paths,
    resolve_forge_root,
    safe_copy_file,
    snapshot_plan_approved,
)
from forge.session.effective import compute_effective_intent
from forge.session.hooks import (
    HookResult,
    handle_session_start,
    parse_hook_input,
    resolve_session_store,
)
from forge.session.store import HOOK_LOCK_TIMEOUT_S

from ._group import hooks
from ._helpers import (
    _append_artifact_entry,
    _find_latest_plan_from_transcript,
    _output_json,
    _output_result,
    _read_stdin_json,
)
from .direct_commands import (
    _handle_cmd_cancel_verification,
    _handle_cmd_clean,
    _handle_cmd_config,
    _handle_cmd_guard,
    _handle_cmd_help,
    _handle_cmd_plan,
    _handle_cmd_proxy,
    _handle_cmd_session,
    _parse_direct_command,
)
from .policy import (
    _build_action_context,
    _derive_policy_source_label,
    _persist_policy_state,
)
from .verification import _run_verification_check

logger = logging.getLogger(__name__)


@hooks.command(name="session-start")
@click.option(
    "--cwd",
    "-C",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Working directory (defaults to current directory)",
)
def session_start(cwd: Path | None) -> None:
    """Handle SessionStart hook from Claude Code.

    Reads JSON from stdin with session info, reconciles session state,
    and outputs JSON result to stdout.

    Expected stdin format:
    {"session_id": "...", "transcript_path": "...", "source": "startup|resume|compact|clear"}

    Always exits 0 to avoid breaking Claude. Errors are reported in JSON output.
    """
    if cwd is None:
        cwd = Path.cwd()

    data, err = _read_stdin_json()
    if data is None:
        message = "No input received on stdin" if err == "empty" else "Invalid JSON"
        result = HookResult(
            success=False,
            error="invalid_input",
            message=message,
        )
        _output_result(result)
        return
    logger.debug("session-start: session_id=%s", str(data.get("session_id", "?"))[:12])

    hook_input = parse_hook_input(data)
    if hook_input is None:
        result = HookResult(
            success=False,
            error="invalid_input",
            message="Missing or invalid required fields: session_id, transcript_path, source",
        )
        _output_result(result)
        return

    result = handle_session_start(hook_input, cwd)
    _output_result(result)


@hooks.command(name="plan-write")
def plan_write() -> None:
    """Record latest plan file path on PostToolUse:Write.

    This hook runs frequently (every Write), so it must be cheap:
    - If the file written is not a plan file, exit successfully with no-op.
    - If it is a plan file, store `confirmed.latest_plan_path` in the manifest.

    Expected stdin keys (best-effort; we tolerate extra fields):
    - hook_event_name: "PostToolUse"
    - tool_input.file_path: path written (may be absolute or worktree-relative)

    Always exits 0.
    """

    data, err = _read_stdin_json()
    if data is None:
        message = "empty stdin" if err == "empty" else "invalid JSON"
        _output_json({"success": False, "error": "invalid_input", "message": message})
        return
    logger.debug(
        "plan-write: event=%s tool=%s",
        data.get("hook_event_name"),
        data.get("tool_name"),
    )

    if data.get("hook_event_name") != "PostToolUse":
        _output_json({"success": True, "action": "skip", "reason": "wrong_event"})
        return

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        _output_json({"success": True, "action": "skip", "reason": "no_tool_input"})
        return

    # Some variants use file_path; keep a couple of fallbacks.
    file_path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(file_path, str) or not file_path:
        _output_json({"success": True, "action": "skip", "reason": "no_file_path"})
        return

    # Only record plan files
    if "/.claude/plans/" not in file_path and not file_path.startswith(".claude/plans/"):
        _output_json({"success": True, "action": "skip", "reason": "not_a_plan"})
        return

    cwd = Path.cwd().resolve()
    store = resolve_session_store(cwd, session_id=data.get("session_id"))
    if store is None:
        _output_json({"success": True, "action": "skip", "reason": "no_session"})
        return
    try:
        store.read()
    except Exception as e:
        _output_json({"success": False, "error": "manifest_read_failed", "message": str(e)})
        return

    # Normalize to worktree-relative if possible
    plan_path = Path(file_path)
    if plan_path.is_absolute():
        try:
            plan_path = plan_path.resolve().relative_to(cwd)
        except Exception:
            # If we can't make it relative, store as-is.
            pass

    try:

        def _mutate(m: object) -> None:
            # Type narrow via runtime checks.
            from forge.session.models import SessionState

            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            m.confirmed.latest_plan_path = str(plan_path)
            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:plan-write"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
    except FileLockTimeoutError:
        _output_json({"success": True, "action": "skip_lock_contended"})
        return
    except Exception as e:
        _output_json({"success": False, "error": "manifest_write_failed", "message": str(e)})
        return

    _output_json({"success": True, "action": "recorded", "latest_plan_path": str(plan_path)})


@hooks.command(name="exit-plan-mode")
def exit_plan_mode() -> None:
    """Capture an approved plan snapshot on PreToolUse:ExitPlanMode.

    This is treated as the plan approval boundary.

    Expected stdin keys (best-effort):
    - hook_event_name: "PreToolUse"
    - transcript_path: used only as fallback to locate the most recent plan file

    Always exits 0.
    """

    data, err = _read_stdin_json()
    if data is None:
        message = "empty stdin" if err == "empty" else "invalid JSON"
        _output_json({"success": False, "error": "invalid_input", "message": message})
        return
    logger.debug(
        "exit-plan-mode: event=%s tool=%s",
        data.get("hook_event_name"),
        data.get("tool_name"),
    )

    if data.get("hook_event_name") != "PreToolUse":
        _output_json({"success": True, "action": "skip", "reason": "wrong_event"})
        return

    cwd = Path.cwd().resolve()
    store = resolve_session_store(cwd, session_id=data.get("session_id"))
    if store is None:
        _output_json({"success": True, "action": "skip", "reason": "no_session"})
        return
    try:
        manifest = store.read()
    except Exception as e:
        _output_json({"success": False, "error": "manifest_read_failed", "message": str(e)})
        return

    source_plan_path: Path | None = None
    if manifest.confirmed.latest_plan_path:
        source_plan_path = cwd / manifest.confirmed.latest_plan_path

    # Fallback: scan transcript for last plan write (streaming; no full read).
    if source_plan_path is None or not source_plan_path.is_file():
        transcript_path = data.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            source_plan_path = _find_latest_plan_from_transcript(transcript_path, cwd)

    if source_plan_path is None or not source_plan_path.is_file():
        _output_json({"success": False, "error": "plan_not_found"})
        return

    # Compute artifact roots
    project_root = resolve_forge_root(cwd)
    paths = get_artifact_paths(project_root, manifest.name)

    try:
        snapshot_abs, snapshot_rel = snapshot_plan_approved(
            paths=paths,
            source_plan_path=source_plan_path,
        )
    except Exception as e:
        _output_json({"success": False, "error": "snapshot_failed", "message": str(e)})
        return

    source_plan_str = manifest.confirmed.latest_plan_path or str(source_plan_path)

    try:

        def _mutate(m: object) -> None:
            from forge.session.models import SessionState

            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            artifacts = m.confirmed.artifacts

            # Content-addressable snapshot_path makes re-approval of identical
            # content a no-op on disk. Dedupe the audit entry too, but keep the
            # most recently approved unique plan at the end of the list so
            # readers that use "last approved snapshot wins" still surface the
            # current approval after A->B->A.
            existing = artifacts.get("plans")
            new_snapshot_path = str(snapshot_rel)
            if isinstance(existing, list):
                artifacts["plans"] = [
                    entry
                    for entry in existing
                    if not (isinstance(entry, dict) and entry.get("snapshot_path") == new_snapshot_path)
                ]

            _append_artifact_entry(
                artifacts,
                kind="plans",
                entry={
                    "kind": "approved",
                    "captured_at": now_iso(),
                    "source_path": source_plan_str,
                    "snapshot_path": str(snapshot_rel),
                },
            )

            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:exit-plan-mode"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)

    except FileLockTimeoutError:
        _output_json({"success": True, "action": "skip_lock_contended"})
        return

    except Exception as e:
        _output_json({"success": False, "error": "manifest_write_failed", "message": str(e)})
        return

    _output_json({"success": True, "action": "snapshotted", "snapshot_path": str(snapshot_rel)})


@hooks.command(name="stop")
def stop() -> None:
    """Capture transcript copy on Stop, with optional verification policy.

    Expected stdin keys (best-effort):
    - hook_event_name: "Stop"
    - transcript_path
    - session_id

    Exit codes:
    - 0: Allow Stop (normal flow, or verification passed/disabled)
    - 2: Block Stop (verification failed)
    """

    data, err = _read_stdin_json()
    if data is None:
        message = "empty stdin" if err == "empty" else "invalid JSON"
        _output_json({"success": False, "error": "invalid_input", "message": message})
        return
    logger.debug("stop: session_id=%s", str(data.get("session_id", "?"))[:12])

    if data.get("hook_event_name") != "Stop":
        _output_json({"success": True, "action": "skip", "reason": "wrong_event"})
        return

    cwd = Path.cwd().resolve()
    pending_transcript_path: Path | None = None
    raw_transcript_path = data.get("transcript_path")
    if isinstance(raw_transcript_path, str) and raw_transcript_path:
        candidate = Path(raw_transcript_path)
        pending_transcript_path = candidate if candidate.is_absolute() else (cwd / candidate)
    incoming_session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None

    store = resolve_session_store(cwd, session_id=data.get("session_id"))
    if store is None:
        if pending_transcript_path is not None:
            _copy_transcript_to_pending_runs(pending_transcript_path, session_id=incoming_session_id)
        _output_json({"success": True, "action": "skip", "reason": "no_session"})
        return
    try:
        manifest = store.read()
    except Exception as e:
        _output_json({"success": False, "error": "manifest_read_failed", "message": str(e)})
        return

    transcript_path = data.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        transcript_path = manifest.confirmed.transcript_path

    if not transcript_path:
        _output_json({"success": False, "error": "missing_transcript_path"})
        return

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = manifest.confirmed.claude_session_id

    if not session_id:
        _output_json({"success": False, "error": "missing_session_id"})
        return

    project_root = resolve_forge_root(cwd)
    paths = get_artifact_paths(project_root, manifest.name)

    src = Path(transcript_path)
    dst_abs = paths.transcripts_abs / f"{session_id}.jsonl"
    dst_rel = paths.transcripts_rel / f"{session_id}.jsonl"

    try:
        # Claude can invoke Stop repeatedly for the same session UUID as the
        # transcript grows turn by turn. Refresh the UUID-named artifact so
        # search/index consumers see the latest snapshot instead of the first
        # turn that happened to be copied.
        copied = safe_copy_file(src, dst_abs, overwrite=True)
    except Exception as e:
        _output_json({"success": False, "error": "copy_failed", "message": str(e)})
        return

    # Copy transcript to pending run directories (QA/walkthrough artifacts)
    _copy_transcript_to_pending_runs(dst_abs, session_id=session_id)

    # Track manifest update outcome (but don't return early - we still want to enqueue)
    manifest_updated = True
    manifest_error: str | None = None

    try:

        def _mutate(m: object) -> None:
            from forge.session.models import SessionState

            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            artifacts = m.confirmed.artifacts
            _append_artifact_entry(
                artifacts,
                kind="transcripts",
                entry={
                    "captured_at": now_iso(),
                    "reason": "stop",
                    "source_path": transcript_path,
                    "session_id": session_id,
                    "copied_path": str(dst_rel),
                    "copied": copied,
                },
            )

            # Record policy provenance (always on Stop, per design §4.1.6)
            from forge import __version__
            from forge.session.models import PolicyConfirmed

            if m.confirmed.policy:
                m.confirmed.policy.forge_version = __version__
            else:
                m.confirmed.policy = PolicyConfirmed(forge_version=__version__)

            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:stop"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)

    except FileLockTimeoutError:
        manifest_updated = False
        manifest_error = "lock_contended"

    except Exception as e:
        manifest_updated = False
        manifest_error = str(e)

    # Run verification check (Ralph-Wiggum pattern)
    # This must happen AFTER transcript copy so we have the artifact to check
    # Re-read manifest to get latest state (may have been updated by artifact write)
    try:
        manifest = store.read()
    except Exception:
        pass  # Use stale manifest for verification - better than skipping

    should_allow_stop, block_message = _run_verification_check(
        store=store,
        manifest=manifest,
        transcript_path=dst_abs,  # Check the copied artifact, not the original
    )

    if not should_allow_stop:
        # Verification failed - block Stop
        # Do NOT enqueue pending-work since we're blocking
        click.echo(block_message, err=True)
        sys.exit(2)

    # Enqueue pending-work markers for deferred processing (best-effort)
    # Important: enqueue even if manifest update failed - the transcript artifact
    # exists on disk and deferred work should still be triggered.
    # Only enqueue if verification passed (we reach here only if should_allow_stop=True)
    queued_stop = (
        enqueue_stop_marker(
            session_id=session_id,
            worktree_path=cwd,
            session_name=manifest.name,
            transcript_snapshot_rel=str(dst_rel),
        )
        is not None
    )
    queued_index = (
        enqueue_index_marker(
            session_id=session_id,
            worktree_path=cwd,
            session_name=manifest.name,
            transcript_snapshot_rel=str(dst_rel),
        )
        is not None
    )

    # Enqueue handoff marker if auto_update is enabled (best-effort)
    queued_handoff = False
    try:
        effective = compute_effective_intent(manifest)
        if effective.memory and effective.memory.auto_update and effective.memory.auto_update.enabled:
            queued_handoff = (
                enqueue_handoff_marker(
                    session_id=session_id,
                    worktree_path=cwd,
                    session_name=manifest.name,
                    transcript_snapshot_rel=str(dst_rel),
                    subprocess_proxy=effective.subprocess_proxy,
                )
                is not None
            )
    except Exception:
        pass  # Best-effort: don't break stop hook on handoff enqueue failure

    if not manifest_updated:
        # Manifest failed but we still tried to enqueue
        _output_json(
            {
                "success": True,
                "action": "partial",
                "copied_path": str(dst_rel),
                "copied": copied,
                "manifest_updated": False,
                "manifest_error": manifest_error,
                "queued": queued_stop,
                "queued_index": queued_index,
                "queued_handoff": queued_handoff,
            }
        )
    else:
        _output_json(
            {
                "success": True,
                "action": "copied",
                "copied_path": str(dst_rel),
                "copied": copied,
                "queued": queued_stop,
                "queued_index": queued_index,
                "queued_handoff": queued_handoff,
            }
        )


@hooks.command(name="stop-failure")
def stop_failure() -> None:
    """Best-effort transcript capture on StopFailure.

    When Claude's stop fails (crash, timeout, etc.), this hook fires as a
    last-chance opportunity to capture the transcript and enqueue work markers.
    No verification is performed (the session is already in a failed state).

    Expected stdin keys (best-effort):
    - hook_event_name: "StopFailure"
    - transcript_path
    - session_id

    Always exits 0 (fail-open).
    """
    data, err = _read_stdin_json()
    if data is None:
        message = "empty stdin" if err == "empty" else "invalid JSON"
        _output_json({"success": False, "error": "invalid_input", "message": message})
        return
    logger.debug("stop-failure: session_id=%s", str(data.get("session_id", "?"))[:12])

    if data.get("hook_event_name") != "StopFailure":
        _output_json({"success": True, "action": "skip", "reason": "wrong_event"})
        return

    cwd = Path.cwd().resolve()

    # Best-effort transcript copy to pending runs even without a session
    pending_transcript_path: Path | None = None
    raw_transcript_path = data.get("transcript_path")
    if isinstance(raw_transcript_path, str) and raw_transcript_path:
        candidate = Path(raw_transcript_path)
        pending_transcript_path = candidate if candidate.is_absolute() else (cwd / candidate)
    incoming_session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None

    store = resolve_session_store(cwd, session_id=data.get("session_id"))
    if store is None:
        if pending_transcript_path is not None:
            _copy_transcript_to_pending_runs(pending_transcript_path, session_id=incoming_session_id)
        _output_json({"success": True, "action": "skip", "reason": "no_session"})
        return
    try:
        manifest = store.read()
    except Exception as e:
        _output_json({"success": False, "error": "manifest_read_failed", "message": str(e)})
        return

    transcript_path = data.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        transcript_path = manifest.confirmed.transcript_path
    if not transcript_path:
        _output_json({"success": True, "action": "skip", "reason": "no_transcript_path"})
        return

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = manifest.confirmed.claude_session_id
    if not session_id:
        _output_json({"success": True, "action": "skip", "reason": "no_session_id"})
        return

    project_root = resolve_forge_root(cwd)
    paths = get_artifact_paths(project_root, manifest.name)

    src = Path(transcript_path)
    dst_abs = paths.transcripts_abs / f"{session_id}.jsonl"
    dst_rel = paths.transcripts_rel / f"{session_id}.jsonl"

    try:
        # Keep the session artifact aligned with the latest transcript snapshot
        # here too; StopFailure can arrive after earlier Stop captures.
        copied = safe_copy_file(src, dst_abs, overwrite=True)
    except Exception:
        copied = False

    # Only fan out and enqueue if the artifact actually exists on disk.
    # Otherwise we'd consume pending-transcript markers and create index
    # markers that retry until poison handling kicks in.
    if dst_abs.is_file():
        _copy_transcript_to_pending_runs(dst_abs, session_id=session_id)

    # Best-effort manifest update
    try:

        def _mutate(m: object) -> None:
            from forge.session.models import SessionState

            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            artifacts = m.confirmed.artifacts
            _append_artifact_entry(
                artifacts,
                kind="transcripts",
                entry={
                    "captured_at": now_iso(),
                    "reason": "stop-failure",
                    "source_path": transcript_path,
                    "session_id": session_id,
                    "copied_path": str(dst_rel),
                    "copied": copied,
                },
            )

            from forge import __version__
            from forge.session.models import PolicyConfirmed

            if m.confirmed.policy:
                m.confirmed.policy.forge_version = __version__
            else:
                m.confirmed.policy = PolicyConfirmed(forge_version=__version__)

            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:stop-failure"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
    except Exception:
        pass  # Best-effort: never fail on StopFailure

    # Only enqueue work markers if the artifact exists on disk.
    # Enqueuing for a nonexistent artifact wastes retries until poison handling.
    queued_stop = False
    queued_index = False
    if dst_abs.is_file():
        queued_stop = (
            enqueue_stop_marker(
                session_id=session_id,
                worktree_path=cwd,
                session_name=manifest.name,
                transcript_snapshot_rel=str(dst_rel),
            )
            is not None
        )
        queued_index = (
            enqueue_index_marker(
                session_id=session_id,
                worktree_path=cwd,
                session_name=manifest.name,
                transcript_snapshot_rel=str(dst_rel),
            )
            is not None
        )

    _output_json(
        {
            "success": True,
            "action": "copied" if copied else "attempted",
            "copied_path": str(dst_rel),
            "copied": copied,
            "queued": queued_stop,
            "queued_index": queued_index,
        }
    )


@hooks.command(name="session-end")
def session_end() -> None:
    """No-op SessionEnd hook (placeholder).

    Claude Code suppresses SessionEnd hook stderr output
    (anthropics/claude-code#9090). The reconnect tip is printed from
    the parent launcher process instead. This command is kept so the
    hook config doesn't error on missing subcommand.
    """


@hooks.command(name="pre-compact")
def pre_compact() -> None:
    """Capture full transcript before compaction.

    Fires BEFORE compaction when the uncompacted transcript is still available.
    This is the canonical compaction snapshot — SessionStart rollover serves as
    fallback for /clear events and defense-in-depth.

    Always exits 0 (never blocks compaction). CLAUDE_CODE_AUTO_COMPACT_WINDOW
    handles compaction window sizing in proxy mode.
    """
    data, err = _read_stdin_json()
    if data is None:
        sys.exit(0)
    logger.debug("pre-compact: hook_event_name=%s", data.get("hook_event_name"))

    transcript_path = data.get("transcript_path")
    session_id = data.get("session_id")
    cwd_str = data.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    if not transcript_path or not session_id:
        sys.exit(0)

    try:
        store = resolve_session_store(cwd, session_id=session_id)
        if store is None or not store.exists():
            sys.exit(0)

        manifest = store.read()
        if manifest is None:
            sys.exit(0)

        project_root = resolve_forge_root(cwd)
        paths = get_artifact_paths(project_root, manifest.name)

        src = Path(transcript_path)
        timestamp = now_iso().replace(":", "-")
        snapshot_name = f"{session_id}_pre-compact_{timestamp}.jsonl"
        dst_abs = paths.transcripts_abs / snapshot_name
        dst_rel = paths.transcripts_rel / snapshot_name

        copied = safe_copy_file(src, dst_abs, overwrite=False)

        from forge.session.models import CompactionConfirmed, SessionState

        def _mutate(m: object) -> None:
            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            if m.confirmed.compaction is None:
                m.confirmed.compaction = CompactionConfirmed()

            m.confirmed.compaction.compact_count += 1

            _append_artifact_entry(
                m.confirmed.artifacts,
                kind="transcripts",
                entry={
                    "captured_at": now_iso(),
                    "reason": "pre-compact",
                    "source_path": transcript_path,
                    "snapshot_path": str(dst_rel),
                    "copied": copied,
                },
            )
            m.confirmed.compaction.transcript_snapshots.append(
                {
                    "captured_at": now_iso(),
                    "reason": "pre-compact",
                    "source_path": transcript_path,
                    "snapshot_path": str(dst_rel),
                    "copied": copied,
                }
            )
            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:pre-compact"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
        logger.debug("pre-compact: transcript snapshot captured at %s", dst_rel)
    except Exception as e:
        # Fail-open: never block compaction
        logger.debug("pre-compact: snapshot failed: %s", e)

    sys.exit(0)


@hooks.command(name="post-compact")
def post_compact() -> None:
    """Record compaction event in session confirmed state.

    Fires AFTER compaction. Updates last_compact_at and last_compact_type.
    compact_count is incremented by PreCompact (before compaction) so this
    hook only records the completion timestamp.

    Side-effect only -- cannot block compaction.
    """
    data, err = _read_stdin_json()
    if data is None:
        sys.exit(0)
    logger.debug("post-compact: hook_event_name=%s", data.get("hook_event_name"))

    session_id = data.get("session_id")
    cwd_str = data.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()
    # PostCompact supports the same matcher values as PreCompact: "auto" | "manual"
    compact_trigger = data.get("trigger", "unknown")

    if not session_id:
        sys.exit(0)

    store = resolve_session_store(cwd, session_id=session_id)
    if store is None or not store.exists():
        sys.exit(0)

    try:
        from forge.session.models import CompactionConfirmed, SessionState

        def _mutate(m: object) -> None:
            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            if m.confirmed.compaction is None:
                m.confirmed.compaction = CompactionConfirmed()

            m.confirmed.compaction.last_compact_at = now_iso()
            m.confirmed.compaction.last_compact_type = compact_trigger
            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:post-compact"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
        logger.debug("post-compact: compaction metadata recorded (trigger=%s)", compact_trigger)
    except Exception as e:
        logger.debug("post-compact: state update failed: %s", e)

    sys.exit(0)


@hooks.command(name="worktree-create")
def worktree_create() -> None:
    """Create worktree with Forge extensions auto-installed.

    REPLACES Claude Code's default worktree creation and .worktreeinclude
    handling. Prints absolute worktree path to stdout on success.
    Non-zero exit fails worktree creation.

    stdout contract: ONLY the absolute worktree path. All debug goes to stderr.
    """
    data, err = _read_stdin_json()
    if data is None:
        # Can't parse input — let Claude Code's default fail gracefully
        sys.exit(1)

    cwd_str = data.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    # Claude Code may provide a name slug for the worktree (used by both
    # --worktree and isolation: "worktree" for subagents). Each request
    # must produce a distinct checkout — do NOT collapse onto session_id.
    hook_name = data.get("name", "")

    try:
        import subprocess
        import uuid as _uuid

        from forge.session.worktree.create import (
            find_git_binary,
            get_main_repo_root,
            resolve_worktree_path,
        )

        # Use main-repo root to avoid child-worktree resolution bugs
        repo_root = get_main_repo_root(cwd)
        git = find_git_binary()

        # Prefer hook-provided name; fall back to unique name per request
        if hook_name:
            wt_name = hook_name
        else:
            short_uuid = _uuid.uuid4().hex[:8]
            wt_name = f"wt-{short_uuid}"
        branch_name = f"forge/{wt_name}"

        worktree_path = resolve_worktree_path(repo_root, wt_name)

        result = subprocess.run(
            [git, "worktree", "add", str(worktree_path), "-b", branch_name],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Fallback: try without -b (use detached HEAD)
            logger.debug("worktree-create: branched creation failed: %s", result.stderr.strip())
            result = subprocess.run(
                [git, "worktree", "add", str(worktree_path)],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.debug("worktree-create: fallback also failed: %s", result.stderr.strip())
                sys.exit(1)

        # Best-effort: copy runtime config (.env, .claude/settings.local.json,
        # etc.) before installing extensions so the installer merges on top
        # of existing user settings rather than creating a fresh file.
        # Use the current checkout root (not main repo) so child worktrees
        # inherit config from the checkout the user is actually in.
        try:
            from forge.session.worktree.config_copy import copy_runtime_config
            from forge.session.worktree.create import get_repo_root

            source_root = get_repo_root(cwd)
            copy_runtime_config(source_root, worktree_path)
            logger.debug("worktree-create: runtime config copied to %s", worktree_path)
        except Exception as cfg_err:
            logger.debug("worktree-create: config copy failed: %s", cfg_err)

        # Best-effort: install Forge extensions in the new worktree.
        # Suppress stdout to protect the path-only stdout contract.
        try:
            import contextlib
            import os as _os

            from forge.install.installer import Installer
            from forge.install.models import InstallMode, InstallProfile, InstallScope

            with open(_os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
                installer = Installer(
                    scope=InstallScope.LOCAL,
                    project_root=worktree_path,
                )
                installer.init(
                    profile=InstallProfile.STANDARD,
                    mode=InstallMode.COPY,
                )
            logger.debug("worktree-create: extensions installed in %s", worktree_path)
        except Exception as ext_err:
            # Non-fatal: worktree works without Forge extensions
            logger.debug("worktree-create: extension install failed: %s", ext_err)

        # stdout contract: absolute path only
        print(str(worktree_path.resolve()))
        sys.exit(0)

    except Exception as e:
        logger.debug("worktree-create: failed: %s", e)
        sys.exit(1)


@hooks.command(name="subagent-stop")
def subagent_stop() -> None:
    """Track subagent completion in session confirmed state.

    Records agent type, count, transcript path, and message preview.
    Observe-only -- always exits 0. Blocking support preserved
    for future policy enforcement via exit 2.

    Expected stdin keys:
    - session_id, cwd, agent_id, agent_type
    - agent_transcript_path, last_assistant_message
    """
    data, err = _read_stdin_json()
    if data is None:
        sys.exit(0)
    logger.debug("subagent-stop: agent_type=%s", data.get("agent_type"))

    session_id = data.get("session_id")
    cwd_str = data.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    if not session_id:
        sys.exit(0)

    store = resolve_session_store(cwd, session_id=session_id)
    if store is None or not store.exists():
        sys.exit(0)

    agent_id = data.get("agent_id")
    agent_type = data.get("agent_type", "unknown")
    agent_transcript_path = data.get("agent_transcript_path")
    last_message = data.get("last_assistant_message")

    try:
        from forge.session.models import SessionState, SubagentConfirmed

        def _mutate(m: object) -> None:
            if not isinstance(m, SessionState):
                raise TypeError(f"Expected SessionState, got {type(m)}")

            if m.confirmed.subagents is None:
                m.confirmed.subagents = SubagentConfirmed()

            sa = m.confirmed.subagents
            sa.total_count += 1
            sa.by_type[agent_type] = sa.by_type.get(agent_type, 0) + 1
            sa.last_agent_id = agent_id
            sa.last_agent_type = agent_type
            sa.last_stop_at = now_iso()
            sa.last_transcript_path = agent_transcript_path
            sa.last_message_preview = last_message[:200] if last_message else None
            m.confirmed.confirmed_at = now_iso()
            m.confirmed.confirmed_by = "hook:subagent-stop"

        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
        logger.debug("subagent-stop: recorded agent_type=%s total=%s", agent_type, "ok")
    except Exception as e:
        logger.debug("subagent-stop: state update failed: %s", e)

    sys.exit(0)


@hooks.command(name="policy-check")
def policy_check() -> None:
    """Evaluate policies on PreToolUse:Write/Edit.

    This hook enforces policy rules at tool invocation boundaries.
    Deterministic policies (TDD, coding standards) run synchronously.
    Semantic policies (supervisor) may invoke an LLM.

    Exit codes:
    - 0: Allow (continue with tool use)
    - 2: Block (display stderr message to user, abort tool use)

    Always defaults to fail-open on internal errors.
    """
    data, err = _read_stdin_json()
    if data is None:
        # No input or parse error = allow (fail-open)
        sys.exit(0)
    logger.debug(
        "policy-check: event=%s tool=%s session=%s",
        data.get("hook_event_name"),
        data.get("tool_name"),
        str(data.get("session_id", "?"))[:12],
    )

    if data.get("hook_event_name") != "PreToolUse":
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name not in ("Write", "Edit"):
        sys.exit(0)

    cwd = Path.cwd().resolve()
    store = resolve_session_store(cwd, session_id=data.get("session_id"))
    if store is None:
        _output_json({"success": True, "action": "skip", "reason": "no_session"})
        return
    try:
        manifest = store.read()
    except Exception as e:
        print(f"[forge] Policy check: cannot read session manifest: {e}", file=sys.stderr)
        sys.exit(0)

    try:
        effective = compute_effective_intent(manifest)
    except Exception as e:
        print(
            f"[forge] Policy check: cannot compute effective intent: {e}",
            file=sys.stderr,
        )
        sys.exit(0)

    if not effective.policy or not effective.policy.enabled:
        sys.exit(0)

    context = _build_action_context(data, tool_name, manifest)
    if context is None:
        print("[forge] Policy check: cannot build action context", file=sys.stderr)
        sys.exit(0)

    from forge.guard.engine import build_engine
    from forge.guard.types import FailMode

    fail_mode: FailMode = effective.policy.fail_mode or "open"
    bundles = effective.policy.bundles or []
    sup = effective.policy.supervisor if effective.policy else None
    has_supervisor = bool(sup and sup.resume_id and not sup.suspended)

    if not bundles and not has_supervisor:
        sys.exit(0)

    bundle_config: dict[str, dict[str, Any]] = {}
    if effective.policy and effective.policy.bundle_config:
        bundle_config = effective.policy.bundle_config

    try:
        engine = build_engine(bundles, fail_mode=fail_mode, bundle_config=bundle_config or None)
    except Exception as e:
        print(f"[forge] Policy check: cannot build engine: {e}", file=sys.stderr)
        sys.exit(0)

    # Register semantic supervisor before restore_state so cached state is restored with it.
    if has_supervisor:
        from forge.guard.semantic.supervisor import SemanticSupervisorPolicy

        supervisor_policy = SemanticSupervisorPolicy(config=effective.policy.supervisor)
        engine.register(supervisor_policy)

    existing_policy_state = None
    if manifest.confirmed.policy:
        existing_policy_state = manifest.confirmed.policy.policy_states
    engine.restore_state(existing_policy_state)

    import time

    t0 = time.monotonic()
    try:
        result = engine.evaluate(context)
    except Exception as e:
        if fail_mode == "closed":
            print(f"Policy evaluation failed (fail-closed): {e}", file=sys.stderr)
            sys.exit(2)
        # fail-open: allow on evaluation error
        sys.exit(0)
    elapsed = time.monotonic() - t0

    target_label = f"{tool_name}:{context.target_path}" if context.target_path else tool_name

    try:
        _persist_policy_state(
            store=store,
            engine=engine,
            result=result,
            effective=effective,
            context_summary=target_label,
        )
    except Exception as e:
        print(f"[forge] Policy state persistence failed: {e}", file=sys.stderr)

    from forge.runtime_config import get_runtime_config

    show_summary = get_runtime_config().policy_summary_feedback == "on"
    is_cached = any(getattr(d, "cached", False) for d in result.decisions)
    cache_label = ", cached" if is_cached else ""
    source_label = _derive_policy_source_label(result, effective)

    if result.final_decision == "deny":
        lines = ["Policy violation(s):"]
        for d in result.decisions:
            if d.decision != "deny":
                continue
            for i, v in enumerate(d.violations):
                lines.append(f"  [{v.rule_id}] {v.message}")
                if d.intent and i == 0:
                    lines.append(f"    Intent: {d.intent}")
                if v.suggested_fix:
                    lines.append(f"    Fix: {v.suggested_fix}")
            lines.append(
                "    Note: This policy was configured by the project owner. First"
                " try a compliant approach that satisfies the intent above. If the"
                " user's request cannot be fulfilled without violating the intent,"
                " explain the conflict and ask how to proceed. Do not attempt"
                " bypasses that pass the check but defeat the goal."
            )

        print("\n".join(lines), file=sys.stderr)
        if show_summary:
            violation_count = sum(len(d.violations) for d in result.decisions if d.decision == "deny")
            if violation_count > 0:
                print(
                    f"[forge] Policy: checked {target_label} against {source_label}"
                    f" ({violation_count} violation{'s' if violation_count != 1 else ''}, blocked, {elapsed:.1f}s)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[forge] Policy: checked {target_label} against {source_label}"
                    f" (blocked, evaluation error, {elapsed:.1f}s)",
                    file=sys.stderr,
                )
        sys.exit(2)

    if result.final_decision == "needs_review":
        lines = ["Policy review required but no semantic supervisor resolved it:"]
        for d in result.decisions:
            if d.decision == "needs_review":
                lines.append(f"  [{d.policy_id}] requested review")
                if d.intent:
                    lines.append(f"    Intent: {d.intent}")
        lines.append(
            "    Configure a supervisor for this session or ask the user how to proceed before making this change."
        )
        print("\n".join(lines), file=sys.stderr)
        if show_summary:
            print(
                f"[forge] Policy: checked {target_label} against {source_label}"
                f" (review required, unresolved, {elapsed:.1f}s)",
                file=sys.stderr,
            )
        sys.exit(2)

    # Surface warnings before allowing (deduped to avoid spam) -- always visible
    if result.all_warnings:
        seen: set[str] = set()
        for warning in result.all_warnings:
            if warning not in seen:
                seen.add(warning)
                print(f"[forge] Policy warning: {warning}", file=sys.stderr)

    if show_summary:
        if result.final_decision == "allow" and not result.all_warnings:
            verdict = "aligned"
        elif result.final_decision == "allow" and result.all_warnings:
            deduped_count = len(set(result.all_warnings))
            verdict = f"allowed, {deduped_count} warning{'s' if deduped_count != 1 else ''}"
        else:
            verdict = result.final_decision
        print(
            f"[forge] Policy: checked {target_label} against {source_label}"
            f" ({verdict}{cache_label}, {elapsed:.1f}s)",
            file=sys.stderr,
        )
        _output_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": (
                        f"Forge policy: {target_label} checked against {source_label}"
                        f" ({verdict}{cache_label}, {elapsed:.1f}s)"
                    ),
                }
            }
        )
    sys.exit(0)


@hooks.command(name="user-prompt-submit")
def user_prompt_submit() -> None:
    """Dispatch direct user commands from UserPromptSubmit.

    Design goal: install a single hook once, then add new `%<cmd>` handlers over time
    without requiring hook reinstalls.

    This handler follows Claude Code's decision contract for UserPromptSubmit:
    - If we handle a command: print `{ "decision": "block", "reason": "..." }`
    - Otherwise: exit 0 with no output (normal Claude flow)
    """

    data, err = _read_stdin_json()
    if data is None:
        # Don't break Claude for UserPromptSubmit; just no-op.
        return
    prompt = data.get("prompt")
    logger.debug(
        "user-prompt-submit: prompt_len=%d",
        len(prompt) if isinstance(prompt, str) else 0,
    )
    if not isinstance(prompt, str) or not prompt.strip().startswith("%"):
        return

    parsed = _parse_direct_command(prompt)
    if parsed is None:
        return

    cmd, args = parsed

    if cmd in ("h", "help"):
        _handle_cmd_help()
        return

    # Shared commands (mirror CLI syntax)
    if cmd == "session":
        _handle_cmd_session(data, args)
        return

    if cmd == "proxy":
        _handle_cmd_proxy(data, args)
        return

    if cmd == "plan":
        _handle_cmd_plan(args)
        return

    if cmd == "guard":
        _handle_cmd_guard(data, args)
        return

    if cmd == "config":
        _handle_cmd_config(data, args)
        return

    if cmd == "cancel-verification":
        _handle_cmd_cancel_verification()
        return

    if cmd == "clean":
        _handle_cmd_clean(args)
        return

    # Unknown %command: ignore for now (future expansion point).
    return


# --- Run Artifact Helpers ---

# Marker locations for skills that want a transcript copy in their run directory.
_PENDING_TRANSCRIPT_MARKERS = ("manual-testing/qa", "manual-testing/walkthrough")


@dataclass(frozen=True)
class _PendingTranscriptRequest:
    """Validated pending-transcript marker payload."""

    run_dir: Path
    session_id: str | None = None
    transcript_contains: str | None = None


def _load_pending_transcript_request(marker: Path, *, expected_prefix: Path) -> _PendingTranscriptRequest | None:
    """Parse a pending-transcript marker.

    Expected format:
    {"run_dir": "...", "session_id": "...", "transcript_contains": "..."}

    Returns:
        Validated request, or None if the marker is malformed / unsafe.
    """
    raw = marker.read_text(encoding="utf-8").strip()
    if not raw:
        logger.warning("Empty .pending-transcript marker: %s", marker)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in .pending-transcript marker %s: %s", marker, e)
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "Invalid .pending-transcript marker payload (expected object): %s",
            marker,
        )
        return None

    run_dir_value = payload.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value.strip():
        logger.warning("Structured .pending-transcript marker missing run_dir: %s", marker)
        return None
    run_dir_str = run_dir_value.strip()

    expected_session_id: str | None = None
    session_id_value = payload.get("session_id")
    if session_id_value is not None:
        if not isinstance(session_id_value, str):
            logger.warning(
                "Structured .pending-transcript marker has invalid session_id: %s",
                marker,
            )
            return None
        expected_session_id = session_id_value.strip() or None

    transcript_contains: str | None = None
    transcript_value = payload.get("transcript_contains")
    if transcript_value is not None:
        if not isinstance(transcript_value, str):
            logger.warning(
                "Structured .pending-transcript marker has invalid transcript_contains: %s",
                marker,
            )
            return None
        transcript_contains = transcript_value.strip() or None

    run_dir = Path(run_dir_str)
    if not run_dir.is_absolute():
        logger.warning("Rejected .pending-transcript: relative path %s", run_dir)
        return None

    try:
        run_dir.resolve().relative_to(expected_prefix)
    except ValueError:
        logger.warning(
            "Rejected .pending-transcript: %s is not under %s",
            run_dir,
            expected_prefix,
        )
        return None

    if not run_dir.is_dir():
        logger.warning("Run directory does not exist: %s", run_dir)
        return None

    return _PendingTranscriptRequest(
        run_dir=run_dir,
        session_id=expected_session_id,
        transcript_contains=transcript_contains,
    )


def _transcript_contains_text(transcript_path: Path, text: str) -> bool:
    """Return True if the transcript file contains the given text."""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if text in line:
                    return True
    except OSError as e:
        logger.warning(
            "Failed to scan transcript %s for pending marker text: %s",
            transcript_path,
            e,
        )
        return False
    return False


def _copy_transcript_to_pending_runs(transcript_path: Path, *, session_id: str | None = None) -> None:
    """Copy transcript to pending skill run directories (best-effort).

    QA and walkthrough skills write a `.pending-transcript` marker containing
    a structured JSON payload with additional match guards. This function copies
    the transcript there and removes the marker once the current Stop event
    satisfies those guards.

    Never raises -- failures are logged and swallowed to avoid blocking Stop.
    """
    from forge.core.paths import get_forge_home

    try:
        forge_home = get_forge_home()
    except Exception:
        return

    for skill in _PENDING_TRANSCRIPT_MARKERS:
        marker = forge_home / skill / ".pending-transcript"
        if not marker.is_file():
            continue

        try:
            expected_prefix = (forge_home / skill / "runs").resolve()
            request = _load_pending_transcript_request(marker, expected_prefix=expected_prefix)
            if request is None:
                marker.unlink(missing_ok=True)
                continue

            if request.session_id and request.session_id != session_id:
                logger.debug(
                    "Pending transcript marker %s waiting for session %s (got %s)",
                    marker,
                    request.session_id,
                    session_id,
                )
                continue

            if request.transcript_contains and not _transcript_contains_text(
                transcript_path, request.transcript_contains
            ):
                logger.debug(
                    "Pending transcript marker %s waiting for transcript token match",
                    marker,
                )
                continue

            safe_copy_file(transcript_path, request.run_dir / "transcript.jsonl", overwrite=False)
            marker.unlink(missing_ok=True)
            logger.debug("Copied transcript to run dir: %s", request.run_dir)

        except Exception as e:
            logger.warning("Failed to process .pending-transcript %s: %s", marker, e)
            try:
                marker.unlink(missing_ok=True)
            except Exception:
                pass


# --- Team Hook Handlers ---


@hooks.command(name="teammate-idle")
def teammate_idle() -> None:
    """Handle TeammateIdle hook from Claude Code.

    Exit 0: allow teammate to go idle.
    Exit 2: teammate continues working (stderr = feedback).
    """
    data, err = _read_stdin_json()
    if data is None:
        sys.exit(0)
    logger.debug("teammate-idle: session=%s", str(data.get("session_id", "?"))[:12])

    try:
        cwd = Path.cwd().resolve()
        store = resolve_session_store(cwd, session_id=data.get("session_id"))
        if store is None:
            sys.exit(0)
        manifest = store.read()
        effective = compute_effective_intent(manifest)
    except Exception:
        sys.exit(0)

    config = effective.policy.team_supervisor if effective.policy else None
    if not config or not config.enabled:
        sys.exit(0)

    from forge.guard.team.handlers import handle_teammate_idle

    cache_key = _safe_cache_key(data.get("session_id"))
    exit_code, feedback = _run_team_handler(cache_key, lambda cache: handle_teammate_idle(data, config, cache))
    if exit_code == 2 and feedback:
        print(feedback, file=sys.stderr)
    sys.exit(exit_code)


@hooks.command(name="task-completed")
def task_completed() -> None:
    """Handle TaskCompleted hook from Claude Code.

    Exit 0: task marked as completed.
    Exit 2: task stays open (stderr = feedback to teammate).
    """
    data, err = _read_stdin_json()
    if data is None:
        sys.exit(0)
    logger.debug("task-completed: session=%s", str(data.get("session_id", "?"))[:12])

    try:
        cwd = Path.cwd().resolve()
        store = resolve_session_store(cwd, session_id=data.get("session_id"))
        if store is None:
            sys.exit(0)
        manifest = store.read()
        effective = compute_effective_intent(manifest)
    except Exception:
        sys.exit(0)

    config = effective.policy.team_supervisor if effective.policy else None
    if not config or not config.enabled:
        sys.exit(0)

    from forge.guard.team.handlers import handle_task_completed

    cache_key = _safe_cache_key(data.get("session_id"))
    exit_code, feedback = _run_team_handler(cache_key, lambda cache: handle_task_completed(data, config, cache))
    if exit_code == 2 and feedback:
        print(feedback, file=sys.stderr)
    sys.exit(exit_code)


@hooks.command(name="read-hygiene")
def read_hygiene_cmd() -> None:
    """Strip extra Read params from skill instruction file reads.

    Targets skill instruction files ({mode}.md, {mode}-{family}.md) that have
    a strict "file_path only" Read contract. Uses updatedInput to silently fix
    the call. Always exits 0 (fail-open).
    """
    data, err = _read_stdin_json()
    if data is None:
        sys.exit(0)

    try:
        from .read_hygiene import handle_read_hygiene

        result = handle_read_hygiene(data)
        if result is not None:
            _output_json(result)
    except Exception:
        logger.debug("read-hygiene: unexpected error", exc_info=True)
    sys.exit(0)


_SAFE_CACHE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# Short lock timeout for team hooks (concurrent teammates)
_TEAM_CACHE_LOCK_TIMEOUT_S = 0.2


def _safe_cache_key(session_id: Any) -> str:
    """Sanitize session_id for use as a cache filename.

    Rejects path traversal chars (same pattern as workqueue SAFE_MARKER_ID).
    Falls back to 'default' on None, empty, or unsafe values.
    """
    if not session_id or not isinstance(session_id, str):
        return "default"
    if not _SAFE_CACHE_ID.match(session_id):
        return "default"
    return session_id


def _team_cache_path(cache_key: str) -> Path:
    """Return the file path for a team hook cache."""
    from forge.core.paths import get_forge_home

    return get_forge_home() / "team-hooks" / f"{cache_key}.json"


def _run_team_handler(
    cache_key: str,
    handler: Callable[[dict], tuple[int, str]],
) -> tuple[int, str]:
    """Run a team handler with locked file-backed cache.

    Holds a file lock around the read → handler → write cycle to prevent
    lost updates from concurrent teammate hooks.
    """
    from forge.core.state import (
        FileLockTimeoutError,
        atomic_write_json,
        file_lock_for_target,
        read_json,
    )

    cache_path = _team_cache_path(cache_key)

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock_for_target(target_path=cache_path, timeout_s=_TEAM_CACHE_LOCK_TIMEOUT_S):
            cache: dict = {}
            if cache_path.exists():
                try:
                    cache = read_json(cache_path)
                except Exception:
                    cache = {}

            exit_code, feedback = handler(cache)

            if cache:
                atomic_write_json(cache_path, cache, create_parents=True)

            return exit_code, feedback

    except FileLockTimeoutError:
        # Another hook has the lock — run without cache (best-effort)
        return handler({})
    except Exception:
        # Any other I/O error — run without cache
        return handler({})
