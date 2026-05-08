"""Docker-based integration tests for CLI session commands.

These tests run inside a session-scoped container (docker_in marker) for
complete filesystem isolation. Even if test code has bugs, it can't corrupt
host ~/.forge/ or ~/.claude/ directories.

Uses mock claude binary to test session lifecycle without launching real Claude.
"""

from __future__ import annotations

import json
from inspect import cleandoc

import pytest

from tests.fixtures.docker import ContainerLike

pytestmark = [pytest.mark.integration, pytest.mark.docker_in]


def _run_container_python(workspace: ContainerLike, script: str) -> None:
    """Write and run a Python setup script inside the integration container."""
    write_result = workspace.write_file("/tmp/forge-test-script.py", cleandoc(script))
    assert write_result.returncode == 0, write_result.stderr

    run_result = workspace.exec("/forge/.venv/bin/python /tmp/forge-test-script.py")
    assert run_result.returncode == 0, run_result.stderr


class TestSessionList:
    """Tests for 'forge session list' command."""

    def test_list_empty_shows_message(self, mock_claude_workspace: ContainerLike) -> None:
        """Should show message when no sessions exist."""
        result = mock_claude_workspace.exec("cd /workspace && forge session list")

        assert result.returncode == 0
        assert "No sessions found" in result.stdout

    def test_list_shows_sessions(self, mock_claude_workspace: ContainerLike) -> None:
        """Should list existing sessions."""
        # Create a session first
        mock_claude_workspace.exec("cd /workspace && forge session start test-session")

        result = mock_claude_workspace.exec("cd /workspace && forge session list")

        assert result.returncode == 0
        assert "test-session" in result.stdout


class TestSessionShow:
    """Tests for 'forge session show' command."""

    def test_show_no_args_no_env_shows_message(self, mock_claude_workspace: ContainerLike) -> None:
        """Without name or FORGE_SESSION, shows guidance message."""
        result = mock_claude_workspace.exec("cd /workspace && forge session show")

        assert result.returncode == 0
        assert "No session specified" in result.stdout

    def test_show_with_forge_session_env(self, mock_claude_workspace: ContainerLike) -> None:
        """FORGE_SESSION env var resolves the session."""
        mock_claude_workspace.exec("cd /workspace && forge session start env-test")

        result = mock_claude_workspace.exec("cd /workspace && FORGE_SESSION=env-test forge session show")

        assert result.returncode == 0
        assert "env-test" in result.stdout


class TestSessionInspect:
    """Tests for 'forge session show <name>' command."""

    def test_inspect_shows_details(self, mock_claude_workspace: ContainerLike) -> None:
        """Should show detailed session info."""
        mock_claude_workspace.exec("cd /workspace && forge session start inspect-test")

        result = mock_claude_workspace.exec("cd /workspace && forge session show inspect-test")

        assert result.returncode == 0
        assert "inspect-test" in result.stdout
        # Check for session info markers
        assert "UUID" in result.stdout or "Basic Info" in result.stdout

    def test_inspect_nonexistent_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail for nonexistent session."""
        result = mock_claude_workspace.exec("cd /workspace && forge session show nonexistent")

        assert result.returncode == 1
        assert "No session found" in result.stdout or "No session found" in result.stderr


class TestSessionStart:
    """Tests for 'forge session start' command."""

    def test_start_creates_session(self, mock_claude_workspace: ContainerLike) -> None:
        """Should create a new session."""
        result = mock_claude_workspace.exec("cd /workspace && forge session start new-session")

        assert result.returncode == 0
        assert "Created session" in result.stdout
        assert "new-session" in result.stdout

    def test_start_creates_manifest_file(self, mock_claude_workspace: ContainerLike) -> None:
        """Should create per-session manifest."""
        mock_claude_workspace.exec("cd /workspace && forge session start manifest-test")

        # Check manifest file exists and contains session name
        result = mock_claude_workspace.exec("cat /workspace/.forge/sessions/manifest-test/forge.session.json")

        assert result.returncode == 0
        assert "manifest-test" in result.stdout
        assert '"name"' in result.stdout

    def test_start_defaults_to_direct(self, mock_claude_workspace: ContainerLike) -> None:
        """No flags should default to direct mode."""
        result = mock_claude_workspace.exec("cd /workspace && forge session start direct-default")

        assert result.returncode == 0
        assert "Routing: direct" in result.stdout

    def test_start_invokes_claude(
        self,
        mock_claude_workspace: ContainerLike,
    ) -> None:
        """Should invoke claude binary (UUID is launch-owned, no --session-id)."""
        mock_claude_workspace.exec("cd /workspace && forge session start invoke-test")

        # Check that mock claude was invoked
        result = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")

        assert result.returncode == 0
        assert "claude" in result.stdout
        # UUID pre-seeded at launch: --session-id and --name passed
        assert "--session-id" in result.stdout
        assert "--name" in result.stdout

    def test_start_duplicate_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail when session already exists."""
        mock_claude_workspace.exec("cd /workspace && forge session start duplicate-test")

        result = mock_claude_workspace.exec("cd /workspace && forge session start duplicate-test")

        assert result.returncode == 1
        assert "already exists" in result.stdout or "already exists" in result.stderr

    def test_start_with_direct_model_pins_claude_env(self, mock_claude_workspace: ContainerLike) -> None:
        """--model is stored as direct intent and launched through Claude Code env pins."""
        result = mock_claude_workspace.exec("cd /workspace && forge session start model-test --model opus-4-7")

        assert result.returncode == 0, result.stderr
        assert "Routing: direct" in result.stdout

        manifest = json.loads(
            mock_claude_workspace.read_file("/workspace/.forge/sessions/model-test/forge.session.json")
        )
        assert manifest["intent"]["launch"]["direct_model"] == "claude-opus-4-7"

        invocations = mock_claude_workspace.read_file("/tmp/claude_invocations.log")
        assert "--model" not in invocations

        env_path = mock_claude_workspace.exec("ls -1 /tmp/claude_env_*.log | head -n 1").stdout.strip()
        assert env_path
        env_text = mock_claude_workspace.read_file(env_path)
        assert "ANTHROPIC_MODEL=opus" in env_text
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7" in env_text
        assert "ANTHROPIC_BASE_URL=" not in env_text


class TestSessionDelete:
    """Tests for 'forge session delete' command."""

    def test_delete_removes_session(self, mock_claude_workspace: ContainerLike) -> None:
        """Should delete the session."""
        mock_claude_workspace.exec("cd /workspace && forge session start delete-test")

        result = mock_claude_workspace.exec("cd /workspace && forge session delete delete-test --yes")

        assert result.returncode == 0
        assert "Deleted session" in result.stdout

    def test_delete_removes_manifest(self, mock_claude_workspace: ContainerLike) -> None:
        """Should remove manifest file."""
        mock_claude_workspace.exec("cd /workspace && forge session start remove-test")

        # Verify manifest exists
        check_before = mock_claude_workspace.exec("test -f /workspace/.forge/sessions/remove-test/forge.session.json")
        assert check_before.returncode == 0

        # Delete session
        mock_claude_workspace.exec("cd /workspace && forge session delete remove-test --yes")

        # Verify manifest is gone
        check_after = mock_claude_workspace.exec("test -f /workspace/.forge/sessions/remove-test/forge.session.json")
        assert check_after.returncode != 0

    def test_delete_nonexistent_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail for nonexistent session."""
        result = mock_claude_workspace.exec("cd /workspace && forge session delete nonexistent --yes")

        assert result.returncode == 1
        assert "not found" in result.stdout or "not found" in result.stderr


class TestSessionResume:
    """Tests for 'forge session resume' command."""

    def test_resume_existing_session(self, mock_claude_workspace: ContainerLike) -> None:
        """Resume without --fresh launches in-place (session never confirmed by hook)."""
        mock_claude_workspace.exec("cd /workspace && forge session start resume-test")

        result = mock_claude_workspace.exec("cd /workspace && forge session resume resume-test")

        assert result.returncode == 0
        # Session was just started (never hook-confirmed), so resume launches in-place
        assert "Start fresh Claude session" in result.stdout

    def test_resume_invokes_claude_as_new_session(self, mock_claude_workspace: ContainerLike) -> None:
        """With --fresh, should invoke claude as NEW session with context file (not --resume)."""
        mock_claude_workspace.exec("cd /workspace && forge session start resume-invoke-test")

        # Clear previous invocations
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        mock_claude_workspace.exec("cd /workspace && forge session resume resume-invoke-test --fresh")

        # --fresh creates a derived session with context assembled from parent
        result = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")

        # Should NOT have --resume flag (it's a new session with context file)
        assert "--resume" not in result.stdout
        # Should have been invoked (just checking claude was called)
        assert "claude" in result.stdout
        assert "--append-system-prompt-file /workspace/.forge/prev_sessions/resume-invoke-test.md" in result.stdout

    def test_resume_nonexistent_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail for nonexistent session."""
        result = mock_claude_workspace.exec("cd /workspace && forge session resume nonexistent")

        assert result.returncode == 1
        assert "not found" in result.stdout or "not found" in result.stderr


class TestSessionResumeScenarios:
    """Tests for 'forge session resume' behavior across different session states."""

    def test_resume_with_transcript_reconnects(self, mock_claude_workspace: ContainerLike) -> None:
        """Previously-used session should reconnect."""
        mock_claude_workspace.exec("cd /workspace && forge session start launch-existing --no-launch")
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            path = Path("/workspace/.forge/sessions/launch-existing/forge.session.json")
            data = json.loads(path.read_text())
            # Simulate a previously-launched session (hook confirmed the UUID)
            data["confirmed"]["claude_session_id"] = "existing-uuid-123"
            data["confirmed"]["confirmed_by"] = "hook:SessionStart:startup"
            data["confirmed"]["transcript_path"] = "/tmp/transcript.jsonl"
            path.write_text(json.dumps(data))
            """,
        )
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        result = mock_claude_workspace.exec("cd /workspace && forge session resume launch-existing")

        assert result.returncode == 0
        assert "Reconnecting" in result.stdout
        assert "Reconnect to existing Claude conversation" in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        assert "--resume" in invocations.stdout

    def test_resume_never_used_session_starts_fresh(self, mock_claude_workspace: ContainerLike) -> None:
        """Never-launched session should start fresh (UUID set by hook, not CLI)."""
        mock_claude_workspace.exec("cd /workspace && forge session start launch-fresh --no-launch")
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        result = mock_claude_workspace.exec("cd /workspace && forge session resume launch-fresh")

        assert result.returncode == 0
        assert "Start fresh Claude session" in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        # UUID pre-seeded at launch
        assert "--session-id" in invocations.stdout
        assert "--name" in invocations.stdout
        assert "claude" in invocations.stdout

    def test_resume_unconfirmed_session_starts_fresh(self, mock_claude_workspace: ContainerLike) -> None:
        """Session without hook confirmation (never launched) should start fresh."""
        mock_claude_workspace.exec("cd /workspace && forge session start launch-test --no-launch")
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        result = mock_claude_workspace.exec("cd /workspace && forge session resume launch-test")

        assert result.returncode == 0
        assert "Start fresh Claude session" in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        # UUID pre-seeded for fresh session, no --resume (never launched)
        assert "--session-id" in invocations.stdout
        assert "--name" in invocations.stdout
        assert "--resume" not in invocations.stdout

    def test_resume_confirmed_session_reconnects(self, mock_claude_workspace: ContainerLike) -> None:
        """Hook-confirmed (previously used) session should reconnect."""
        mock_claude_workspace.exec("cd /workspace && forge session start launch-confirmed --no-launch")
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            path = Path("/workspace/.forge/sessions/launch-confirmed/forge.session.json")
            data = json.loads(path.read_text())
            data["confirmed"]["claude_session_id"] = "resume-uuid-123"
            data["confirmed"]["confirmed_by"] = "hook:SessionStart:startup"
            data["confirmed"]["transcript_path"] = "/tmp/transcript.jsonl"
            path.write_text(json.dumps(data))
            """,
        )
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        result = mock_claude_workspace.exec("cd /workspace && forge session resume launch-confirmed")

        assert result.returncode == 0
        assert "Reconnecting" in result.stdout
        assert "Reconnect to existing Claude conversation" in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        assert "--resume" in invocations.stdout

    def test_worktree_fork_injects_parent_context(self, mock_claude_workspace: ContainerLike) -> None:
        """Worktree fork should generate parent handoff context and pass it to Claude."""
        mock_claude_workspace.exec("cd /workspace && forge session start fork-parent --no-launch")
        # Seed a parent transcript so handoff can extract context
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            from forge.session.claude.paths import get_transcript_path

            parent_path = Path("/workspace/.forge/sessions/fork-parent/forge.session.json")
            parent = json.loads(parent_path.read_text())
            # UUID starts None, simulate hook confirmation
            parent["confirmed"]["claude_session_id"] = "parent-uuid-001"
            parent_session_id = "parent-uuid-001"
            transcript = get_transcript_path("/workspace", parent_session_id)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                '{"requestId":"r1","timestamp":"2025-01-15T10:00:00Z","message":{"role":"user","content":[{"type":"text","text":"hello from parent"}]}}\\n',
                encoding="utf-8",
            )
            parent["confirmed"]["transcript_path"] = str(transcript)
            parent_path.write_text(json.dumps(parent))
            """,
        )
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session fork fork-parent --name fork-child --worktree"
        )

        assert result.returncode == 0
        assert "Context:" in result.stdout
        assert "Transcript not found" not in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        assert "--append-system-prompt-file" in invocations.stdout
        # Worktree fork has no --session-id (UUID hook-owned)
        assert "--fork-session" not in invocations.stdout

        context_file = mock_claude_workspace.exec("cat /workspace-fork-child/.forge/prev_sessions/fork-parent.md")
        assert "# Session Context: fork-parent" in context_file.stdout
        assert "hello from parent" in context_file.stdout

    def test_worktree_fork_handles_requestless_legacy_transcript(self, mock_claude_workspace: ContainerLike) -> None:
        """Worktree fork should summarize legacy transcript entries without request IDs."""
        mock_claude_workspace.exec("cd /workspace && forge session start legacy-parent --no-launch")
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            from forge.session.claude.paths import get_transcript_path

            parent_path = Path("/workspace/.forge/sessions/legacy-parent/forge.session.json")
            parent = json.loads(parent_path.read_text())
            # UUID starts None, simulate hook confirmation
            parent["confirmed"]["claude_session_id"] = "parent-uuid-001"
            parent_session_id = "parent-uuid-001"
            transcript = get_transcript_path("/workspace", parent_session_id)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                '{"type":"user","timestamp":"2025-01-15T10:00:00Z","message":{"content":[{"type":"text","text":"legacy hello from parent"}]}}\\n'
                '{"type":"assistant","timestamp":"2025-01-15T10:00:01Z","message":{"content":[{"type":"text","text":"legacy response from assistant"}]}}\\n',
                encoding="utf-8",
            )
            parent["confirmed"]["transcript_path"] = str(transcript)
            parent_path.write_text(json.dumps(parent))
            """,
        )

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session fork legacy-parent --name legacy-child --worktree --no-launch"
        )

        assert result.returncode == 0
        assert "no valid turns" not in result.stdout.lower()

        context_file = mock_claude_workspace.exec("cat /workspace-legacy-child/.forge/prev_sessions/legacy-parent.md")
        assert "# Session Context: legacy-parent" in context_file.stdout
        assert "legacy hello from parent" in context_file.stdout
        assert "legacy response from assistant" in context_file.stdout

    def test_worktree_fork_refreshes_stale_parent_context(self, mock_claude_workspace: ContainerLike) -> None:
        """Worktree fork should regenerate context instead of reusing stale parent handoff files."""
        mock_claude_workspace.exec("cd /workspace && forge session start stale-parent --no-launch")
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            from forge.session.claude.paths import get_transcript_path

            parent_path = Path("/workspace/.forge/sessions/stale-parent/forge.session.json")
            parent = json.loads(parent_path.read_text())
            # UUID starts None, simulate hook confirmation
            parent["confirmed"]["claude_session_id"] = "parent-uuid-001"
            parent_session_id = "parent-uuid-001"
            transcript = get_transcript_path("/workspace", parent_session_id)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                '{"requestId":"r1","timestamp":"2025-01-15T10:00:00Z","message":{"role":"user","content":[{"type":"text","text":"fresh context from transcript"}]}}\\n',
                encoding="utf-8",
            )
            parent["confirmed"]["transcript_path"] = str(transcript)
            parent_path.write_text(json.dumps(parent))

            stale_context = Path("/workspace/.forge/prev_sessions/stale-parent.md")
            stale_context.parent.mkdir(parents=True, exist_ok=True)
            stale_context.write_text("# Session Context: stale-parent\\n\\nstale context file\\n", encoding="utf-8")
            """,
        )

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session fork stale-parent --name stale-child --worktree --no-launch"
        )

        assert result.returncode == 0
        context_file = mock_claude_workspace.exec("cat /workspace-stale-child/.forge/prev_sessions/stale-parent.md")
        assert "fresh context from transcript" in context_file.stdout
        assert "stale context file" not in context_file.stdout

    def test_fork_default_same_directory_uses_fork_session(self, mock_claude_workspace: ContainerLike) -> None:
        """Default fork stays in parent's dir and invokes Claude with --fork-session."""
        mock_claude_workspace.exec("cd /workspace && forge session start fork-parent-nw --no-launch")
        # Parent needs a UUID for fork to use --resume --fork-session
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            path = Path("/workspace/.forge/sessions/fork-parent-nw/forge.session.json")
            data = json.loads(path.read_text())
            data["confirmed"]["claude_session_id"] = "parent-nw-uuid"
            path.write_text(json.dumps(data))
            """,
        )
        mock_claude_workspace.exec("> /tmp/claude_invocations.log")

        result = mock_claude_workspace.exec("cd /workspace && forge session fork fork-parent-nw --name fork-child-nw")

        assert result.returncode == 0
        # No worktree info in output
        assert "Worktree:" not in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        assert "--resume" in invocations.stdout
        assert "--fork-session" in invocations.stdout

        # Manifest lives under /workspace (same dir as parent)
        manifest = mock_claude_workspace.exec("cat /workspace/.forge/sessions/fork-child-nw/forge.session.json")
        assert manifest.returncode == 0
        assert '"is_fork": true' in manifest.stdout

    def test_resume_deferred_same_dir_fork_uses_fork_session(self, mock_claude_workspace: ContainerLike) -> None:
        """Resuming a deferred same-dir fork should still use --resume --fork-session."""
        mock_claude_workspace.exec("cd /workspace && forge session start fork-parent-later --no-launch")
        # Parent needs a UUID for fork to work
        _run_container_python(
            mock_claude_workspace,
            """
            import json
            from pathlib import Path

            path = Path("/workspace/.forge/sessions/fork-parent-later/forge.session.json")
            data = json.loads(path.read_text())
            data["confirmed"]["claude_session_id"] = "parent-later-uuid"
            path.write_text(json.dumps(data))
            """,
        )

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session fork fork-parent-later --name fork-child-later --no-launch"
        )
        assert result.returncode == 0

        mock_claude_workspace.exec("> /tmp/claude_invocations.log")
        result = mock_claude_workspace.exec("cd /workspace && forge session resume fork-child-later")

        assert result.returncode == 0
        assert "Fork parent Claude conversation" in result.stdout

        invocations = mock_claude_workspace.exec("cat /tmp/claude_invocations.log")
        assert "--resume" in invocations.stdout
        assert "--fork-session" in invocations.stdout
        assert "--name" in invocations.stdout
        assert "--session-id" not in invocations.stdout  # Uses --resume, not --session-id
        assert "--append-system-prompt-file" not in invocations.stdout


class TestSessionSetOverride:
    """Tests for 'forge session set' command."""

    def test_set_updates_overrides(self, mock_claude_workspace: ContainerLike) -> None:
        """Should set an override value."""
        mock_claude_workspace.exec("cd /workspace && forge session start set-test")

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session set --session set-test policy.fail_mode closed"
        )

        assert result.returncode == 0
        assert "Set" in result.stdout
        assert "policy.fail_mode" in result.stdout
        assert "closed" in result.stdout

    def test_set_persists_in_manifest(self, mock_claude_workspace: ContainerLike) -> None:
        """Override should be persisted in manifest file."""
        mock_claude_workspace.exec("cd /workspace && forge session start persist-test")
        mock_claude_workspace.exec("cd /workspace && forge session set --session persist-test agent custom-agent")

        # Check manifest contains override (per-session directory layout)
        result = mock_claude_workspace.exec("find /workspace/.forge/sessions -name forge.session.json -exec cat {} \\;")

        assert "custom-agent" in result.stdout or "agent" in result.stdout

    def test_set_nested_key_not_supported(self, mock_claude_workspace: ContainerLike) -> None:
        """Should reject custom.* nested keys."""
        mock_claude_workspace.exec("cd /workspace && forge session start nested-test")

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session set --session nested-test custom.my_flag true"
        )

        assert result.returncode == 1
        assert "custom.* is not supported" in result.stdout or "not supported" in result.stderr

    def test_set_invalid_key_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail for invalid/protected keys."""
        mock_claude_workspace.exec("cd /workspace && forge session start invalid-key-test")

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session set --session invalid-key-test confirmed.claude_session_id value"
        )

        assert result.returncode == 1
        assert "cannot override" in result.stdout or "Error" in result.stderr

    def test_set_no_session_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail when no session specified and no FORGE_SESSION env."""
        result = mock_claude_workspace.exec("cd /workspace && forge session set policy.fail_mode closed")

        assert result.returncode == 1


class TestSessionReset:
    """Tests for 'forge session reset' command."""

    def test_reset_single_key(self, mock_claude_workspace: ContainerLike) -> None:
        """Should reset a single override key."""
        mock_claude_workspace.exec("cd /workspace && forge session start reset-single")
        mock_claude_workspace.exec("cd /workspace && forge session set --session reset-single policy.fail_mode closed")

        result = mock_claude_workspace.exec(
            "cd /workspace && forge session reset --session reset-single policy.fail_mode"
        )

        assert result.returncode == 0
        assert "Reset" in result.stdout or "policy.fail_mode" in result.stdout

    def test_reset_all(self, mock_claude_workspace: ContainerLike) -> None:
        """Should reset all overrides with --all."""
        mock_claude_workspace.exec("cd /workspace && forge session start reset-all")
        mock_claude_workspace.exec("cd /workspace && forge session set --session reset-all policy.fail_mode closed")

        result = mock_claude_workspace.exec("cd /workspace && forge session reset --session reset-all --all")

        assert result.returncode == 0
        assert "Cleared" in result.stdout or "all" in result.stdout.lower()

    def test_reset_no_session_fails(self, mock_claude_workspace: ContainerLike) -> None:
        """Should fail when no session specified and no FORGE_SESSION env."""
        result = mock_claude_workspace.exec("cd /workspace && forge session reset policy.fail_mode")

        assert result.returncode == 1


class TestMainGroup:
    """Tests for main CLI group."""

    def test_help(self, mock_claude_workspace: ContainerLike) -> None:
        """Should show help."""
        result = mock_claude_workspace.exec("forge --help")

        assert result.returncode == 0
        assert "Claude Forge" in result.stdout or "forge" in result.stdout.lower()

    def test_session_subcommand_help(self, mock_claude_workspace: ContainerLike) -> None:
        """Should show session subcommand help."""
        result = mock_claude_workspace.exec("forge session --help")

        assert result.returncode == 0
        assert "start" in result.stdout
        assert "resume" in result.stdout
        assert "list" in result.stdout


class TestInspectShowsOverrides:
    """Tests that inspect command shows override information."""

    def test_inspect_shows_overrides_section(self, mock_claude_workspace: ContainerLike) -> None:
        """Show should display active overrides."""
        mock_claude_workspace.exec("cd /workspace && forge session start inspect-override")
        mock_claude_workspace.exec("cd /workspace && forge session set --session reset-all policy.fail_mode closed")

        result = mock_claude_workspace.exec("cd /workspace && forge session show inspect-override")

        assert result.returncode == 0
        # Should show the override value
        assert "closed" in result.stdout or "override" in result.stdout.lower()


class TestTransactionalBehavior:
    """Tests that manifest is not modified when validation fails."""

    def test_no_write_on_invalid_key(self, mock_claude_workspace: ContainerLike) -> None:
        """Manifest file should be unchanged when set fails key validation."""
        mock_claude_workspace.exec("cd /workspace && forge session start transactional-test")

        # Read manifest before
        before = mock_claude_workspace.exec("cat /workspace/.forge/sessions/transactional-test/forge.session.json")
        content_before = before.stdout

        # Attempt to set a confirmed field (should be rejected)
        result = mock_claude_workspace.exec("cd /workspace && forge session set confirmed.foo bar")
        assert result.returncode == 1

        # Manifest should be unchanged
        after = mock_claude_workspace.exec("cat /workspace/.forge/sessions/transactional-test/forge.session.json")
        content_after = after.stdout

        assert content_before == content_after
