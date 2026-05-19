"""Docker-based E2E tests for `forge handoff run`.

Tests the full chain inside a Docker container with real filesystem:
CLI → SessionStore → effective intent → path validation → prompt → claude -p.

The mock claude binary captures both args and stdin (the prompt), so we can
verify the exact prompt content sent to the agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict

import pytest

from forge.session.models import (
    DesignatedDoc,
    HandoffConfig,
    MemoryIntent,
    create_session_state,
)
from tests.fixtures.docker import ContainerLike

pytestmark = [pytest.mark.integration, pytest.mark.docker_in]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_NAME = "handoff-test"
PROXY_TEMPLATE = "test-template"
PROXY_URL = "http://localhost:8084"
TRANSCRIPT_REL = ".forge/artifacts/handoff-test/transcripts/uuid-123.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    handoff_enabled: bool = True,
    min_turns: int = 1,
    mode: str = "augment",
    designated_docs: list[DesignatedDoc] | None = None,
) -> dict:
    """Build a session manifest dict with handoff config."""
    manifest = create_session_state(
        SESSION_NAME,
        proxy_template=PROXY_TEMPLATE,
        proxy_base_url=PROXY_URL,
    )
    manifest.intent.memory = MemoryIntent(
        auto_update=HandoffConfig(
            enabled=handoff_enabled,
            mode=mode,
            min_turns=min_turns,
        ),
        designated_docs=designated_docs or [],
    )
    return asdict(manifest)


def _build_transcript(turn_count: int = 10) -> str:
    """Build a JSONL transcript string with the given number of turns."""
    lines = []
    for i in range(turn_count):
        lines.append(
            json.dumps(
                {
                    "requestId": f"req-{i}",
                    "timestamp": f"2026-01-01T00:0{i % 10}:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"turn {i}"}],
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "requestId": f"req-{i}",
                    "timestamp": f"2026-01-01T00:0{i % 10}:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"response {i}"}],
                    },
                }
            )
        )
    return "\n".join(lines) + "\n"


def _install_outputting_claude_mock(workspace: ContainerLike) -> None:
    """Replace the default mock with one that emits stable stdout for report tests."""
    workspace.write_file(
        "/tmp/claude-mock",
        """#!/bin/bash
set -euo pipefail

if [ "${1:-}" = "--version" ]; then
    echo "99.99.99 (Claude Code)"
    exit 0
fi

pid="$$"
echo "$(date -Iseconds) claude $*" >> /tmp/claude_invocations.log
env | sort > "/tmp/claude_env_${pid}.log"

if [ ! -t 0 ]; then
    cat > "/tmp/claude_stdin_${pid}.log"
else
    : > "/tmp/claude_stdin_${pid}.log"
fi

cat <<'MOCK_STDOUT'
Review proposal: add the latest session fact to docs/state.md.
MOCK_STDOUT

exit "${FORGE_MOCK_CLAUDE_EXIT_CODE:-0}"
""",
    )
    result = workspace.exec("chmod +x /tmp/claude-mock && > /tmp/claude_invocations.log")
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandoffRunMultiDoc:
    """E2E: forge handoff run with designated_docs."""

    def _setup_session(
        self,
        workspace: ContainerLike,
        *,
        manifest_dict: dict | None = None,
        target_files: dict[str, str] | None = None,
    ) -> None:
        """Create session manifest, transcript, and optional target files."""
        # Write manifest
        manifest = manifest_dict or _build_manifest()
        workspace.mkdir(f"/workspace/.forge/sessions/{SESSION_NAME}", parents=True)
        workspace.write_json(
            f"/workspace/.forge/sessions/{SESSION_NAME}/forge.session.json",
            manifest,
        )

        # Write transcript
        transcript_path = f"/workspace/{TRANSCRIPT_REL}"
        workspace.exec(f"mkdir -p $(dirname {transcript_path})")
        workspace.write_file(transcript_path, _build_transcript())

        # Write target doc files (for non-creatable strategies)
        if target_files:
            for path, content in target_files.items():
                workspace.exec(f"mkdir -p $(dirname /workspace/{path})")
                workspace.write_file(f"/workspace/{path}", content)

    def _run_handoff(self, workspace: ContainerLike, timeout: int = 30) -> int:
        """Run forge handoff run and return exit code."""
        result = workspace.exec(
            f"cd /workspace && forge handoff run "
            f"--session-name {SESSION_NAME} "
            f"--worktree-path /workspace "
            f"--transcript-rel {TRANSCRIPT_REL} "
            f"--timeout {timeout}",
            timeout=timeout + 10,
        )
        return result.returncode

    def test_multi_doc_prompt_sent_to_claude(
        self,
        mock_claude_workspace: ContainerLike,
        claude_capture_file: Callable[[str], str],
        claude_invocations: Callable[[], list[str]],
    ) -> None:
        """Full chain: manifest with designated_docs → multi-doc prompt to claude -p."""
        self._setup_session(
            mock_claude_workspace,
            manifest_dict=_build_manifest(
                designated_docs=[
                    DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
                    DesignatedDoc(path="docs/changelog.md", strategy="changelog"),
                    DesignatedDoc(path=".forge/memory/project-state.md", strategy="project-state"),
                ],
            ),
            target_files={
                "docs/checklist.md": "# Checklist\n- [ ] task 1\n",
                "docs/changelog.md": "# Change Log\n## 2026-01-01\nInitial.\n",
                ".forge/memory/project-state.md": "# Project State\n",
            },
        )

        exit_code = self._run_handoff(mock_claude_workspace)
        assert exit_code == 0

        # Verify claude -p was invoked
        invocations = claude_invocations()
        assert any("claude -p" in inv for inv in invocations), f"Expected claude -p call, got: {invocations}"

        # Verify the prompt contains all three docs with correct strategies
        prompt = claude_capture_file("/tmp/claude_stdin_*.log")
        assert "docs/checklist.md" in prompt
        assert "docs/changelog.md" in prompt
        assert ".forge/memory/project-state.md" in prompt
        assert "Mark completed tasks" in prompt  # checklist strategy
        assert "accomplishments" in prompt  # changelog strategy

    def test_no_designated_docs_skips_cleanly(
        self,
        mock_claude_workspace: ContainerLike,
        claude_invocations: Callable[[], list[str]],
    ) -> None:
        """No designated_docs → exit 0, no claude -p call (nothing to update)."""
        self._setup_session(mock_claude_workspace, manifest_dict=_build_manifest())

        exit_code = self._run_handoff(mock_claude_workspace)
        assert exit_code == 0

        invocations = claude_invocations()
        assert not any("claude -p" in inv for inv in invocations)

    def test_missing_docs_filtered(
        self,
        mock_claude_workspace: ContainerLike,
        claude_capture_file: Callable[[str], str],
    ) -> None:
        """Docs that don't exist on disk are filtered from the prompt."""
        self._setup_session(
            mock_claude_workspace,
            manifest_dict=_build_manifest(
                designated_docs=[
                    DesignatedDoc(path="docs/nonexistent.md", strategy="checklist"),
                    DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
                ],
            ),
            target_files={
                "docs/checklist.md": "# Checklist\n",
            },
            # Note: docs/nonexistent.md NOT created — should be filtered
        )

        exit_code = self._run_handoff(mock_claude_workspace)
        assert exit_code == 0

        prompt = claude_capture_file("/tmp/claude_stdin_*.log")
        assert "docs/checklist.md" in prompt
        assert "docs/nonexistent.md" not in prompt

    def test_no_file_creation_for_missing_docs(
        self,
        mock_claude_workspace: ContainerLike,
        claude_invocations: Callable[[], list[str]],
    ) -> None:
        """Missing docs are skipped — no directory creation, no claude -p call."""
        self._setup_session(
            mock_claude_workspace,
            manifest_dict=_build_manifest(
                designated_docs=[
                    DesignatedDoc(path=".forge/memory/project-state.md", strategy="project-state"),
                ],
            ),
        )

        # Verify directory doesn't exist
        check = mock_claude_workspace.exec("test -d /workspace/.forge/memory && echo yes || echo no")
        assert "no" in check.stdout

        exit_code = self._run_handoff(mock_claude_workspace)
        assert exit_code == 0

        # Directory still should NOT exist (no file creation)
        check = mock_claude_workspace.exec("test -d /workspace/.forge/memory && echo yes || echo no")
        assert "no" in check.stdout

        # No claude -p call (all docs missing)
        invocations = claude_invocations()
        assert not any("claude -p" in inv for inv in invocations)

    def test_review_only_mode(
        self,
        mock_claude_workspace: ContainerLike,
        claude_capture_file: Callable[[str], str],
    ) -> None:
        """Review-only mode → prompt says 'Do NOT modify any files'."""
        self._setup_session(
            mock_claude_workspace,
            manifest_dict=_build_manifest(
                mode="review-only",
                designated_docs=[
                    DesignatedDoc(path="docs/state.md", strategy="project-state"),
                ],
            ),
            target_files={
                "docs/state.md": "# State\n",
            },
        )

        exit_code = self._run_handoff(mock_claude_workspace)
        assert exit_code == 0

        prompt = claude_capture_file("/tmp/claude_stdin_*.log")
        assert "Do NOT modify any files" in prompt

    def test_memory_cli_review_only_report_is_visible(
        self,
        mock_claude_workspace: ContainerLike,
        claude_capture_file: Callable[[str], str],
    ) -> None:
        """memory add-doc → handoff review-only → session handoff show exposes the report."""
        session_name = "memory-review"
        transcript_rel = f".forge/artifacts/{session_name}/transcripts/uuid-456.jsonl"
        _install_outputting_claude_mock(mock_claude_workspace)

        result = mock_claude_workspace.exec(f"cd /workspace && forge session start {session_name} --no-launch")
        assert result.returncode == 0, result.stderr
        mock_claude_workspace.exec("mkdir -p /workspace/docs")
        mock_claude_workspace.write_file("/workspace/docs/state.md", "# State\n")
        mock_claude_workspace.exec(f"mkdir -p /workspace/$(dirname {transcript_rel})")
        mock_claude_workspace.write_file(f"/workspace/{transcript_rel}", _build_transcript())

        result = mock_claude_workspace.exec(
            f"cd /workspace && forge session memory add-doc docs/state.md "
            f"--strategy project-state --session {session_name}"
        )
        assert result.returncode == 0, result.stderr
        for key, value in (
            ("memory.auto_update.enabled", "true"),
            ("memory.auto_update.mode", "review-only"),
            ("memory.auto_update.min_turns", "1"),
        ):
            result = mock_claude_workspace.exec(
                f"cd /workspace && forge session set --session {session_name} {key} {value}"
            )
            assert result.returncode == 0, result.stderr

        list_result = mock_claude_workspace.exec(
            f"cd /workspace && forge session memory list-docs --session {session_name} --json"
        )
        assert list_result.returncode == 0, list_result.stderr
        assert json.loads(list_result.stdout) == [
            {"path": "docs/state.md", "strategy": "project-state", "shadows": None}
        ]

        result = mock_claude_workspace.exec(
            "cd /workspace && forge handoff run "
            f"--session-name {session_name} "
            "--worktree-path /workspace "
            f"--transcript-rel {transcript_rel} "
            "--timeout 5",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr

        prompt = claude_capture_file("/tmp/claude_stdin_*.log")
        assert "Do NOT modify any files" in prompt
        assert "docs/state.md" in prompt

        show_result = mock_claude_workspace.exec(f"cd /workspace && forge session handoff show {session_name} --latest")
        assert show_result.returncode == 0, show_result.stderr
        assert "Handoff Agent Report -- memory-review" in show_result.stdout
        assert "Mode**: review-only" in show_result.stdout
        assert "Review proposal: add the latest session fact to docs/state.md." in show_result.stdout

    def test_shadow_doc_prompt(
        self,
        mock_claude_workspace: ContainerLike,
        claude_capture_file: Callable[[str], str],
    ) -> None:
        """Shadow doc: prompt reads official doc first, proposes changes to shadow."""
        self._setup_session(
            mock_claude_workspace,
            manifest_dict=_build_manifest(
                designated_docs=[
                    DesignatedDoc(
                        path=".forge/memory/suggested.md",
                        strategy="suggested",
                        shadows="STANDARDS.md",
                    ),
                ],
            ),
            target_files={
                ".forge/memory/suggested.md": "# Suggested\n",
                "STANDARDS.md": "# Standards\n",
            },
        )

        exit_code = self._run_handoff(mock_claude_workspace)
        assert exit_code == 0

        prompt = claude_capture_file("/tmp/claude_stdin_*.log")
        assert "proposes changes to `STANDARDS.md`" in prompt
        assert "Read the OFFICIAL document" in prompt


class TestHandoffRunDisabled:
    """E2E: forge handoff run when handoff is disabled or not configured."""

    def _setup_session(self, workspace: ContainerLike, manifest_dict: dict) -> None:
        """Create session manifest and transcript."""
        workspace.mkdir(f"/workspace/.forge/sessions/{SESSION_NAME}", parents=True)
        workspace.write_json(
            f"/workspace/.forge/sessions/{SESSION_NAME}/forge.session.json",
            manifest_dict,
        )
        transcript_path = f"/workspace/{TRANSCRIPT_REL}"
        workspace.exec(f"mkdir -p $(dirname {transcript_path})")
        workspace.write_file(transcript_path, _build_transcript())

    def test_disabled_exits_clean(
        self,
        mock_claude_workspace: ContainerLike,
        claude_invocations: Callable[[], list[str]],
    ) -> None:
        """Handoff enabled=false → exit 0, no claude -p call."""
        self._setup_session(
            mock_claude_workspace,
            _build_manifest(handoff_enabled=False),
        )

        result = mock_claude_workspace.exec(
            f"cd /workspace && forge handoff run "
            f"--session-name {SESSION_NAME} "
            f"--worktree-path /workspace "
            f"--transcript-rel {TRANSCRIPT_REL}"
        )
        assert result.returncode == 0

        invocations = claude_invocations()
        assert not any("claude -p" in inv for inv in invocations)

    def test_missing_session_exits_clean(
        self,
        mock_claude_workspace: ContainerLike,
        claude_invocations: Callable[[], list[str]],
    ) -> None:
        """Missing session manifest → exit 0, no claude -p call."""
        # Don't create any manifest — just the transcript
        workspace = mock_claude_workspace
        transcript_path = f"/workspace/{TRANSCRIPT_REL}"
        workspace.exec(f"mkdir -p $(dirname {transcript_path})")
        workspace.write_file(transcript_path, _build_transcript())

        result = workspace.exec(
            f"cd /workspace && forge handoff run "
            f"--session-name nonexistent-session "
            f"--worktree-path /workspace "
            f"--transcript-rel {TRANSCRIPT_REL}"
        )
        assert result.returncode == 0

        invocations = claude_invocations()
        assert not any("claude -p" in inv for inv in invocations)

    def test_below_min_turns_skips(
        self,
        mock_claude_workspace: ContainerLike,
        claude_invocations: Callable[[], list[str]],
    ) -> None:
        """Session below min_turns → exit 0, no claude -p call."""
        self._setup_session(
            mock_claude_workspace,
            _build_manifest(min_turns=100),  # transcript has 10 turns
        )

        result = mock_claude_workspace.exec(
            f"cd /workspace && forge handoff run "
            f"--session-name {SESSION_NAME} "
            f"--worktree-path /workspace "
            f"--transcript-rel {TRANSCRIPT_REL}"
        )
        assert result.returncode == 0

        invocations = claude_invocations()
        assert not any("claude -p" in inv for inv in invocations)
