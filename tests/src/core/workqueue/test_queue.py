"""Tests for the Forge async work queue (forge.core.workqueue).

Covers: marker creation, validation, processing, handler dispatch,
poison markers, schema v2, v1 migration, and marker_id safety.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forge.core.workqueue import (
    MARKER_SCHEMA_VERSION,
    MAX_ATTEMPTS,
    PENDING_WORK_DIR,
    Marker,
    enqueue,
    enqueue_handoff_marker,
    enqueue_index_marker,
    enqueue_stop_marker,
    marker_path,
    pending_work_dir,
    process_pending_work,
)


class TestPathHelpers:
    """Tests for path helper functions."""

    def test_pending_work_dir_uses_forge_home(self) -> None:
        """pending_work_dir() returns <FORGE_HOME>/pending-work."""
        result = pending_work_dir()
        forge_home = Path(os.environ["FORGE_HOME"])
        assert result == forge_home / PENDING_WORK_DIR

    def test_marker_path_valid_marker_id(self) -> None:
        """marker_path() returns correct path for valid marker ID."""
        result = marker_path("uuid-123-abc")
        expected = pending_work_dir() / "uuid-123-abc.json"
        assert result == expected

    def test_marker_path_accepts_dots(self) -> None:
        """marker_path() accepts dots in marker IDs."""
        result = marker_path("session.v2.abc")
        assert result == pending_work_dir() / "session.v2.abc.json"

    def test_marker_path_rejects_empty(self) -> None:
        """marker_path() rejects empty marker ID."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("")

    def test_marker_path_rejects_path_traversal(self) -> None:
        """marker_path() rejects marker IDs with path traversal characters."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("../evil")
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("foo/bar")
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("foo\\bar")

    def test_marker_path_rejects_spaces(self) -> None:
        """marker_path() rejects marker IDs with spaces."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("has space")


class TestEnqueue:
    """Tests for the generic enqueue() function."""

    def test_enqueue_creates_marker_with_schema_v2(self, tmp_path: Path) -> None:
        """enqueue() creates a marker file with v2 schema."""
        result = enqueue(
            kind="index",
            marker_id="test-123",
            payload={"path": "/some/path", "mode": "keyword"},
        )

        assert result is not None
        assert result.is_file()

        data = json.loads(result.read_text())
        assert data["schema_version"] == MARKER_SCHEMA_VERSION
        assert data["kind"] == "index"
        assert data["marker_id"] == "test-123"
        assert data["forge_version"] is not None
        assert data["payload"] == {"path": "/some/path", "mode": "keyword"}
        assert data["attempt_count"] == 0
        assert data["last_attempt_at"] is None
        assert data["last_error"] is None
        # v2: no "work" field, no "session_id" in envelope
        assert "work" not in data
        assert "session_id" not in data

    def test_enqueue_custom_kind(self) -> None:
        """enqueue() accepts any non-empty kind string."""
        result = enqueue(kind="handoff", marker_id="abc-123", payload={"agent": "summarizer"})
        assert result is not None
        data = json.loads(result.read_text())
        assert data["kind"] == "handoff"

    def test_enqueue_returns_none_on_invalid_marker_id(self) -> None:
        """enqueue() returns None for invalid marker ID."""
        result = enqueue(kind="test", marker_id="../evil", payload={})
        assert result is None

    def test_enqueue_creates_directory(self) -> None:
        """enqueue() creates the pending-work directory if missing."""
        queue_dir = pending_work_dir()
        assert not queue_dir.exists()

        enqueue(kind="test", marker_id="dir-test", payload={})

        assert queue_dir.exists()


class TestEnqueueStopMarker:
    """Tests for the enqueue_stop_marker() convenience wrapper."""

    def test_creates_stop_marker_with_payload(self, tmp_path: Path) -> None:
        """enqueue_stop_marker creates a stop marker with session data in payload."""
        result = enqueue_stop_marker(
            session_id="test-session-123",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel=".forge/artifacts/my-session/transcripts/test-session-123.jsonl",
        )

        assert result is not None
        assert result.is_file()

        data = json.loads(result.read_text())
        assert data["schema_version"] == MARKER_SCHEMA_VERSION
        assert data["kind"] == "stop"
        assert data["marker_id"] == "test-session-123"
        # Session-specific data is in payload
        assert data["payload"]["session_id"] == "test-session-123"
        assert data["payload"]["worktree_path"] == str(tmp_path)
        assert data["payload"]["session_name"] == "my-session"
        assert (
            data["payload"]["transcript_snapshot_rel"]
            == ".forge/artifacts/my-session/transcripts/test-session-123.jsonl"
        )

    def test_returns_none_on_invalid_session_id(self, tmp_path: Path) -> None:
        """enqueue_stop_marker returns None for invalid session ID."""
        result = enqueue_stop_marker(
            session_id="../evil",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel=".forge/artifacts/my-session/transcripts/evil.jsonl",
        )
        assert result is None


class TestEnqueueIndexMarker:
    """Tests for the enqueue_index_marker() convenience wrapper."""

    def test_creates_index_marker_with_payload(self, tmp_path: Path) -> None:
        """enqueue_index_marker creates an index marker with session data in payload."""
        result = enqueue_index_marker(
            session_id="test-session-123",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel=".forge/artifacts/my-session/transcripts/test-session-123.jsonl",
        )

        assert result is not None
        assert result.is_file()

        data = json.loads(result.read_text())
        assert data["schema_version"] == MARKER_SCHEMA_VERSION
        assert data["kind"] == "index"
        assert data["marker_id"] == "idx-test-session-123"
        assert data["payload"]["session_id"] == "test-session-123"
        assert data["payload"]["worktree_path"] == str(tmp_path)
        assert data["payload"]["session_name"] == "my-session"
        assert (
            data["payload"]["transcript_snapshot_rel"]
            == ".forge/artifacts/my-session/transcripts/test-session-123.jsonl"
        )

    def test_marker_id_prefixed_with_idx(self, tmp_path: Path) -> None:
        """enqueue_index_marker uses idx-<session_id> as marker_id to avoid collision."""
        result = enqueue_index_marker(
            session_id="session-abc",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )

        assert result is not None
        assert result.name == "idx-session-abc.json"

    def test_no_collision_with_stop_marker(self, tmp_path: Path) -> None:
        """Index and stop markers for the same session coexist without collision."""
        stop_result = enqueue_stop_marker(
            session_id="same-session",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )
        index_result = enqueue_index_marker(
            session_id="same-session",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )

        assert stop_result is not None
        assert index_result is not None
        assert stop_result != index_result
        assert stop_result.name == "same-session.json"
        assert index_result.name == "idx-same-session.json"
        assert stop_result.is_file()
        assert index_result.is_file()

    def test_returns_none_on_invalid_session_id(self, tmp_path: Path) -> None:
        """enqueue_index_marker returns None for invalid session ID."""
        result = enqueue_index_marker(
            session_id="../evil",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel="transcript.jsonl",
        )
        assert result is None


class TestEnqueueHandoffMarker:
    """Tests for the enqueue_handoff_marker() convenience wrapper."""

    def test_creates_handoff_marker_with_payload(self, tmp_path: Path) -> None:
        """enqueue_handoff_marker creates a handoff marker with session data in payload."""
        result = enqueue_handoff_marker(
            session_id="test-session-123",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel=".forge/artifacts/my-session/transcripts/test-session-123.jsonl",
        )

        assert result is not None
        assert result.is_file()

        data = json.loads(result.read_text())
        assert data["schema_version"] == MARKER_SCHEMA_VERSION
        assert data["kind"] == "handoff"
        assert data["marker_id"] == "handoff-test-session-123"
        assert data["payload"]["session_id"] == "test-session-123"
        assert data["payload"]["worktree_path"] == str(tmp_path)
        assert data["payload"]["session_name"] == "my-session"
        assert (
            data["payload"]["transcript_snapshot_rel"]
            == ".forge/artifacts/my-session/transcripts/test-session-123.jsonl"
        )
        assert "subprocess_proxy" not in data["payload"]

    def test_includes_subprocess_proxy_when_provided(self, tmp_path: Path) -> None:
        """Handoff marker snapshots subprocess proxy intent for detached execution."""
        result = enqueue_handoff_marker(
            session_id="test-session-123",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel="transcript.jsonl",
            subprocess_proxy="openrouter-subprocess",
        )

        assert result is not None
        data = json.loads(result.read_text())
        assert data["payload"]["subprocess_proxy"] == "openrouter-subprocess"

    def test_marker_id_prefixed_with_handoff(self, tmp_path: Path) -> None:
        """enqueue_handoff_marker uses handoff-<session_id> as marker_id."""
        result = enqueue_handoff_marker(
            session_id="session-abc",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )

        assert result is not None
        assert result.name == "handoff-session-abc.json"

    def test_no_collision_with_stop_and_index_markers(self, tmp_path: Path) -> None:
        """Handoff, index, and stop markers for the same session coexist without collision."""
        stop_result = enqueue_stop_marker(
            session_id="same-session",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )
        index_result = enqueue_index_marker(
            session_id="same-session",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )
        handoff_result = enqueue_handoff_marker(
            session_id="same-session",
            worktree_path=tmp_path,
            session_name="test",
            transcript_snapshot_rel="transcript.jsonl",
        )

        assert stop_result is not None
        assert index_result is not None
        assert handoff_result is not None
        # All three have unique filenames
        assert len({stop_result.name, index_result.name, handoff_result.name}) == 3
        assert stop_result.name == "same-session.json"
        assert index_result.name == "idx-same-session.json"
        assert handoff_result.name == "handoff-same-session.json"

    def test_returns_none_on_invalid_session_id(self, tmp_path: Path) -> None:
        """enqueue_handoff_marker returns None for invalid session ID."""
        result = enqueue_handoff_marker(
            session_id="../evil",
            worktree_path=tmp_path,
            session_name="my-session",
            transcript_snapshot_rel="transcript.jsonl",
        )
        assert result is None


class TestProcessPendingWork:
    """Tests for process_pending_work()."""

    def test_fast_path_empty_directory(self) -> None:
        """process_pending_work returns quickly when queue is empty."""
        result = process_pending_work()
        assert result.processed == 0
        assert result.skipped == 0
        assert result.errors == []

    def test_fast_path_no_directory(self) -> None:
        """process_pending_work returns quickly when directory doesn't exist."""
        queue_dir = pending_work_dir()
        assert not queue_dir.exists()

        result = process_pending_work()
        assert result.processed == 0
        assert result.skipped == 0
        assert result.errors == []

    def test_respects_max_items(self) -> None:
        """process_pending_work respects max_items limit."""
        handler = MagicMock()
        for i in range(5):
            enqueue(kind="test", marker_id=f"uuid-{i}", payload={})

        result = process_pending_work(max_items=2, handlers={"test": handler})
        assert result.processed == 2
        assert handler.call_count == 2

        remaining = list(pending_work_dir().glob("*.json"))
        assert len(remaining) == 3


class TestHandlerDispatch:
    """Tests for handler registration and dispatch."""

    def test_handler_called_and_marker_deleted(self) -> None:
        """Handler is called with Marker and marker is deleted on success."""
        handler = MagicMock()

        marker = enqueue(kind="index", marker_id="dispatch-test", payload={"key": "val"})
        assert marker is not None
        assert marker.is_file()

        result = process_pending_work(handlers={"index": handler})
        assert result.processed == 1
        assert not marker.is_file()

        # Verify handler received a Marker object
        handler.assert_called_once()
        received_marker = handler.call_args[0][0]
        assert isinstance(received_marker, Marker)
        assert received_marker.kind == "index"
        assert received_marker.marker_id == "dispatch-test"
        assert received_marker.payload == {"key": "val"}

    def test_unhandled_kind_leaves_marker(self) -> None:
        """Markers with no handler are left in place (counted as skipped)."""
        marker = enqueue(kind="unknown", marker_id="no-handler", payload={})
        assert marker is not None

        # Process with no handler for "unknown"
        result = process_pending_work(handlers={"stop": MagicMock()})
        assert result.skipped == 1
        assert result.processed == 0
        assert marker.is_file()

    def test_handler_failure_increments_attempt(self) -> None:
        """Handler raising an exception increments attempt_count."""
        handler = MagicMock(side_effect=RuntimeError("handler broke"))

        marker = enqueue(kind="fail", marker_id="fail-test", payload={})
        assert marker is not None

        result = process_pending_work(handlers={"fail": handler})
        assert len(result.errors) == 1
        assert "handler broke" in result.errors[0]

        # Marker should still exist with incremented attempt_count
        assert marker.is_file()
        data = json.loads(marker.read_text())
        assert data["attempt_count"] == 1
        assert "handler broke" in data["last_error"]
        assert data["last_attempt_at"] is not None

    def test_multiple_kinds_dispatched_correctly(self) -> None:
        """Multiple kinds are dispatched to the correct handlers."""
        stop_handler = MagicMock()
        index_handler = MagicMock()

        enqueue(kind="stop", marker_id="s1", payload={"type": "stop"})
        enqueue(kind="index", marker_id="i1", payload={"type": "index"})

        result = process_pending_work(handlers={"stop": stop_handler, "index": index_handler})
        assert result.processed == 2
        assert stop_handler.call_count == 1
        assert index_handler.call_count == 1

    def test_no_handlers_dict_skips_all(self) -> None:
        """With no handlers (default), all markers are skipped."""
        enqueue(kind="stop", marker_id="skip-test", payload={})

        result = process_pending_work()  # handlers=None → empty dict
        assert result.skipped == 1
        assert result.processed == 0


class TestPoisonMarkers:
    """Tests for poison marker strategy (MAX_ATTEMPTS → failed/)."""

    def test_poison_marker_moved_to_failed(self) -> None:
        """Marker exceeding MAX_ATTEMPTS is moved to failed/ directory."""
        marker = enqueue(kind="bad", marker_id="poison-test", payload={})
        assert marker is not None

        # Manually set attempt_count to MAX_ATTEMPTS
        data = json.loads(marker.read_text())
        data["attempt_count"] = MAX_ATTEMPTS
        marker.write_text(json.dumps(data))

        result = process_pending_work(handlers={"bad": MagicMock()})
        assert result.failed == 1

        # Original marker should be gone
        assert not marker.is_file()

        # Should be in failed/
        failed_dir = pending_work_dir() / "failed"
        assert failed_dir.is_dir()
        failed_marker = failed_dir / "poison-test.json"
        assert failed_marker.is_file()

    def test_non_poison_marker_retried(self) -> None:
        """Marker below MAX_ATTEMPTS is retried, not moved to failed/."""
        handler = MagicMock(side_effect=RuntimeError("try again"))

        marker = enqueue(kind="retry", marker_id="retry-test", payload={})
        assert marker is not None

        # Set attempt_count to just below limit
        data = json.loads(marker.read_text())
        data["attempt_count"] = MAX_ATTEMPTS - 2
        marker.write_text(json.dumps(data))

        result = process_pending_work(handlers={"retry": handler})
        assert result.failed == 0
        assert len(result.errors) == 1
        # Still in queue, not moved
        assert marker.is_file()

    def test_handler_failure_moves_to_failed_after_updated_attempt_count(self) -> None:
        """A handler error that reaches MAX_ATTEMPTS moves the marker immediately."""
        handler = MagicMock(side_effect=RuntimeError("last try"))

        marker = enqueue(kind="retry", marker_id="handler-poison-test", payload={})
        assert marker is not None

        data = json.loads(marker.read_text())
        data["attempt_count"] = MAX_ATTEMPTS - 1
        marker.write_text(json.dumps(data))

        result = process_pending_work(handlers={"retry": handler})

        assert result.failed == 1
        assert result.errors == []
        assert not marker.is_file()
        assert (pending_work_dir() / "failed" / "handler-poison-test.json").is_file()


class TestSchemaValidation:
    """Tests for marker schema validation."""

    def test_corrupted_marker_moved_to_failed(self) -> None:
        """Corrupted (unparseable JSON) markers are moved to failed/ immediately.

        Unlike handler failures (which retry), corrupted markers can never be
        parsed, so retrying is pointless. They must be moved to failed/ to
        prevent them from staying in the queue permanently.
        """
        queue_dir = pending_work_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)

        corrupt_marker = queue_dir / "corrupted.json"
        corrupt_marker.write_text("not valid json {{{")

        result = process_pending_work(handlers={})
        assert result.failed == 1
        assert result.processed == 0

        # Original marker should be gone from queue
        assert not corrupt_marker.is_file()

        # Should be in failed/
        failed_dir = queue_dir / "failed"
        assert (failed_dir / "corrupted.json").is_file()

    def test_rejects_invalid_schema_version(self) -> None:
        """process_pending_work rejects markers with invalid schema version."""
        queue_dir = pending_work_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)

        invalid_marker = queue_dir / "invalid-schema.json"
        invalid_marker.write_text(
            json.dumps(
                {
                    "schema_version": 999,
                    "kind": "stop",
                    "marker_id": "test",
                }
            )
        )

        result = process_pending_work(handlers={})
        assert result.processed == 0
        assert len(result.errors) == 1
        assert "unsupported schema_version" in result.errors[0]

    def test_writes_last_error_on_failure(self) -> None:
        """process_pending_work writes last_error to marker on failure."""
        queue_dir = pending_work_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)

        invalid_marker = queue_dir / "invalid-kind.json"
        invalid_marker.write_text(
            json.dumps(
                {
                    "schema_version": MARKER_SCHEMA_VERSION,
                    "kind": "",  # Empty kind is invalid
                    "marker_id": "test",
                    "attempt_count": 0,
                }
            )
        )

        process_pending_work(handlers={})

        data = json.loads(invalid_marker.read_text())
        assert data["last_error"] is not None
        assert "invalid kind" in data["last_error"]
        assert data["attempt_count"] == 1
        assert data["last_attempt_at"] is not None

    def test_invalid_parseable_marker_moves_to_failed_at_max_attempts(self) -> None:
        """Schema-invalid JSON is retried only up to MAX_ATTEMPTS, then quarantined."""
        queue_dir = pending_work_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)

        invalid_marker = queue_dir / "invalid-kind-poison.json"
        invalid_marker.write_text(
            json.dumps(
                {
                    "schema_version": MARKER_SCHEMA_VERSION,
                    "kind": "",
                    "marker_id": "test",
                    "attempt_count": MAX_ATTEMPTS - 1,
                }
            )
        )

        result = process_pending_work(handlers={})

        assert result.failed == 1
        assert result.errors == []
        assert not invalid_marker.is_file()
        failed_marker = queue_dir / "failed" / "invalid-kind-poison.json"
        assert failed_marker.is_file()

        data = json.loads(failed_marker.read_text())
        assert data["attempt_count"] == MAX_ATTEMPTS
        assert "invalid kind" in data["last_error"]

    def test_invalid_parseable_markers_do_not_starve_later_valid_markers(self) -> None:
        """Poisoned invalid markers leave the pending queue so valid work can run next."""
        queue_dir = pending_work_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)

        for i in range(5):
            (queue_dir / f"invalid-{i}.json").write_text(
                json.dumps(
                    {
                        "schema_version": MARKER_SCHEMA_VERSION,
                        "kind": "",
                        "marker_id": f"invalid-{i}",
                        "attempt_count": MAX_ATTEMPTS - 1,
                    }
                )
            )

        handler = MagicMock()
        valid_marker = enqueue(kind="valid", marker_id="valid-after-poison", payload={})
        assert valid_marker is not None

        first = process_pending_work(max_items=5, handlers={"valid": handler})
        assert first.failed == 5
        assert handler.call_count == 0

        second = process_pending_work(max_items=5, handlers={"valid": handler})
        assert second.processed == 1
        handler.assert_called_once()
        assert not valid_marker.is_file()

    def test_old_shape_marker_cleanup(self) -> None:
        """Old-shape markers (session_id/work, no marker_id) are cleaned up on sight."""
        queue_dir = pending_work_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)

        old_marker = queue_dir / "old-shape.json"
        old_marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "stop",
                    "session_id": "old-session",
                    "work": [],
                }
            )
        )

        result = process_pending_work(handlers={})
        assert result.processed == 1
        assert not old_marker.is_file()


class TestMarkerIdValidation:
    """Tests for marker_id filename safety."""

    def test_accepts_alphanumeric(self) -> None:
        """marker_path() accepts standard alphanumeric IDs."""
        path = marker_path("abc123")
        assert path.name == "abc123.json"

    def test_accepts_hyphens_underscores(self) -> None:
        """marker_path() accepts hyphens and underscores."""
        path = marker_path("my-session_v2")
        assert path.name == "my-session_v2.json"

    def test_accepts_dots(self) -> None:
        """marker_path() accepts dots."""
        path = marker_path("session.2026.01")
        assert path.name == "session.2026.01.json"

    def test_rejects_slash(self) -> None:
        """marker_path() rejects forward slashes."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("foo/bar")

    def test_rejects_backslash(self) -> None:
        """marker_path() rejects backslashes."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("foo\\bar")

    def test_rejects_dotdot(self) -> None:
        """marker_path() rejects '..' path traversal."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("../etc/passwd")

    def test_rejects_space(self) -> None:
        """marker_path() rejects spaces."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("has space")

    def test_rejects_null_byte(self) -> None:
        """marker_path() rejects null bytes."""
        with pytest.raises(ValueError, match="Invalid marker_id"):
            marker_path("null\x00byte")


class TestLockContention:
    """Tests for lock contention handling."""

    def test_skips_locked_marker(self, tmp_path: Path) -> None:
        """process_pending_work skips markers that are locked by another process."""
        from forge.core.state import file_lock_for_target

        handler = MagicMock()

        marker = enqueue(kind="locked", marker_id="locked-marker", payload={})
        assert marker is not None

        # Acquire lock on the marker (simulating another process)
        with file_lock_for_target(target_path=marker, timeout_s=1.0):
            result = process_pending_work(timeout_s=0.01, handlers={"locked": handler})
            assert result.skipped == 1
            assert result.processed == 0
            assert result.errors == []
            assert marker.is_file()
            handler.assert_not_called()

        # After lock released, processing should succeed
        result = process_pending_work(handlers={"locked": handler})
        assert result.processed == 1
        assert not marker.is_file()


class TestNoGlobalState:
    """Tests verifying no global state leakage."""

    def test_handlers_are_per_call(self) -> None:
        """Each process_pending_work call uses its own handlers dict."""
        handler_a = MagicMock()
        handler_b = MagicMock()

        enqueue(kind="a", marker_id="marker-a", payload={})
        enqueue(kind="b", marker_id="marker-b", payload={})

        # First call only handles "a"
        result = process_pending_work(handlers={"a": handler_a})
        assert result.processed == 1
        assert result.skipped == 1  # "b" has no handler
        handler_a.assert_called_once()
        handler_b.assert_not_called()

        # Second call handles "b"
        result = process_pending_work(handlers={"b": handler_b})
        assert result.processed == 1
        handler_b.assert_called_once()
