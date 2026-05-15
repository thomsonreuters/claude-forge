"""Async work queue for deferred processing.

A general-purpose, file-based queue for work items that producers enqueue
and CLI startup processes opportunistically.

Design goals:
- Best-effort semantics: enqueue failures are non-fatal, processing is opportunistic
- Fast path: no-op when queue is empty (cheap directory scan)
- Concurrent-safe: per-marker advisory locks prevent corruption
- Exactly-once-ish: markers deleted on successful processing
- Poison marker protection: markers exceeding MAX_ATTEMPTS moved to failed/

Queue location: ~/.forge/pending-work/ (respects FORGE_HOME)

Marker schema:
    {
        "schema_version": 1,
        "kind": "stop",
        "marker_id": "uuid-123",
        "forge_version": "<from forge.__version__>",
        "created_at": "2026-01-07T12:00:00Z",
        "payload": {
            "session_id": "...",
            "worktree_path": "/abs/path",
            ...
        },
        "attempt_count": 0,
        "last_attempt_at": null,
        "last_error": null
    }
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from forge import __version__
from forge.core.paths import get_forge_home
from forge.core.state import (
    FileLockTimeoutError,
    atomic_write_json,
    file_lock_for_target,
    now_iso,
    read_json,
)

from .types import (
    FAILED_WORK_DIR,
    MARKER_SCHEMA_VERSION,
    MAX_ATTEMPTS,
    MAX_ERROR_LENGTH,
    PENDING_WORK_DIR,
    Marker,
    ProcessResult,
    WorkHandler,
)

logger = logging.getLogger(__name__)

# Lock timeouts
MARKER_LOCK_TIMEOUT_S = 0.1  # 100ms for hooks (must be fast)
PROCESSOR_LOCK_TIMEOUT_S = 0.05  # 50ms per marker during startup processing

# Regex for safe marker IDs (prevents path traversal)
# Allow alphanumeric, hyphens, underscores, dots (typical UUID/session ID chars)
SAFE_MARKER_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def pending_work_dir() -> Path:
    """Get the pending-work queue directory.

    Returns:
        Path to ~/.forge/pending-work/ (respects FORGE_HOME).
    """
    return get_forge_home() / PENDING_WORK_DIR


def _failed_work_dir() -> Path:
    """Get the failed-work directory for poison markers.

    Returns:
        Path to ~/.forge/pending-work/failed/ (respects FORGE_HOME).
    """
    return get_forge_home() / FAILED_WORK_DIR


def marker_path(marker_id: str) -> Path:
    """Get the marker file path for a marker ID.

    Args:
        marker_id: The marker identifier (used as filename).

    Returns:
        Path to <pending_work_dir>/<marker_id>.json

    Raises:
        ValueError: If marker_id is invalid (empty, contains path traversal chars).
    """
    if not marker_id or not SAFE_MARKER_ID.match(marker_id):
        raise ValueError(f"Invalid marker_id: {marker_id!r}")

    return pending_work_dir() / f"{marker_id}.json"


def enqueue(
    *,
    kind: str,
    marker_id: str,
    payload: dict[str, Any],
) -> Path | None:
    """Enqueue a work marker for deferred processing.

    Best-effort semantics: returns marker path on success, None on failure.
    Failures are logged but not raised.

    Args:
        kind: The marker kind (determines which handler processes it).
        marker_id: The marker identifier (used as filename; must be a safe filename).
        payload: Kind-specific data to include in the marker.

    Returns:
        Path to created marker, or None if enqueue failed.
    """
    try:
        path = marker_path(marker_id)
    except ValueError as e:
        logger.warning("Failed to enqueue marker: %s", e)
        return None

    marker_data: dict[str, Any] = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "kind": kind,
        "marker_id": marker_id,
        "forge_version": __version__,
        "created_at": now_iso(),
        "payload": payload,
        "attempt_count": 0,
        "last_attempt_at": None,
        "last_error": None,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        with file_lock_for_target(target_path=path, timeout_s=MARKER_LOCK_TIMEOUT_S):
            atomic_write_json(path, marker_data)

        logger.debug("Enqueued %s marker: %s", kind, path)
        return path

    except FileLockTimeoutError:
        logger.warning("Lock contention while enqueuing marker for %s", marker_id)
        return None
    except Exception as e:
        logger.warning("Failed to enqueue %s marker for %s: %s", kind, marker_id, e)
        return None


def enqueue_stop_marker(
    *,
    session_id: str,
    worktree_path: Path,
    session_name: str,
    transcript_snapshot_rel: str,
) -> Path | None:
    """Enqueue a Stop marker for deferred processing.

    Convenience wrapper around enqueue() for stop hook callers.

    Args:
        session_id: The Claude session ID (used as marker_id).
        worktree_path: Absolute path to the worktree.
        session_name: Forge session name.
        transcript_snapshot_rel: Repo-relative path to transcript artifact.

    Returns:
        Path to created marker, or None if enqueue failed.
    """
    return enqueue(
        kind="stop",
        marker_id=session_id,
        payload={
            "session_id": session_id,
            "worktree_path": str(worktree_path),
            "session_name": session_name,
            "transcript_snapshot_rel": transcript_snapshot_rel,
        },
    )


def enqueue_index_marker(
    *,
    session_id: str,
    worktree_path: Path,
    session_name: str,
    transcript_snapshot_rel: str,
) -> Path | None:
    """Enqueue an Index marker for deferred search indexing.

    Convenience wrapper around enqueue() for stop hook callers.
    Uses marker_id="idx-<session_id>" to avoid collision with the stop marker
    (which uses marker_id=session_id).

    Args:
        session_id: The Claude session ID.
        worktree_path: Absolute path to the worktree.
        session_name: Forge session name.
        transcript_snapshot_rel: Repo-relative path to transcript artifact.

    Returns:
        Path to created marker, or None if enqueue failed.
    """
    return enqueue(
        kind="index",
        marker_id=f"idx-{session_id}",
        payload={
            "session_id": session_id,
            "worktree_path": str(worktree_path),
            "session_name": session_name,
            "transcript_snapshot_rel": transcript_snapshot_rel,
        },
    )


def enqueue_handoff_marker(
    *,
    session_id: str,
    worktree_path: Path,
    session_name: str,
    transcript_snapshot_rel: str,
    subprocess_proxy: str | None = None,
) -> Path | None:
    """Enqueue a Handoff marker for background memory doc update.

    Convenience wrapper around enqueue() for stop hook callers.
    Uses marker_id="handoff-<session_id>" to avoid collision with the stop marker
    (which uses marker_id=session_id) and the index marker (idx-<session_id>).

    Args:
        session_id: The Claude session ID.
        worktree_path: Absolute path to the worktree.
        session_name: Forge session name.
        transcript_snapshot_rel: Repo-relative path to transcript artifact.
        subprocess_proxy: Optional Stop-time subprocess proxy intent.

    Returns:
        Path to created marker, or None if enqueue failed.
    """
    payload = {
        "session_id": session_id,
        "worktree_path": str(worktree_path),
        "session_name": session_name,
        "transcript_snapshot_rel": transcript_snapshot_rel,
    }
    if subprocess_proxy:
        payload["subprocess_proxy"] = subprocess_proxy

    return enqueue(
        kind="handoff",
        marker_id=f"handoff-{session_id}",
        payload=payload,
    )


def process_pending_work(
    *,
    max_items: int = 25,
    timeout_s: float = PROCESSOR_LOCK_TIMEOUT_S,
    handlers: dict[str, WorkHandler] | None = None,
) -> ProcessResult:
    """Process pending-work markers opportunistically.

    Fast path: if pending dir doesn't exist or is empty, returns immediately.

    For each marker (up to max_items):
    - Acquires per-marker lock (short timeout)
    - Validates marker schema
    - Dispatches to handler by kind (if handler exists)
    - Deletes marker on success
    - On failure: keeps marker, updates attempt_count/last_error
    - On poison (attempt_count >= MAX_ATTEMPTS): moves to failed/

    Args:
        max_items: Maximum markers to process in one invocation.
        timeout_s: Lock timeout per marker (default 50ms).
        handlers: Dict mapping kind -> handler function. If None, uses empty dict
                  (markers with no handler are left in place).

    Returns:
        ProcessResult with counts and any error messages.
    """
    if handlers is None:
        handlers = {}

    result = ProcessResult()

    queue_dir = pending_work_dir()
    if not queue_dir.is_dir():
        return result

    try:
        markers = sorted(queue_dir.glob("*.json"))
    except OSError as e:
        result.errors.append(f"Failed to list markers: {e}")
        return result

    if not markers:
        return result

    for marker_file in markers[:max_items]:
        outcome = _process_single_marker(marker_file, timeout_s=timeout_s, handlers=handlers)
        if outcome is None:
            result.processed += 1
        elif outcome == "skipped":
            result.skipped += 1
        elif outcome == "failed":
            result.failed += 1
        else:
            result.errors.append(outcome)

    return result


def _process_single_marker(
    marker_file: Path,
    *,
    timeout_s: float,
    handlers: dict[str, WorkHandler],
) -> str | None:
    """Process a single marker file.

    Args:
        marker_file: Path to the marker JSON file.
        timeout_s: Lock timeout.
        handlers: Dict mapping kind -> handler function.

    Returns:
        None on success, "skipped" if lock contention, "failed" if moved to failed/,
        error message on validation failure.
    """
    try:
        with file_lock_for_target(target_path=marker_file, timeout_s=timeout_s):
            # Another process may have deleted this between the glob and lock acquisition
            if not marker_file.is_file():
                return None  # Already processed

            try:
                data = read_json(marker_file)
            except Exception as e:
                # Unrecoverable: marker isn't valid JSON, retrying won't help.
                # Move directly to failed/ rather than leaving it stuck forever.
                _move_corrupted_to_failed(marker_file, f"read error: {e}")
                return "failed"

            # Clean up old-shape markers (session_id/work, no marker_id) on sight
            # rather than letting them consume the per-run processing budget
            if "marker_id" not in data and ("session_id" in data or "work" in data):
                logger.info("Cleaning up old-shape marker: %s", marker_file.name)
                marker_file.unlink()
                return None

            error = _validate_marker(data)
            if error:
                attempt_count = _try_write_error(marker_file, error)
                if attempt_count is not None and attempt_count >= MAX_ATTEMPTS:
                    return _move_invalid_to_failed(marker_file, error, attempt_count)
                return f"Invalid marker {marker_file.name}: {error}"

            marker = Marker(
                schema_version=data["schema_version"],
                kind=data["kind"],
                marker_id=data["marker_id"],
                forge_version=data.get("forge_version", "unknown"),
                created_at=data.get("created_at", ""),
                payload=data.get("payload", {}),
                attempt_count=data.get("attempt_count", 0),
                last_attempt_at=data.get("last_attempt_at"),
                last_error=data.get("last_error"),
            )

            # Check for poison marker (too many failures)
            if marker.attempt_count >= MAX_ATTEMPTS:
                return _move_to_failed(marker_file, marker)

            handler = handlers.get(marker.kind)
            if handler is None:
                # No handler registered — leave in place, don't count as error
                logger.debug(
                    "No handler for kind=%s, leaving marker %s in place",
                    marker.kind,
                    marker_file.name,
                )
                return "skipped"

            try:
                handler(marker)
            except Exception as e:
                error_text = f"handler error: {e}"
                attempt_count = _try_write_error(marker_file, error_text)
                if attempt_count is not None and attempt_count >= MAX_ATTEMPTS:
                    marker.attempt_count = attempt_count
                    marker.last_attempt_at = now_iso()
                    marker.last_error = (
                        error_text[:MAX_ERROR_LENGTH] if len(error_text) > MAX_ERROR_LENGTH else error_text
                    )
                    return _move_to_failed(marker_file, marker)
                logger.warning(
                    "Handler failed for %s marker %s: %s",
                    marker.kind,
                    marker_file.name,
                    e,
                )
                return f"Handler error for {marker_file.name}: {e}"

            logger.debug(
                "Processed marker: marker_id=%s, kind=%s",
                marker.marker_id,
                marker.kind,
            )
            marker_file.unlink()
            return None

    except FileLockTimeoutError:
        return "skipped"

    except Exception as e:
        return f"Error processing {marker_file.name}: {e}"


def _validate_marker(data: dict[str, Any]) -> str | None:
    """Validate marker data.

    Returns:
        None if valid, error message if invalid.
    """
    schema_version = data.get("schema_version")
    if schema_version != MARKER_SCHEMA_VERSION:
        return f"unsupported schema_version: {schema_version}"

    kind = data.get("kind")
    if not kind or not isinstance(kind, str):
        return "missing or invalid kind"

    marker_id = data.get("marker_id")
    if not marker_id or not isinstance(marker_id, str):
        return "missing or invalid marker_id"

    if not SAFE_MARKER_ID.match(marker_id):
        return f"unsafe marker_id: {marker_id!r}"

    return None


def _move_corrupted_to_failed(marker_file: Path, error: str) -> None:
    """Move a corrupted (unparseable) marker to the failed/ directory.

    Unlike _move_to_failed(), this handles markers that can't be parsed as JSON.
    Without this, corrupted markers stay in the queue permanently because
    _try_write_error() can't increment attempt_count on invalid JSON.

    IMPORTANT: Caller must hold the per-marker lock.
    """
    try:
        failed_dir = _failed_work_dir()
        failed_dir.mkdir(parents=True, exist_ok=True)
        dest = failed_dir / marker_file.name
        shutil.move(str(marker_file), str(dest))
        logger.warning(
            "Moved corrupted marker to failed/: %s (%s)",
            marker_file.name,
            error,
        )
    except Exception as e:
        logger.warning("Failed to move corrupted marker %s: %s", marker_file.name, e)


def _move_to_failed(marker_file: Path, marker: Marker) -> str:
    """Move a poison marker to the failed/ directory.

    Preserves the marker for debugging. Returns "failed" status string.

    IMPORTANT: Caller must hold the per-marker lock.
    """
    try:
        failed_dir = _failed_work_dir()
        failed_dir.mkdir(parents=True, exist_ok=True)
        dest = failed_dir / marker_file.name
        shutil.move(str(marker_file), str(dest))
        logger.warning(
            "Moved poison marker to failed/ after %d attempts: %s (kind=%s, last_error=%s)",
            marker.attempt_count,
            marker_file.name,
            marker.kind,
            marker.last_error,
        )
    except Exception as e:
        logger.warning("Failed to move poison marker %s: %s", marker_file.name, e)
    return "failed"


def _move_invalid_to_failed(marker_file: Path, error: str, attempt_count: int) -> str:
    """Move a parseable but schema-invalid marker to failed/ after retries."""
    try:
        failed_dir = _failed_work_dir()
        failed_dir.mkdir(parents=True, exist_ok=True)
        dest = failed_dir / marker_file.name
        shutil.move(str(marker_file), str(dest))
        logger.warning(
            "Moved invalid marker to failed/ after %d attempts: %s (%s)",
            attempt_count,
            marker_file.name,
            error,
        )
    except Exception as e:
        logger.warning("Failed to move invalid marker %s: %s", marker_file.name, e)
    return "failed"


def _try_write_error(marker_file: Path, error: str) -> int | None:
    """Best-effort write last_error to marker.

    IMPORTANT: This function assumes the caller already holds the per-marker lock.
    It does not acquire any locks itself. Only call from within a file_lock_for_target
    context on the marker file.

    Returns the updated attempt_count, or None if the write failed.
    """
    try:
        with open(marker_file, encoding="utf-8") as f:
            data = json.load(f)

        data["attempt_count"] = data.get("attempt_count", 0) + 1
        data["last_attempt_at"] = now_iso()
        # Truncate error to avoid bloating
        data["last_error"] = error[:MAX_ERROR_LENGTH] if len(error) > MAX_ERROR_LENGTH else error

        atomic_write_json(marker_file, data)
        return data["attempt_count"]
    except Exception:
        # Best-effort; swallow errors
        return None
