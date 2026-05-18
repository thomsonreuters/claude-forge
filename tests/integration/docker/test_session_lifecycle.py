"""Session lifecycle tests with Docker isolation.

These tests verify worktree ownership, index/pointer self-healing,
and intent vs confirmed state.

Note: Uses forge_workspace fixture for forge in PATH + clean state.
"""

from __future__ import annotations

import pytest

from tests.fixtures.docker import ContainerLike


def _python_find_entry_by_name(name: str) -> str:
    """Return inline Python that resolves a raw v1 index entry by display name."""
    return f"""
def find_entry(index, name):
    sessions = index.get('sessions', {{}})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {{name!r}}, found {{len(matches)}}'
    return matches[0]

entry = find_entry(index, '{name}')
"""


def _python_session_present(name: str) -> str:
    """Return inline Python that checks if any raw v1 index key matches a name."""
    return f"any(key.split('|', 1)[0] == '{name}' for key in index.get('sessions', {{}}))"


# Mark all tests as integration + docker_in
pytestmark = [pytest.mark.integration, pytest.mark.docker_in]


def _simulate_hook_uuid(workspace: ContainerLike, session_name: str, uuid: str = "test-uuid-001") -> None:
    """Simulate what SessionStart hook does: set confirmed.claude_session_id.

    UUID is hook-owned. Mock claude doesn't run real hooks,
    so tests that need a UUID must call this after session start.
    """
    workspace.exec(
        f'cd /forge && uv run python -c "'
        f"import json; from pathlib import Path; "
        f"p = list(Path('/workspace').rglob('.forge/sessions/{session_name}/forge.session.json')); "
        f"assert p, 'manifest not found'; "
        f"d = json.loads(p[0].read_text()); "
        f"d['confirmed']['claude_session_id'] = '{uuid}'; "
        f"p[0].write_text(json.dumps(d))"
        f'"'
    )


class TestWorktreeSemantics:
    """Tests for worktree ownership and --worktree flag."""

    def test_multiple_sessions_in_same_worktree(self, forge_workspace: ContainerLike) -> None:
        """Verify multiple sessions can coexist in same worktree (per-session dirs)."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")

        # Create first session
        result1 = forge_workspace.exec("cd /workspace && forge session start alpha")
        assert result1.returncode == 0, f"First session failed: {result1.stderr}"

        # Create second session in same worktree — should succeed (D5)
        result2 = forge_workspace.exec("cd /workspace && forge session start beta")
        assert result2.returncode == 0, f"Second session should succeed: {result2.stderr}"

        # Both manifests should exist
        check = forge_workspace.exec(
            "test -f /workspace/.forge/sessions/alpha/forge.session.json && test -f /workspace/.forge/sessions/beta/forge.session.json && echo ok"
        )
        assert "ok" in check.stdout, "Both session manifests should exist"

    def test_worktree_flag_creates_isolated_session(self, forge_workspace: ContainerLike) -> None:
        """Verify --worktree creates a new worktree with isolated session."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")

        # Create first session in main worktree
        result1 = forge_workspace.exec("cd /workspace && forge session start main-session")
        assert result1.returncode == 0, f"Main session failed: {result1.stderr}"

        # Create second session with --worktree flag
        result2 = forge_workspace.exec("cd /workspace && forge session start worktree-session --worktree")
        assert result2.returncode == 0, f"Worktree session failed: {result2.stderr}"

        # Verify both manifests exist in different locations
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
from pathlib import Path
import json

# Main worktree manifest (per-session path)
main_manifest = Path('/workspace/.forge/sessions/main-session/forge.session.json')
assert main_manifest.exists(), 'Main manifest missing'
main_data = json.loads(main_manifest.read_text())
assert main_data['name'] == 'main-session', f'Expected main-session, got {main_data[\"name\"]}'

# Worktree manifest (find the worktree path from index)
index_path = Path.home() / '.forge' / 'sessions' / 'index.json'
index = json.loads(index_path.read_text())
def find_entry(index, name):
    sessions = index.get('sessions', {})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {name!r}, found {len(matches)}'
    return matches[0]

wt_entry = find_entry(index, 'worktree-session')
wt_manifest = Path(wt_entry.get('forge_root', wt_entry['worktree_path'])) / '.forge' / 'sessions' / 'worktree-session' / 'forge.session.json'
assert wt_manifest.exists(), f'Worktree manifest missing at {wt_manifest}'
wt_data = json.loads(wt_manifest.read_text())
assert wt_data['name'] == 'worktree-session', f'Expected worktree-session, got {wt_data[\"name\"]}'

print('isolated')
"
        """)
        assert "isolated" in check.stdout, f"Isolation check failed: {check.stderr}"

    def test_worktree_session_in_index(self, forge_workspace: ContainerLike) -> None:
        """Verify both sessions tracked in global index."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start main-session")
        forge_workspace.exec("cd /workspace && forge session start worktree-session --worktree")

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
sessions = list(index.get('sessions', {}).keys())
names = [key.split('|', 1)[0] for key in sessions]
assert 'main-session' in names, f'main-session missing: {sessions}'
assert 'worktree-session' in names, f'worktree-session missing: {sessions}'
print('both_indexed')
"
        """)
        assert "both_indexed" in check.stdout, f"Index check failed: {check.stderr}"


class TestIndexSelfHealing:
    """Tests for session index self-healing on access."""

    def test_deleted_session_removed_from_index(self, forge_workspace: ContainerLike) -> None:
        """Verify deleted session is removed from global index."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start test-session")

        # Verify in index
        check1 = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert any(key.split('|', 1)[0] == 'test-session' for key in index.get('sessions', {})), 'not in index before delete'
print('in_index')
"
        """)
        assert "in_index" in check1.stdout, f"Pre-delete check failed: {check1.stderr}"

        # Delete session
        result = forge_workspace.exec("cd /workspace && forge session delete test-session --yes")
        assert result.returncode == 0, f"Delete failed: {result.stderr}"

        # Verify NOT in index
        check2 = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert not any(key.split('|', 1)[0] == 'test-session' for key in index.get('sessions', {})), 'still in index after delete'
print('removed_from_index')
"
        """)
        assert "removed_from_index" in check2.stdout, f"Post-delete check failed: {check2.stderr}"

    def test_orphaned_manifest_pruned_on_list(self, forge_workspace: ContainerLike) -> None:
        """Verify orphaned index entry pruned on access."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start test-session")

        # Manually delete manifest (simulating corruption/external deletion)
        forge_workspace.exec("rm /workspace/.forge/sessions/test-session/forge.session.json")

        # Run forge session list (triggers prune)
        result = forge_workspace.exec("cd /workspace && forge session list")

        # Command should succeed (not fail due to orphaned entry)
        assert result.returncode == 0, f"Session list failed: {result.stderr}"
        # Session should not appear in output (pruned on access)
        assert "test-session" not in result.stdout, f"Orphaned session should be pruned: {result.stdout}"


class TestIntentVsConfirmed:
    """Tests for intent vs confirmed state divergence."""

    def test_intent_structure_after_start(self, forge_workspace: ContainerLike) -> None:
        """Verify manifest has intent structure after session start.

        The CLI still pre-seeds a UUID at creation time so launch/no-launch flows
        have a stable session identifier before hook reconciliation.
        We verify:
        - intent.proxy is None (direct mode is default)
        - confirmed.claude_session_id is pre-seeded
        - confirmed.confirmed_by is None before hook confirmation
        """
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start test-session --no-launch")

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
manifest = json.loads(Path('/workspace/.forge/sessions/test-session/forge.session.json').read_text())

# UUID is pre-seeded at creation time, but hook confirmation has not happened yet
assert manifest['confirmed']['claude_session_id'] is not None, 'UUID should be pre-seeded'
assert len(manifest['confirmed']['claude_session_id']) == 36, 'UUID should be valid format'
assert manifest['confirmed']['confirmed_by'] is None, 'confirmed_by should be None before hook'

# Default is direct mode (no proxy intent)
assert manifest['intent']['proxy'] is None, 'intent.proxy should be None in direct mode'

print('manifest_structure_ok')
"
        """)
        assert "manifest_structure_ok" in check.stdout, f"Check failed: {check.stderr}"

    def test_session_set_rejects_confirmed_fields(self, forge_workspace: ContainerLike) -> None:
        """Verify session set rejects confirmed.* fields."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start test-session")

        # Try to set a confirmed field
        result = forge_workspace.exec(
            "cd /workspace && forge session set --session test-session confirmed.claude_session_id 'malicious' 2>&1"
        )
        assert result.returncode != 0, "Expected non-zero exit for confirmed.* field"
        assert (
            "confirmed" in result.stdout.lower() or "invalid" in result.stdout.lower()
        ), f"Expected error about confirmed fields: {result.stdout}"

    def test_override_stored_in_overrides_not_intent(self, forge_workspace: ContainerLike) -> None:
        """Verify session set stores override in overrides section, not intent."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start test-session")

        # Set an override (key is relative to intent, not prefixed with 'intent.')
        result = forge_workspace.exec(
            "cd /workspace && forge session set --session test-session policy.fail_mode closed"
        )
        assert result.returncode == 0, f"Session set failed: {result.stderr}"

        # Verify stored in overrides, not mutating intent
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path

# Find per-session manifest
sessions_dir = Path('/workspace/.forge/sessions')
manifests = list(sessions_dir.rglob('forge.session.json'))
assert manifests, f'No manifest found under {sessions_dir}'
manifest = json.loads(manifests[0].read_text())

# Override should be in overrides section
policy_override = manifest['overrides'].get('policy', {})
assert policy_override.get('fail_mode') == 'closed', \\
    f'Override not in overrides: {manifest[\"overrides\"]}'

# Intent section should NOT be mutated
policy_intent = manifest['intent'].get('policy') or {}
assert policy_intent.get('fail_mode') != 'closed', \\
    f'Intent was mutated: {manifest[\"intent\"]}'

print('override_stored_correctly')
"
        """)
        assert "override_stored_correctly" in check.stdout, f"Check failed: {check.stderr}"

    def test_corrupt_manifest_error_message_helpful(self, forge_workspace: ContainerLike) -> None:
        """Verify corrupt manifest produces helpful error message."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start test-session")

        # Corrupt manifest
        forge_workspace.exec("echo 'not json' > /workspace/.forge/sessions/test-session/forge.session.json")

        # Try to show details (corrupt manifest should produce helpful error)
        result = forge_workspace.exec("cd /workspace && forge session show test-session 2>&1")
        assert result.returncode != 0, "Expected non-zero exit for corrupt manifest"

        # Verify helpful error (mentions manifest and JSON issue)
        output_lower = result.stdout.lower()
        assert (
            "manifest" in output_lower or "json" in output_lower or "corrupt" in output_lower
        ), f"Expected helpful error about corrupt manifest: {result.stdout}"


class TestWorktreeWorkflow:
    """End-to-end tests for worktree lifecycle workflows.

    Covers fork, delete, config propagation, artifact routing, and dirty
    worktree protection — the cross-component gaps not covered by per-module
    CIT tests in tests/src/session/worktree/.
    """

    def test_fork_creates_worktree_and_tracks_parent(self, forge_workspace: ContainerLike) -> None:
        """Verify fork --worktree creates a new worktree with parent link and git branch."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start parent-sess --no-launch")
        _simulate_hook_uuid(forge_workspace, "parent-sess", "parent-uuid-wt")

        result = forge_workspace.exec("cd /workspace && forge session fork parent-sess --name fork-sess --worktree")
        assert result.returncode == 0, f"Fork failed: {result.stderr}"

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
def find_entry(index, name):
    sessions = index.get('sessions', {})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {name!r}, found {len(matches)}'
    return matches[0]

# Fork tracked in index
fork_entry = find_entry(index, 'fork-sess')

# Worktree path is a sibling to /workspace
wt_path = Path(fork_entry['worktree_path'])
assert wt_path.exists(), f'Worktree dir missing: {wt_path}'
assert wt_path != Path('/workspace'), f'Fork should have its own worktree, not /workspace'

# project_root points to main repo
assert fork_entry['project_root'] == '/workspace', f'project_root wrong: {fork_entry[\"project_root\"]}'

# Manifest exists in fork worktree
manifest_path = wt_path / '.forge' / 'sessions' / 'fork-sess' / 'forge.session.json'
assert manifest_path.exists(), f'Fork manifest missing: {manifest_path}'
manifest = json.loads(manifest_path.read_text())
assert manifest['is_fork'] is True, f'Expected is_fork=True: {manifest.get(\"is_fork\")}'
assert manifest['parent_session'] == 'parent-sess', f'Wrong parent: {manifest.get(\"parent_session\")}'

# Git branch exists
result = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/fork-sess'],
                       cwd='/workspace', capture_output=True)
assert result.returncode == 0, 'Git branch fork-sess not found'

print('fork_ok')
"
        """)
        assert "fork_ok" in check.stdout, f"Fork check failed: {check.stderr}"

    def test_fork_default_no_worktree(self, forge_workspace: ContainerLike) -> None:
        """Default fork stays in parent's directory with no git branch."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start parent-nw --no-launch")
        _simulate_hook_uuid(forge_workspace, "parent-nw", "parent-nw-uuid")

        result = forge_workspace.exec("cd /workspace && forge session fork parent-nw --name fork-nw")
        assert result.returncode == 0, f"Fork failed: {result.stderr}"

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
def find_entry(index, name):
    sessions = index.get('sessions', {})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {name!r}, found {len(matches)}'
    return matches[0]

fork_entry = find_entry(index, 'fork-nw')

# Fork should be in the same directory as parent
assert fork_entry['worktree_path'] == '/workspace', f'Expected /workspace, got {fork_entry[\"worktree_path\"]}'

# Manifest exists under parent's directory
manifest_path = Path('/workspace/.forge/sessions/fork-nw/forge.session.json')
assert manifest_path.exists(), f'Fork manifest missing: {manifest_path}'
manifest = json.loads(manifest_path.read_text())
assert manifest['is_fork'] is True
assert manifest['parent_session'] == 'parent-nw'
assert manifest['worktree']['is_worktree'] is False

# No git branch created
result = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/fork-nw'],
                       cwd='/workspace', capture_output=True)
assert result.returncode != 0, 'Git branch fork-nw should NOT exist for no-worktree fork'

print('fork_nw_ok')
"
        """)
        assert "fork_nw_ok" in check.stdout, f"No-worktree fork check failed: {check.stderr}"

    def test_force_same_dir_fork_rejects_unrelated_existing_session(self, forge_workspace: ContainerLike) -> None:
        """Force same-dir fork should fail when the target name already belongs to a different session."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start parent-nw --no-launch")
        _simulate_hook_uuid(forge_workspace, "parent-nw", "parent-nw-uuid")
        forge_workspace.exec("cd /workspace && forge session start fork-nw --no-launch")

        result = forge_workspace.exec(
            "cd /workspace && forge session fork parent-nw --name fork-nw --force --no-launch"
        )

        assert result.returncode != 0, "Force fork should not replace an unrelated existing session"
        output = (result.stdout + result.stderr).lower()
        assert "already exists" in output or "session 'fork-nw'" in output, output

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path

manifest = json.loads(Path('/workspace/.forge/sessions/fork-nw/forge.session.json').read_text())
assert manifest['is_fork'] is False, manifest
assert manifest['parent_session'] is None, manifest

print('same_dir_force_guard_ok')
"
        """)
        assert "same_dir_force_guard_ok" in check.stdout, f"Existing session check failed: {check.stderr}"

    def test_delete_worktree_session_cleans_up(self, forge_workspace: ContainerLike) -> None:
        """Verify delete removes worktree directory, branch, and index entry."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start wt-sess --worktree")

        # Capture worktree path before deletion
        wt_path_result = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
entry = next(entry for key, entry in index.get('sessions', {}).items() if key.split('|', 1)[0] == 'wt-sess')
print(entry['worktree_path'])
"
        """)
        wt_path = wt_path_result.stdout.strip()
        assert wt_path, f"Could not determine worktree path: {wt_path_result.stderr}"

        # Delete with --force --delete-branch
        result = forge_workspace.exec("cd /workspace && forge session delete wt-sess --yes --force --delete-branch")
        assert result.returncode == 0, f"Delete failed: {result.stderr}"

        # Verify cleanup (use %s formatting to avoid f-string escaping in inline Python)
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

# Worktree directory removed
assert not Path('%s').exists(), 'Worktree directory still exists'

# Session removed from index
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert not any(key.split('|', 1)[0] == 'wt-sess' for key in index.get('sessions', {})), 'Session still in index'

# Git branch removed
result = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/wt-sess'],
                       cwd='/workspace', capture_output=True)
assert result.returncode != 0, 'Git branch wt-sess should be deleted'

print('cleanup_ok')
"
        """ % wt_path)
        assert "cleanup_ok" in check.stdout, f"Cleanup check failed: {check.stderr}"

    def test_delete_keep_worktree_preserves_directory(self, forge_workspace: ContainerLike) -> None:
        """Verify --keep-worktree preserves the directory but removes the session."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start wt-keep --worktree")

        # Capture worktree path from index before deletion (avoids brittle glob assumptions)
        wt_path_result = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
entry = next(entry for key, entry in index.get('sessions', {}).items() if key.split('|', 1)[0] == 'wt-keep')
print(entry['worktree_path'])
"
        """)
        wt_path = wt_path_result.stdout.strip()
        assert wt_path, f"Could not determine worktree path: {wt_path_result.stderr}"

        # Delete with --keep-worktree
        result = forge_workspace.exec("cd /workspace && forge session delete wt-keep --yes --keep-worktree")
        assert result.returncode == 0, f"Delete failed: {result.stderr}"

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

# Session removed from index
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert not any(key.split('|', 1)[0] == 'wt-keep' for key in index.get('sessions', {})), 'Session still in index'

# Git branch still exists (--delete-branch was not passed)
result = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/wt-keep'],
                       cwd='/workspace', capture_output=True)
assert result.returncode == 0, 'Git branch should still exist'

# Worktree directory still exists (read path from index before deletion)
wt_path = Path('%s')
assert wt_path.exists(), f'Worktree directory should be preserved: {wt_path}'

# Manifest removed even though directory preserved
manifest = wt_path / '.forge' / 'sessions' / 'wt-keep' / 'forge.session.json'
assert not manifest.exists(), f'Manifest should be deleted inside preserved worktree: {manifest}'

print('keep_ok')
"
        """ % wt_path)
        assert "keep_ok" in check.stdout, f"Keep-worktree check failed: {check.stderr}"

    def test_worktree_config_copy(self, forge_workspace: ContainerLike) -> None:
        """Verify .env is copied from main repo to worktree on creation."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")

        # Create .env in main repo (untracked)
        forge_workspace.write_file("/workspace/.env", "SECRET_KEY=test123\nDB_URL=localhost\n")

        result = forge_workspace.exec("cd /workspace && forge session start config-sess --worktree")
        assert result.returncode == 0, f"Session start failed: {result.stderr}"

        # Verify .env copied to worktree
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
entry = next(entry for key, entry in index.get('sessions', {}).items() if key.split('|', 1)[0] == 'config-sess')
wt_path = Path(entry['worktree_path'])

env_file = wt_path / '.env'
assert env_file.exists(), f'.env missing in worktree: {wt_path}'
content = env_file.read_text()
assert 'SECRET_KEY=test123' in content, f'.env content mismatch: {content}'
assert 'DB_URL=localhost' in content, f'.env content mismatch: {content}'

print('config_ok')
"
        """)
        assert "config_ok" in check.stdout, f"Config copy check failed: {check.stderr}"

    def test_artifact_paths_route_to_main_repo(self, forge_workspace: ContainerLike) -> None:
        """Verify worktree session's project_root points to main repo for artifact storage."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")

        result = forge_workspace.exec("cd /workspace && forge session start art-sess --worktree")
        assert result.returncode == 0, f"Session start failed: {result.stderr}"

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
entry = next(entry for key, entry in index.get('sessions', {}).items() if key.split('|', 1)[0] == 'art-sess')

# project_root is the main repo (/workspace)
assert entry['project_root'] == '/workspace', f'project_root wrong: {entry[\"project_root\"]}'

# worktree_path is different (the sibling worktree)
assert entry['worktree_path'] != '/workspace', f'worktree_path should differ from project_root'
assert entry['worktree_path'] != entry['project_root'], 'worktree and project root should differ'

print('artifacts_ok')
"
        """)
        assert "artifacts_ok" in check.stdout, f"Artifact routing check failed: {check.stderr}"

    def test_dirty_worktree_blocks_delete(self, forge_workspace: ContainerLike) -> None:
        """Verify delete rejects dirty worktree without --force."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start dirty-sess --worktree")

        # Get worktree path
        wt_path_result = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
entry = next(entry for key, entry in index.get('sessions', {}).items() if key.split('|', 1)[0] == 'dirty-sess')
print(entry['worktree_path'])
"
        """)
        wt_path = wt_path_result.stdout.strip()

        # Create uncommitted file in worktree (use write_file to avoid shell interpolation)
        forge_workspace.write_file(f"{wt_path}/dirty-file.txt", "uncommitted change\n")

        # Delete without --force should fail (dirty worktree)
        result_no_force = forge_workspace.exec("cd /workspace && forge session delete dirty-sess 2>&1")
        assert result_no_force.returncode != 0, "Delete should fail on dirty worktree without --force"

        # Verify "no damage" — session and worktree intact after failed delete
        no_damage_check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
def find_entry(index, name):
    sessions = index.get('sessions', {})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {name!r}, found {len(matches)}'
    return matches[0]

assert any(key.split('|', 1)[0] == 'dirty-sess' for key in index.get('sessions', {})), 'Session removed from index despite failed delete'

entry = find_entry(index, 'dirty-sess')
wt_path = Path(entry['worktree_path'])
assert wt_path.exists(), 'Worktree directory removed despite failed delete'

forge_root = Path(entry.get('forge_root', str(wt_path)))
manifest = forge_root / '.forge' / 'sessions' / 'dirty-sess' / 'forge.session.json'
assert manifest.exists(), f'Manifest removed despite failed delete (checked {manifest})'

print('no_damage_ok')
"
        """)
        assert "no_damage_ok" in no_damage_check.stdout, f"No-damage check failed: {no_damage_check.stderr}"

        # Delete with --force should succeed
        result_force = forge_workspace.exec("cd /workspace && forge session delete dirty-sess --yes --force")
        assert result_force.returncode == 0, f"Force delete failed: {result_force.stderr}"

        # Session should be gone
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert not any(key.split('|', 1)[0] == 'dirty-sess' for key in index.get('sessions', {})), 'Session still in index after force delete'
print('dirty_ok')
"
        """)
        assert "dirty_ok" in check.stdout, f"Dirty worktree check failed: {check.stderr}"

    def test_resume_from_worktree_inherits_worktree(self, forge_workspace: ContainerLike) -> None:
        """Verify resume creates child in same worktree as parent, not a new one."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start wt-parent --worktree")

        # Resume --fresh creates a child session in the parent's worktree
        result = forge_workspace.exec(
            "cd /workspace && forge session resume wt-parent --fresh --child-name wt-child --strategy minimal"
        )
        assert result.returncode == 0, f"Resume failed: {result.stderr}"

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
def find_entry(index, name):
    sessions = index.get('sessions', {})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {name!r}, found {len(matches)}'
    return matches[0]

parent_entry = find_entry(index, 'wt-parent')
child_entry = find_entry(index, 'wt-child')

# Child lives in same worktree as parent (resume = context continuation, not divergence)
assert child_entry['worktree_path'] == parent_entry['worktree_path'], \
    f'Child worktree {child_entry[\"worktree_path\"]} != parent {parent_entry[\"worktree_path\"]}'

# Child manifest exists under parent's forge_root
forge_root = Path(parent_entry.get('forge_root', parent_entry['worktree_path']))
child_manifest_path = forge_root / '.forge' / 'sessions' / 'wt-child' / 'forge.session.json'
assert child_manifest_path.exists(), f'Child manifest missing: {child_manifest_path}'

manifest = json.loads(child_manifest_path.read_text())
assert manifest['parent_session'] == 'wt-parent', f'Wrong parent: {manifest.get(\"parent_session\")}'
assert manifest['is_fork'] is False, f'Resume should not be a fork: is_fork={manifest.get(\"is_fork\")}'

# Child should exist in the index after resume
index2 = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert any(key.split('|', 1)[0] == 'wt-child' for key in index2.get('sessions', {})), 'wt-child not in index after resume'

print('resume_ok')
"
        """)
        assert "resume_ok" in check.stdout, f"Resume check failed: {check.stderr}"

    def test_fork_with_branch_override(self, forge_workspace: ContainerLike) -> None:
        """Verify fork --branch implies a worktree and uses the custom branch name."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start branch-parent --no-launch")
        _simulate_hook_uuid(forge_workspace, "branch-parent", "branch-parent-uuid")

        result = forge_workspace.exec(
            "cd /workspace && forge session fork branch-parent --name branch-fork --branch custom-branch"
        )
        assert result.returncode == 0, f"Fork with --branch failed: {result.stderr}"

        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
def find_entry(index, name):
    sessions = index.get('sessions', {})
    matches = [entry for key, entry in sessions.items() if key.split('|', 1)[0] == name]
    assert len(matches) == 1, f'Expected exactly one entry for {name!r}, found {len(matches)}'
    return matches[0]

fork_entry = find_entry(index, 'branch-fork')

# Read manifest to verify branch name
wt_path = Path(fork_entry['worktree_path'])
manifest_path = Path(fork_entry.get('forge_root', fork_entry['worktree_path'])) / '.forge' / 'sessions' / 'branch-fork' / 'forge.session.json'
manifest = json.loads(manifest_path.read_text())

assert wt_path != Path('/workspace'), 'fork --branch should imply a separate worktree'
assert fork_entry['worktree_path'] != '/workspace', 'fork --branch should not stay in the parent checkout'

# Worktree branch should be the custom name, not session-name-derived
assert manifest['worktree']['branch'] == 'custom-branch', \
    f'Expected custom-branch, got {manifest[\"worktree\"][\"branch\"]}'

# Git branch 'custom-branch' should exist, 'branch-fork' should NOT
result_custom = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/custom-branch'],
                               cwd='/workspace', capture_output=True)
assert result_custom.returncode == 0, 'Git branch custom-branch not found'

result_default = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/branch-fork'],
                                cwd='/workspace', capture_output=True)
assert result_default.returncode != 0, 'Git branch branch-fork should NOT exist when --branch overrides'

print('branch_ok')
"
        """)
        assert "branch_ok" in check.stdout, f"Branch override check failed: {check.stderr}"

    def test_fork_from_incognito_rejected(self, forge_workspace: ContainerLike) -> None:
        """Verify forking from an incognito session is rejected."""
        forge_workspace.exec("forge extension enable --scope user --profile minimal")

        # Create session with --no-launch, then patch is_incognito in the manifest.
        # Can't use --incognito directly: mock Claude exits instantly, triggering
        # auto-delete before the fork command runs.
        result_start = forge_workspace.exec("cd /workspace && forge session start incog-parent --no-launch")
        assert result_start.returncode == 0, f"Session start failed: {result_start.stderr}"

        # Patch manifest to mark as incognito
        forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
p = Path('/workspace/.forge/sessions/incog-parent/forge.session.json')
d = json.loads(p.read_text())
d['is_incognito'] = True
p.write_text(json.dumps(d))
"
        """)

        # Fork should be rejected
        result_fork = forge_workspace.exec("cd /workspace && forge session fork incog-parent --name incog-fork 2>&1")
        assert result_fork.returncode != 0, "Fork from incognito should fail"

        output_lower = result_fork.stdout.lower()
        assert "incognito" in output_lower, f"Error should mention incognito: {result_fork.stdout}"

        # No fork artifacts should have been created (fail-fast before side effects)
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert not any(key.split('|', 1)[0] == 'incog-fork' for key in index.get('sessions', {})), 'Fork session created despite rejection'

# No git branch for the fork
result = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/incog-fork'],
                       cwd='/workspace', capture_output=True)
assert result.returncode != 0, 'Git branch incog-fork should not exist'

print('incognito_ok')
"
        """)
        assert "incognito_ok" in check.stdout, f"Incognito guard check failed: {check.stderr}"

    def test_delete_from_inside_worktree(self, forge_workspace: ContainerLike) -> None:
        """Verify delete works when cwd is inside the worktree being deleted.

        cleanup.py runs 'git worktree remove' from main repo root (not from
        the worktree), so this should succeed even when the user's shell is
        inside the worktree directory.
        """
        forge_workspace.exec("forge extension enable --scope user --profile minimal")
        forge_workspace.exec("cd /workspace && forge session start inside-sess --worktree")

        # Get worktree path
        wt_path_result = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
entry = next(entry for key, entry in index.get('sessions', {}).items() if key.split('|', 1)[0] == 'inside-sess')
print(entry['worktree_path'])
"
        """)
        wt_path = wt_path_result.stdout.strip()

        # Delete while cwd is inside the worktree (quote path for shell safety)
        result = forge_workspace.exec(
            'cd "%s" && forge session delete inside-sess --yes --force --delete-branch' % wt_path
        )
        assert result.returncode == 0, f"Delete from inside worktree failed: {result.stderr}"

        # Verify cleanup (use %s formatting for wt_path injection into inline Python)
        check = forge_workspace.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
import subprocess

# Worktree directory removed
assert not Path('%s').exists(), 'Worktree directory should be removed after delete'

# Session removed from index
index = json.loads((Path.home() / '.forge' / 'sessions' / 'index.json').read_text())
assert not any(key.split('|', 1)[0] == 'inside-sess' for key in index.get('sessions', {})), 'Session still in index'

# Git branch removed
result = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/inside-sess'],
                       cwd='/workspace', capture_output=True)
assert result.returncode != 0, 'Git branch should be deleted'

print('inside_ok')
"
        """ % wt_path)
        assert "inside_ok" in check.stdout, f"Inside-worktree delete check failed: {check.stderr}"
