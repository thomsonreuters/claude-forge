"""E2E tests for project identity model.

Verifies:
- forge_root, checkout_root, relative_path populated in index entries
- public v1 index shape rejects pre-OSS bare-key indexes
- status line and hooks use FORGE_SESSION only (no CWD fallback)
- Rule 1 enforcement (session start requires .forge/)
- fork --into preserves relative_path position
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from tests.fixtures.docker import ContainerLike


def _entry_by_name(index: Mapping[str, object], name: str) -> dict[str, object]:
    """Return the raw v1 index entry for a display name."""
    sessions = index.get("sessions", {})
    assert isinstance(sessions, dict), f"Invalid sessions payload: {type(sessions)!r}"

    matches = [entry for key, entry in sessions.items() if key.split("|", 1)[0] == name]
    assert len(matches) == 1, f"Expected exactly one index entry for {name!r}, found {len(matches)}"

    entry = matches[0]
    assert isinstance(entry, dict), f"Invalid entry payload for {name!r}: {type(entry)!r}"
    return entry


pytestmark = [pytest.mark.integration, pytest.mark.docker_in]


class TestPhase0IdentityFields:
    """New identity fields are populated on session creation."""

    def test_start_session_populates_identity_fields(self, forge_workspace: ContainerLike) -> None:
        """forge session start should populate forge_root, checkout_root, relative_path in the index."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        result = forge_workspace.exec("cd /workspace && forge session start identity-test")
        assert result.returncode == 0, f"Session start failed: {result.stderr}"

        # Read the global session index and check identity fields
        index = forge_workspace.read_json("$HOME/.forge/sessions/index.json")
        entry = _entry_by_name(index, "identity-test")

        assert entry["forge_root"], "forge_root should be populated"
        assert entry["checkout_root"], "checkout_root should be populated"
        assert entry["relative_path"], "relative_path should be populated"
        # For a session at repo root, forge_root == checkout_root and relative_path == "."
        assert entry["forge_root"] == entry["checkout_root"]
        assert entry["relative_path"] == "."

    def test_session_state_has_forge_root(self, forge_workspace: ContainerLike) -> None:
        """Session manifest should have forge_root field."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        result = forge_workspace.exec("cd /workspace && forge session start state-test")
        assert result.returncode == 0, f"Session start failed: {result.stderr}"

        manifest = forge_workspace.read_json("/workspace/.forge/sessions/state-test/forge.session.json")
        assert manifest.get("forge_root") is not None, "forge_root should be in session state"


class TestPhase1IndexShapeValidation:
    """Public v1 index shape rejects pre-OSS v1 files."""

    def test_pre_oss_v1_bare_key_index_rejected(self, forge_workspace: ContainerLike) -> None:
        """A pre-OSS v1 bare-key index should fail with reset instructions."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        # Ensure the index directory exists
        forge_workspace.mkdir("$HOME/.forge/sessions")

        # Write the old pre-OSS v1 shape manually. Public v1 keeps the same
        # version number, so readers distinguish this by structure.
        v1_index = {
            "version": 1,
            "sessions": {
                "legacy-session": {
                    "worktree_path": "/workspace",
                    "project_root": "/workspace",
                    "last_accessed_at": "2024-01-01T00:00:00+00:00",
                    "is_fork": False,
                    "is_incognito": False,
                    "parent_session": None,
                    "claude_session_id": None,
                }
            },
        }
        forge_workspace.write_json("$HOME/.forge/sessions/index.json", v1_index)

        # Also create the manifest so self-healing doesn't prune it
        forge_workspace.mkdir("/workspace/.forge/sessions/legacy-session")
        forge_workspace.write_file(
            "/workspace/.forge/sessions/legacy-session/forge.session.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "legacy-session",
                    "created_at": "2024-01-01T00:00:00",
                    "last_accessed_at": "2024-01-01T00:00:00",
                    "intent": {},
                    "overrides": {},
                    "confirmed": {},
                }
            ),
        )

        # Trigger a read by listing sessions
        result = forge_workspace.exec("cd /workspace && forge session list")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        normalized = " ".join(combined.split())
        assert "pre-OSS session index shape" in normalized
        assert "delete ~/.forge/sessions/index.json" in normalized


class TestPhase2NoCwdFallback:
    """Status line and hooks use FORGE_SESSION only."""

    def test_status_line_no_session_without_env(self, forge_workspace: ContainerLike) -> None:
        """Status line should not detect sessions via CWD when FORGE_SESSION is unset."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        # Create a session (creates .forge/sessions/ in CWD)
        forge_workspace.exec("cd /workspace && forge session start cwd-test")

        # Run status line WITHOUT FORGE_SESSION set
        # Feed it the minimal JSON that Claude Code sends
        result = forge_workspace.exec("""
            unset FORGE_SESSION
            cd /workspace && echo '{"cwd":"/workspace","session_id":"test-uuid"}' | forge status-line
        """)
        assert result.returncode == 0

        # The session name should NOT appear in the status line output
        # (no CWD fallback means no session detected)
        assert "cwd-test" not in result.stdout, (
            f"Status line should not show session without FORGE_SESSION env var. " f"Output: {result.stdout}"
        )

    def test_status_line_shows_session_with_env(self, forge_workspace: ContainerLike) -> None:
        """Status line should detect session when FORGE_SESSION is set."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start env-test")

        # Run status line WITH FORGE_SESSION set
        result = forge_workspace.exec("""
            export FORGE_SESSION=env-test
            cd /workspace && echo '{"cwd":"/workspace","session_id":"test-uuid"}' | forge status-line
        """)
        assert result.returncode == 0
        # Session name should appear in breadcrumb
        assert "env-test" in result.stdout, (
            f"Status line should show session when FORGE_SESSION is set. " f"Output: {result.stdout}"
        )

    def test_hook_resolution_without_env_fails_gracefully(self, forge_workspace: ContainerLike) -> None:
        """Hook session resolution should return None without FORGE_SESSION (no CWD scan)."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        # Create a session and its manifest
        forge_workspace.exec("cd /workspace && forge session start hook-test")

        # Run a python snippet that tests resolve_session_for_hook without env vars
        result = forge_workspace.exec("""
            unset FORGE_SESSION
            unset FORGE_FORK_NAME
            cd /forge && uv run python -c "
from pathlib import Path
from forge.session.hooks.session_start import resolve_session_for_hook

name = resolve_session_for_hook(Path('/workspace'), session_id='nonexistent-uuid')
print(f'resolved={name}')
"
        """)
        assert result.returncode == 0, f"Resolution check failed: {result.stderr}"
        assert "resolved=None" in result.stdout, f"Should resolve to None without env vars. Output: {result.stdout}"


class TestPhase3SessionListScopes:
    """Session list defaults to repo scope and supports --scope."""

    def test_session_list_scope_matrix(self, forge_workspace: ContainerLike) -> None:
        """repo/project/all scopes should split sessions by logical repo vs Forge project."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        root_result = forge_workspace.exec("cd /workspace && forge session start repo-root --no-launch")
        assert root_result.returncode == 0, f"Root session start failed: {root_result.stderr}"

        worktree_result = forge_workspace.exec(
            "cd /workspace && forge session start repo-worktree --worktree --no-launch"
        )
        assert worktree_result.returncode == 0, f"Worktree session start failed: {worktree_result.stderr}"

        other_repo_result = forge_workspace.exec("""
            mkdir -p /workspace-other && cd /workspace-other
            git init -b main
            git config user.email "test@forge.local"
            git config user.name "Forge Test"
            echo "# Other Repo" > README.md
            git add . && git commit -m "init"
            mkdir -p .forge .claude
            forge session start other-repo --no-launch
        """)
        assert other_repo_result.returncode == 0, f"Other repo session start failed: {other_repo_result.stderr}"

        repo_list = forge_workspace.exec("cd /workspace && forge session list --json")
        assert repo_list.returncode == 0, f"Repo-scope list failed: {repo_list.stderr}"
        repo_names = {item["name"] for item in json.loads(repo_list.stdout)}
        assert repo_names == {"repo-root", "repo-worktree"}

        project_list = forge_workspace.exec("cd /workspace && forge session list --scope project --json")
        assert project_list.returncode == 0, f"Project-scope list failed: {project_list.stderr}"
        project_names = {item["name"] for item in json.loads(project_list.stdout)}
        # Exact-root semantics: --worktree sessions inherit the parent forge_root,
        # so they remain in the same Forge project as the root session.
        assert project_names == {"repo-root", "repo-worktree"}

        all_list = forge_workspace.exec("cd /workspace && forge session list --scope all --json")
        assert all_list.returncode == 0, f"Global-scope list failed: {all_list.stderr}"
        all_names = {item["name"] for item in json.loads(all_list.stdout)}
        assert all_names == {"repo-root", "repo-worktree", "other-repo"}


class TestPhase7Rule1Enforcement:
    """Session start requires .forge/ (Rule 1)."""

    def test_enable_then_start_succeeds(self, forge_workspace: ContainerLike) -> None:
        """Full flow: enable creates .forge/, then session start succeeds."""
        # Start from a completely clean repo (no .forge/, no .claude/)
        result = forge_workspace.exec("""
            cd /workspace
            forge extensions enable --scope local --profile minimal
            forge session start rule1-test --no-launch
        """)
        assert result.returncode == 0, f"Enable+start failed: {result.stderr}"

        # Verify .forge/ was created by enable and session landed there
        assert forge_workspace.file_exists("/workspace/.forge/sessions/rule1-test/forge.session.json")

    def test_start_fails_without_enable(self, forge_workspace: ContainerLike) -> None:
        """Session start without prior enable fails with clear error."""
        forge_workspace.exec("rm -rf /workspace/.forge")
        result = forge_workspace.exec("cd /workspace && forge session start no-enable --no-launch")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "forge extension enable" in combined.lower(), f"Error should mention enable. Output: {combined}"

    def test_worktree_start_uses_parent_forge_root(self, forge_workspace: ContainerLike) -> None:
        """--worktree session resolves forge_root from the parent repo, not the new worktree."""
        forge_workspace.exec("cd /workspace && forge extensions enable --scope local --profile minimal")

        result = forge_workspace.exec("cd /workspace && forge session start wt-rule1 --worktree --no-proxy --no-launch")
        assert result.returncode == 0, f"Worktree start failed: {result.stderr}\n{result.stdout}"

        # Session manifest should live under the parent's .forge/, not the worktree's
        assert forge_workspace.file_exists("/workspace/.forge/sessions/wt-rule1/forge.session.json")


class TestPhase8ForkIntoRelativePath:
    """fork --into preserves relative_path position."""

    def test_fork_into_worktree_project(self, forge_workspace: ContainerLike) -> None:
        """Fork --into a real git worktree with .forge/ lands correctly."""
        # Create parent session
        forge_workspace.exec("cd /workspace && forge session start parent-fork --no-launch")

        # Create a real git worktree (--into requires an existing worktree, not a standalone repo)
        forge_workspace.exec("""
            cd /workspace && git worktree add /tmp/target-wt -b fork-target
            mkdir -p /tmp/target-wt/.forge
        """)

        # Fork into the worktree
        result = forge_workspace.exec(
            "cd /workspace && forge session fork parent-fork --name child-fork --into /tmp/target-wt --no-launch"
        )
        assert result.returncode == 0, f"Fork --into failed: {result.stderr}\n{result.stdout}"

        # Verify child index entry
        index_data = forge_workspace.read_json("$HOME/.forge/sessions/index.json")
        child = _entry_by_name(index_data, "child-fork")
        assert child["forge_root"] == "/tmp/target-wt"
        assert child["relative_path"] == "."

        # Verify manifest exists at target forge_root
        assert forge_workspace.file_exists("/tmp/target-wt/.forge/sessions/child-fork/forge.session.json")

    def test_fork_into_missing_forge_fails(self, forge_workspace: ContainerLike) -> None:
        """Fork --into fails with clear error when target worktree has no .forge/."""
        forge_workspace.exec("cd /workspace && forge session start parent-nf --no-launch")

        # Create a real worktree without .forge/
        forge_workspace.exec("cd /workspace && git worktree add /tmp/bare-wt -b bare-target")

        result = forge_workspace.exec(
            "cd /workspace && forge session fork parent-nf --name child-nf --into /tmp/bare-wt --no-launch"
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "No Forge project" in combined, f"Expected 'No Forge project' in: {combined}"


class TestNestedRelativePath:
    """Nested Forge project: relative_path != '.' (monorepo)."""

    def test_start_session_nested_forge_project(self, forge_workspace: ContainerLike) -> None:
        """Session in a nested Forge project has correct relative_path."""
        # Create nested project structure with .claude/ and .forge/ at nested location.
        # enable --local finds .claude/ at /workspace (git root), so we manually
        # create the Forge project anchor at the nested path.
        forge_workspace.exec("mkdir -p /workspace/packages/app/.claude /workspace/packages/app/.forge")

        result = forge_workspace.exec("cd /workspace/packages/app && forge session start nested-test --no-launch")
        assert result.returncode == 0, f"Session start failed: {result.stderr}"

        index = forge_workspace.read_json("$HOME/.forge/sessions/index.json")
        entry = _entry_by_name(index, "nested-test")

        assert entry["relative_path"] == "packages/app", f"Expected 'packages/app', got '{entry['relative_path']}'"
        forge_root = str(entry["forge_root"])
        assert forge_root.endswith("packages/app"), f"forge_root should end with packages/app, got: {forge_root}"
