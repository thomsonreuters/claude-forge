"""Tests for artifact-capture hook commands.

These hooks should always exit 0 (Claude Code safety) but report success/failure
in the JSON payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from forge.cli.hooks import hooks
from forge.session import SessionStore, create_session_state
from forge.session.index import IndexStore


def _write_pending_transcript_marker(
    marker: Path,
    *,
    run_dir: Path,
    session_id: str | None = None,
    transcript_contains: str | None = None,
) -> None:
    payload: dict[str, str] = {"run_dir": str(run_dir)}
    if session_id is not None:
        payload["session_id"] = session_id
    if transcript_contains is not None:
        payload["transcript_contains"] = transcript_contains
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload), encoding="utf-8")


def _write_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_name: str = "test-session"
) -> SessionStore:
    manifest = create_session_state(
        session_name,
        proxy_template="test-family",
        proxy_base_url="http://localhost:8080",
    )
    store = SessionStore(str(tmp_path), session_name)
    store.write(manifest)

    monkeypatch.setenv("FORGE_SESSION", session_name)
    monkeypatch.setenv("FORGE_FORGE_ROOT", str(tmp_path))
    return store


class TestPlanWriteHook:
    def test_skips_non_plan_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_manifest(tmp_path, monkeypatch)

        runner = CliRunner()
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_input": {"file_path": "README.md"},
        }
        result = runner.invoke(hooks, ["plan-write"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["action"] == "skip"

    def test_records_plan_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        store = _write_manifest(tmp_path, monkeypatch)

        runner = CliRunner()
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_input": {"file_path": ".claude/plans/foo.md"},
        }
        result = runner.invoke(hooks, ["plan-write"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["action"] == "recorded"

        updated = store.read()
        assert updated.confirmed.latest_plan_path == ".claude/plans/foo.md"


class TestExitPlanModeHook:
    def test_snapshots_approved_plan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        store = _write_manifest(tmp_path, monkeypatch)

        # Create a plan file
        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "foo.md"
        plan_file.write_text("hello plan", encoding="utf-8")

        # Point manifest at it
        manifest = store.read()
        manifest.confirmed.latest_plan_path = ".claude/plans/foo.md"
        store.write(manifest)

        runner = CliRunner()
        payload = {"hook_event_name": "PreToolUse"}
        result = runner.invoke(hooks, ["exit-plan-mode"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["action"] == "snapshotted"

        updated = store.read()
        plans = updated.confirmed.artifacts.get("plans")
        assert isinstance(plans, list)
        assert plans, "expected at least one plan artifact"
        assert plans[-1]["kind"] == "approved"
        assert str(plans[-1]["snapshot_path"]).startswith(".forge/artifacts/test-session/plans/")


class TestStopHook:
    def test_copies_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        # Isolate FORGE_HOME so real markers aren't consumed
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge-test"))
        store = _write_manifest(tmp_path, monkeypatch)

        # Create a fake transcript file
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        # Put it in manifest so hook can fallback
        manifest = store.read()
        manifest.confirmed.transcript_path = str(transcript)
        manifest.confirmed.claude_session_id = "uuid-123"
        store.write(manifest)

        runner = CliRunner()
        payload = {"hook_event_name": "Stop"}
        result = runner.invoke(hooks, ["stop"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["action"] == "copied"
        assert output["queued"] is True  # marker enqueue

        updated = store.read()
        transcripts = updated.confirmed.artifacts.get("transcripts")
        assert isinstance(transcripts, list)
        assert transcripts
        assert transcripts[-1]["session_id"] == "uuid-123"
        assert str(transcripts[-1]["copied_path"]).endswith("/uuid-123.jsonl")

        # Verify pending-work marker was created
        from forge.core.workqueue import pending_work_dir

        marker_file = pending_work_dir() / "uuid-123.json"
        assert marker_file.is_file(), "Stop hook should enqueue pending-work marker"

        marker_data = json.loads(marker_file.read_text())
        assert marker_data["kind"] == "stop"
        assert marker_data["marker_id"] == "uuid-123"
        assert marker_data["payload"]["session_name"] == "test-session"
        assert marker_data["payload"]["transcript_snapshot_rel"].endswith("/uuid-123.jsonl")

    def test_reconciles_child_uuid_when_fork_session_start_kept_parent_uuid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop should correct same-dir fork manifests when SessionStart saw the parent UUID."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge-test"))
        store = _write_manifest(tmp_path, monkeypatch, session_name="fork-child")

        manifest = store.read()
        manifest.is_fork = True
        manifest.parent_session = "fork-parent"
        manifest.forge_root = str(tmp_path)
        manifest.confirmed.claude_session_id = "parent-uuid"
        manifest.confirmed.transcript_path = str(tmp_path / "parent.jsonl")
        manifest.confirmed.confirmed_by = "hook:SessionStart:startup"
        store.write(manifest)

        IndexStore().add_from_state(
            manifest,
            str(tmp_path),
            checkout_root=str(tmp_path),
            forge_root=str(tmp_path),
            relative_path=".",
        )

        child_transcript = tmp_path / "child.jsonl"
        child_transcript.write_text("{}\n", encoding="utf-8")

        runner = CliRunner()
        payload = {
            "hook_event_name": "Stop",
            "session_id": "child-uuid",
            "transcript_path": str(child_transcript),
        }
        result = runner.invoke(hooks, ["stop"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True

        updated = store.read()
        assert updated.confirmed.claude_session_id == "child-uuid"
        assert updated.confirmed.transcript_path == str(child_transcript)
        assert updated.confirmed.confirmed_by == "hook:stop"

        index_entry = IndexStore().get_session("fork-child", forge_root=str(tmp_path))
        assert index_entry.claude_session_id == "child-uuid"

    def test_no_session_still_copies_pending_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        forge_home = tmp_path / ".forge-test"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-17-133346"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        token = "forge-qa-transcript-token:test-stop-match"
        _write_pending_transcript_marker(
            marker,
            run_dir=run_dir,
            session_id="uuid-123",
            transcript_contains=token,
        )

        transcript = tmp_path / "t.jsonl"
        transcript.write_text(f'{{"msg":"hello {token}"}}\n', encoding="utf-8")

        runner = CliRunner()
        payload = {
            "hook_event_name": "Stop",
            "session_id": "uuid-123",
            "transcript_path": str(transcript),
        }
        result = runner.invoke(hooks, ["stop"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["action"] == "skip"
        assert output["reason"] == "no_session"
        assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == f'{{"msg":"hello {token}"}}\n'
        assert not marker.exists()

    def test_no_session_leaves_pending_transcript_for_other_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        forge_home = tmp_path / ".forge-test"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-17-142207"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(
            marker,
            run_dir=run_dir,
            session_id="qa-session-123",
            transcript_contains="forge-qa-transcript-token:qa-session-123",
        )

        transcript = tmp_path / "wrong-thread.jsonl"
        transcript.write_text('{"msg":"wrong thread"}\n', encoding="utf-8")

        runner = CliRunner()
        payload = {
            "hook_event_name": "Stop",
            "session_id": "other-session-456",
            "transcript_path": str(transcript),
        }
        result = runner.invoke(hooks, ["stop"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["action"] == "skip"
        assert output["reason"] == "no_session"
        assert not (run_dir / "transcript.jsonl").exists()
        assert marker.exists()


class TestStopFailureHook:
    def test_empty_stdin(self) -> None:
        runner = CliRunner()
        result = runner.invoke(hooks, ["stop-failure"], input="")
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is False
        assert output["error"] == "invalid_input"

    def test_wrong_event(self) -> None:
        runner = CliRunner()
        payload = {"hook_event_name": "Stop"}
        result = runner.invoke(hooks, ["stop-failure"], input=json.dumps(payload))
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["action"] == "skip"
        assert output["reason"] == "wrong_event"

    def test_no_session_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge-test"))
        monkeypatch.delenv("FORGE_SESSION", raising=False)
        monkeypatch.delenv("FORGE_FORK_NAME", raising=False)

        runner = CliRunner()
        payload = {"hook_event_name": "StopFailure", "session_id": "uuid-fail"}
        result = runner.invoke(hooks, ["stop-failure"], input=json.dumps(payload))
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["reason"] == "no_session"

    def test_captures_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge-test"))
        store = _write_manifest(tmp_path, monkeypatch)

        transcript = tmp_path / "t.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        manifest = store.read()
        manifest.confirmed.transcript_path = str(transcript)
        manifest.confirmed.claude_session_id = "uuid-fail-123"
        store.write(manifest)

        runner = CliRunner()
        payload = {"hook_event_name": "StopFailure"}
        result = runner.invoke(hooks, ["stop-failure"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["copied"] is True

        updated = store.read()
        transcripts = updated.confirmed.artifacts.get("transcripts")
        assert isinstance(transcripts, list)
        assert transcripts
        assert transcripts[-1]["reason"] == "stop-failure"
        assert transcripts[-1]["session_id"] == "uuid-fail-123"

    def test_never_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """StopFailure must always exit 0, even with missing transcript."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge-test"))
        store = _write_manifest(tmp_path, monkeypatch)

        manifest = store.read()
        manifest.confirmed.claude_session_id = "uuid-nope"
        store.write(manifest)

        runner = CliRunner()
        payload = {"hook_event_name": "StopFailure"}
        result = runner.invoke(hooks, ["stop-failure"], input=json.dumps(payload))
        assert result.exit_code == 0

    def test_copy_failure_skips_enqueue(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When transcript copy fails, don't enqueue markers for nonexistent artifacts."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge-test"))
        store = _write_manifest(tmp_path, monkeypatch)

        manifest = store.read()
        # Point to a nonexistent transcript so copy fails
        manifest.confirmed.transcript_path = str(tmp_path / "nonexistent.jsonl")
        manifest.confirmed.claude_session_id = "uuid-copy-fail"
        store.write(manifest)

        runner = CliRunner()
        payload = {"hook_event_name": "StopFailure"}
        result = runner.invoke(hooks, ["stop-failure"], input=json.dumps(payload))

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["copied"] is False
        assert output["queued"] is False
        assert output["queued_index"] is False


class TestCopyTranscriptToPendingRuns:
    """Tests for _copy_transcript_to_pending_runs helper."""

    def _call(self, transcript_path: Path, *, session_id: str | None = None) -> None:
        from forge.cli.hooks.commands import _copy_transcript_to_pending_runs

        _copy_transcript_to_pending_runs(transcript_path, session_id=session_id)

    def test_marker_copies_transcript_and_removes_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        # Create run dir and marker
        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-02-120000"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(marker, run_dir=run_dir)

        # Create transcript
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"msg":"hello"}\n', encoding="utf-8")

        self._call(transcript)

        assert (run_dir / "transcript.jsonl").is_file()
        assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == '{"msg":"hello"}\n'
        assert not marker.exists()

    def test_structured_marker_copies_only_for_matching_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-17-142207"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        token = "forge-qa-transcript-token:run-142207"
        _write_pending_transcript_marker(
            marker,
            run_dir=run_dir,
            session_id="qa-session-123",
            transcript_contains=token,
        )

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(f'{{"msg":"hello {token}"}}\n', encoding="utf-8")

        self._call(transcript, session_id="qa-session-123")

        assert (run_dir / "transcript.jsonl").is_file()
        assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == f'{{"msg":"hello {token}"}}\n'
        assert not marker.exists()

    def test_structured_marker_waits_for_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-17-142207"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(
            marker,
            run_dir=run_dir,
            session_id="qa-session-123",
            transcript_contains="forge-qa-transcript-token:run-142207",
        )

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"msg":"wrong thread"}\n', encoding="utf-8")

        self._call(transcript, session_id="other-session-456")

        assert not (run_dir / "transcript.jsonl").exists()
        assert marker.exists()

    def test_structured_marker_waits_for_token_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-17-142208"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(
            marker,
            run_dir=run_dir,
            transcript_contains="forge-qa-transcript-token:run-142208",
        )

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"msg":"same session, wrong token"}\n', encoding="utf-8")

        self._call(transcript, session_id="qa-session-123")

        assert not (run_dir / "transcript.jsonl").exists()
        assert marker.exists()

    def test_no_marker_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        forge_home.mkdir(parents=True)
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        # Should not raise
        self._call(transcript)

    def test_invalid_path_in_marker_removes_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(
            marker,
            run_dir=forge_home / "manual-testing" / "qa" / "runs" / "nonexistent",
        )

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        self._call(transcript)

        assert not marker.exists()

    def test_path_outside_forge_home_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        # Point marker to a directory outside forge_home
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        marker = forge_home / "manual-testing" / "walkthrough" / ".pending-transcript"
        _write_pending_transcript_marker(marker, run_dir=evil_dir)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        self._call(transcript)

        assert not marker.exists()
        assert not (evil_dir / "transcript.jsonl").exists()

    def test_path_inside_forge_home_but_outside_runs_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        # Point marker to a dir under manual-testing/qa/ but not under runs/
        sneaky_dir = forge_home / "manual-testing" / "qa" / "sneaky"
        sneaky_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(marker, run_dir=sneaky_dir)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        self._call(transcript)

        assert not marker.exists()
        assert not (sneaky_dir / "transcript.jsonl").exists()

    def test_relative_path_in_marker_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-02-120000"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"run_dir": "runs/2026-03-02-120000"}),
            encoding="utf-8",
        )

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        self._call(transcript)

        assert not marker.exists()
        assert not (run_dir / "transcript.jsonl").exists()

    def test_legacy_plain_string_marker_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-02-120000"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(run_dir), encoding="utf-8")

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        self._call(transcript)

        assert not marker.exists()
        assert not (run_dir / "transcript.jsonl").exists()

    def test_transcript_not_found_no_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        forge_home = tmp_path / ".forge"
        monkeypatch.setenv("FORGE_HOME", str(forge_home))

        run_dir = forge_home / "manual-testing" / "qa" / "runs" / "2026-03-02-120000"
        run_dir.mkdir(parents=True)
        marker = forge_home / "manual-testing" / "qa" / ".pending-transcript"
        _write_pending_transcript_marker(marker, run_dir=run_dir)

        # Pass a nonexistent transcript path
        missing = tmp_path / "no-such-file.jsonl"

        self._call(missing)

        # Marker should be cleaned up, no crash
        assert not marker.exists()
        assert not (run_dir / "transcript.jsonl").exists()
