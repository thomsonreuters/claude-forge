"""Tests for SessionManager.

Component Integration Tests (CIT) that test SessionManager with real filesystem
operations and git repo fixtures. All tests run inside Docker containers for
isolation from host filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.src.session.conftest import WorktreeWorkspace

# Mark all tests as Docker integration tests
pytestmark = [pytest.mark.integration, pytest.mark.docker_in]

# HOME path used in container for isolated session state
HOME = "/home/test"


class TestSessionManagerQuery:
    """Tests for SessionManager query operations."""

    def test_list_sessions_empty(self, manager_workspace: "WorktreeWorkspace") -> None:
        """Should return empty list when no sessions."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
sessions = manager.list_sessions()
print(json.dumps({'sessions': sessions}))
""",
            home=HOME,
        )
        assert result.ok, result.stdout
        assert result.data is not None
        assert result.data["sessions"] == []

    def test_session_exists_false_when_missing(self, manager_workspace: "WorktreeWorkspace") -> None:
        """Should return False for non-existent session."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
exists = manager.session_exists('nonexistent')
print(json.dumps({'exists': exists}))
""",
            home=HOME,
        )
        assert result.ok, result.stdout
        assert result.data is not None
        assert result.data["exists"] is False


class TestSessionManagerStartSession:
    """Tests for SessionManager.start_session()."""

    def test_start_create_worktree_git_failure_rolls_back(self, manager_workspace: "WorktreeWorkspace") -> None:
        """If git worktree add fails, Forge state should not be written."""
        # Create a branch to cause BranchExistsError
        manager_workspace.git("branch", "boom")

        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())

error = None
try:
    manager.start_session(name='boom', create_worktree=True)
except Exception as e:
    error = type(e).__name__

# No index entry should exist
exists = manager.session_exists('boom')

print(json.dumps({
    'error': error,
    'session_exists': exists,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] is not None  # Some exception was raised
        assert result.data["session_exists"] is False

    def test_start_create_worktree_rollback_on_index_collision(self, manager_workspace: "WorktreeWorkspace") -> None:
        """If index add fails after worktree creation, worktree should be removed."""
        result = manager_workspace.run_python(
            """
import json
from pathlib import Path
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.worktree import resolve_worktree_path
from forge.session.worktree.create import get_repo_root

index_store = IndexStore()
manager = SessionManager(index_store=index_store)

# Pre-create an index entry so add_from_state fails with SessionExistsError
index_store.add_session(
    name='collision',
    worktree_path='/workspace',
    project_root='/workspace',
    is_fork=False,
    is_incognito=False,
    parent_session=None,
)

repo_root = get_repo_root(Path('/workspace'))
expected_worktree = resolve_worktree_path(repo_root, 'collision')

error = None
try:
    manager.start_session(name='collision', create_worktree=True)
except Exception as e:
    error = type(e).__name__

# Worktree should have been cleaned up
worktree_exists = expected_worktree.exists()

print(json.dumps({
    'error': error,
    'worktree_exists': worktree_exists,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "SessionExistsError"
        assert result.data["worktree_exists"] is False

    def test_start_creates_manifest(self, manager_workspace: "WorktreeWorkspace") -> None:
        """start_session should create manifest file."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.store import SessionStore

manager = SessionManager(index_store=IndexStore())
manifest = manager.start_session(
    name='test-session',
    worktree_path='/workspace',
)

store = SessionStore('/workspace', 'test-session')
print(json.dumps({
    'name': manifest.name,
    'store_exists': store.exists()
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["name"] == "test-session"
        assert result.data["store_exists"] is True

    def test_start_adds_to_index(self, manager_workspace: "WorktreeWorkspace") -> None:
        """start_session should add session to index."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='indexed-session',
    worktree_path='/workspace',
)

exists = manager.session_exists('indexed-session')
print(json.dumps({'exists': exists}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["exists"] is True

    def test_start_leaves_uuid_none(self, manager_workspace: "WorktreeWorkspace") -> None:
        """start_session should leave claude_session_id as None (launch-owned)."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manifest = manager.start_session(
    name='uuid-session',
    worktree_path='/workspace',
)

print(json.dumps({
    'session_id': manifest.confirmed.claude_session_id,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["session_id"] is None

    def test_start_raises_when_exists(self, manager_workspace: "WorktreeWorkspace") -> None:
        """start_session should raise SessionExistsError for duplicate names."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session import SessionExistsError

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='duplicate-session',
    worktree_path='/workspace',
)

error = None
try:
    manager.start_session(
        name='duplicate-session',
        worktree_path='/workspace',
    )
except SessionExistsError:
    error = 'SessionExistsError'

print(json.dumps({'error': error}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "SessionExistsError"


class TestSessionManagerResumeSession:
    """Tests for SessionManager.resume_session()."""

    def test_resume_creates_child_session(self, manager_workspace: "WorktreeWorkspace") -> None:
        """resume_session should create a new child session derived from parent."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())

# Create parent session
manager.start_session(
    name='parent-session',
    worktree_path='/workspace',
)

# Resume creates a child session
child_manifest, handoff_result = manager.resume_session('parent-session')

print(json.dumps({
    'child_name': child_manifest.name,
    'parent_session': child_manifest.parent_session,
    'has_handoff': handoff_result is not None,
    'context_file_rel': handoff_result.context_file_rel if handoff_result else None,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["child_name"] == "parent-session-resumed"
        assert result.data["parent_session"] == "parent-session"
        assert result.data["has_handoff"] is True
        assert (
            result.data["context_file_rel"] == ".forge/prev_sessions/parent-session/children/parent-session-resumed.md"
        )

    def test_resume_with_explicit_child_name(self, manager_workspace: "WorktreeWorkspace") -> None:
        """resume_session should accept explicit child name."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())

# Create parent session
manager.start_session(
    name='parent-v1',
    worktree_path='/workspace',
)

# Resume with explicit child name
child_manifest, _ = manager.resume_session('parent-v1', child_name='parent-v2')

print(json.dumps({
    'child_name': child_manifest.name,
    'parent_session': child_manifest.parent_session,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["child_name"] == "parent-v2"
        assert result.data["parent_session"] == "parent-v1"

    def test_resume_raises_when_not_found(self, manager_workspace: "WorktreeWorkspace") -> None:
        """resume_session should raise SessionNotFoundError."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session import SessionNotFoundError

manager = SessionManager(index_store=IndexStore())

error = None
try:
    manager.resume_session('nonexistent')
except SessionNotFoundError:
    error = 'SessionNotFoundError'

print(json.dumps({'error': error}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "SessionNotFoundError"


class TestSessionManagerSwitchSession:
    """Tests for SessionManager.switch_session()."""

    def test_switch_updates_timestamps(self, manager_workspace: "WorktreeWorkspace") -> None:
        """switch_session should update last_accessed_at."""
        result = manager_workspace.run_python(
            """
import json
import time
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='switch-time-test',
    worktree_path='/workspace',
)

time.sleep(0.01)

manifest = manager.switch_session('switch-time-test')
has_timestamp = manifest.last_accessed_at is not None
print(json.dumps({'has_timestamp': has_timestamp}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["has_timestamp"] is True


class TestSessionManagerForkSession:
    """Tests for SessionManager.fork_session()."""

    def test_fork_default_no_worktree(self, manager_workspace: "WorktreeWorkspace") -> None:
        """Default fork stays in parent's directory (no git worktree)."""
        result = manager_workspace.run_python(
            """
import json
from pathlib import Path
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.worktree import branch_exists

manager = SessionManager(
    index_store=IndexStore(),
)

manager.start_session(
    name='parent-session',
    worktree_path='/workspace',
)

parent, fork = manager.fork_session('parent-session', 'fork-session')
derivation = fork.confirmed.derivation

manifest_path = Path('/workspace/.forge/sessions/fork-session/forge.session.json')

print(json.dumps({
    'parent_name': parent.name,
    'fork_name': fork.name,
    'is_fork': fork.is_fork,
    'parent_session': fork.parent_session,
    'has_worktree': fork.worktree is not None,
    'is_worktree': fork.worktree.is_worktree if fork.worktree else False,
    'fork_path': fork.worktree.path if fork.worktree else None,
    'manifest_exists': manifest_path.exists(),
    'branch_exists': branch_exists('fork-session', Path('/workspace')),
    'derivation': None if derivation is None else {
        'parent_session': derivation.parent_session,
        'resume_mode': derivation.resume_mode,
        'strategy': derivation.strategy,
        'depth': derivation.depth,
        'lineage': derivation.lineage,
        'parent_forge_root': derivation.parent_forge_root,
        'parent_project_root': derivation.parent_project_root,
    },
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["parent_name"] == "parent-session"
        assert result.data["fork_name"] == "fork-session"
        assert result.data["is_fork"] is True
        assert result.data["parent_session"] == "parent-session"
        assert result.data["has_worktree"] is True
        assert result.data["is_worktree"] is False
        assert result.data["fork_path"] == "/workspace"
        assert result.data["manifest_exists"] is True
        assert result.data["branch_exists"] is False
        assert result.data["derivation"] == {
            "parent_session": "parent-session",
            "resume_mode": "native",
            "strategy": None,
            "depth": 1,
            "lineage": ["parent-session"],
            "parent_forge_root": "/workspace",
            "parent_project_root": "/workspace",
        }

    def test_fork_with_worktree_creates_worktree(self, manager_workspace: "WorktreeWorkspace") -> None:
        """fork_session(create_worktree=True) creates a git worktree."""
        result = manager_workspace.run_python(
            """
import json
from pathlib import Path
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.worktree import branch_exists

manager = SessionManager(
    index_store=IndexStore(),
)

manager.start_session(
    name='parent-session',
    worktree_path='/workspace',
)

parent, fork = manager.fork_session('parent-session', 'fork-session', create_worktree=True)
derivation = fork.confirmed.derivation

print(json.dumps({
    'parent_name': parent.name,
    'fork_name': fork.name,
    'is_fork': fork.is_fork,
    'parent_session': fork.parent_session,
    'has_worktree': fork.worktree is not None,
    'is_worktree': fork.worktree.is_worktree if fork.worktree else False,
    'worktree_exists': Path(fork.worktree.path).exists() if fork.worktree else False,
    'branch_exists': branch_exists('fork-session', Path('/workspace')),
    'derivation': None if derivation is None else {
        'parent_session': derivation.parent_session,
        'resume_mode': derivation.resume_mode,
        'strategy': derivation.strategy,
        'depth': derivation.depth,
        'lineage': derivation.lineage,
        'parent_forge_root': derivation.parent_forge_root,
        'parent_project_root': derivation.parent_project_root,
    },
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["parent_name"] == "parent-session"
        assert result.data["fork_name"] == "fork-session"
        assert result.data["is_fork"] is True
        assert result.data["parent_session"] == "parent-session"
        assert result.data["has_worktree"] is True
        assert result.data["is_worktree"] is True
        assert result.data["worktree_exists"] is True
        assert result.data["branch_exists"] is True
        assert result.data["derivation"]["parent_session"] == "parent-session"
        assert result.data["derivation"]["resume_mode"] == "handoff"
        assert result.data["derivation"]["strategy"] is None
        assert result.data["derivation"]["depth"] == 1
        assert result.data["derivation"]["lineage"] == ["parent-session"]
        assert result.data["derivation"]["parent_forge_root"] == "/workspace"
        assert result.data["derivation"]["parent_project_root"] == "/workspace"

    def test_force_worktree_fork_keeps_same_name_session_in_parent_root(
        self, manager_workspace: "WorktreeWorkspace"
    ) -> None:
        """Force worktree fork should not delete a same-name session in the parent forge_root."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(name='parent-session', worktree_path='/workspace')
manager.start_session(name='fork-session', worktree_path='/workspace')

_, fork = manager.fork_session('parent-session', 'fork-session', create_worktree=True, force=True)

parent_entry = manager.index_store.get_session('fork-session', forge_root='/workspace')
child_entry = manager.index_store.get_session('fork-session', forge_root=fork.forge_root)

print(json.dumps({
    'parent_root': parent_entry.forge_root,
    'child_root': child_entry.forge_root,
    'roots_differ': parent_entry.forge_root != child_entry.forge_root,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["parent_root"] == "/workspace"
        assert result.data["roots_differ"] is True

    def test_force_worktree_fork_replaces_stale_child_without_deleting_new_worktree(
        self, manager_workspace: "WorktreeWorkspace"
    ) -> None:
        """Force retry should replace stale child state without removing the new worktree."""
        result = manager_workspace.run_python(
            """
import json
from pathlib import Path
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(name='parent-session', worktree_path='/workspace')
_, first = manager.fork_session('parent-session', 'fork-session', create_worktree=True)
_, second = manager.fork_session('parent-session', 'fork-session', create_worktree=True, force=True)

entry = manager.index_store.get_session('fork-session', forge_root=second.forge_root)

print(json.dumps({
    'first_path': first.worktree.path,
    'second_path': second.worktree.path,
    'worktree_exists': Path(second.worktree.path).exists(),
    'entry_path': entry.worktree_path,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["first_path"] == result.data["second_path"]
        assert result.data["worktree_exists"] is True
        assert result.data["entry_path"] == result.data["second_path"]

    def test_force_worktree_fork_rolls_back_created_worktree_on_index_failure(
        self, manager_workspace: "WorktreeWorkspace"
    ) -> None:
        """If fork commit fails after worktree creation, the new worktree/branch should be removed."""
        result = manager_workspace.run_python(
            """
import json
from pathlib import Path
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.worktree import branch_exists, resolve_worktree_path
from forge.session.worktree.create import get_repo_root

index_store = IndexStore()
manager = SessionManager(index_store=index_store)
manager.start_session(name='parent-session', worktree_path='/workspace')

repo_root = get_repo_root(Path('/workspace'))
expected_worktree = resolve_worktree_path(repo_root, 'fork-session')

original_add = index_store.add_from_state

def fail_add(state, *args, **kwargs):
    if state.name == 'fork-session':
        raise RuntimeError('boom')
    return original_add(state, *args, **kwargs)

index_store.add_from_state = fail_add

error = None
try:
    manager.fork_session('parent-session', 'fork-session', create_worktree=True)
except Exception as e:
    error = type(e).__name__

session_exists = manager.session_exists('fork-session', forge_root=str(expected_worktree))

print(json.dumps({
    'error': error,
    'worktree_exists': expected_worktree.exists(),
    'branch_exists': branch_exists('fork-session', Path('/workspace')),
    'session_exists': session_exists,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "RuntimeError"
        assert result.data["worktree_exists"] is False
        assert result.data["branch_exists"] is False
        assert result.data["session_exists"] is False

    def test_force_same_dir_fork_replaces_matching_stale_child(self, manager_workspace: "WorktreeWorkspace") -> None:
        """Force same-dir fork should retry only the stale child it previously created."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(name='parent-session', worktree_path='/workspace')
_, first = manager.fork_session('parent-session', 'fork-session')
_, second = manager.fork_session('parent-session', 'fork-session', force=True)

entry = manager.index_store.get_session('fork-session', forge_root='/workspace')
current = manager.get_session('fork-session', forge_root='/workspace')

print(json.dumps({
    'first_parent': first.parent_session,
    'second_parent': second.parent_session,
    'is_fork': current.is_fork,
    'entry_parent': entry.parent_session,
    'worktree_path': second.worktree.path if second.worktree else None,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["first_parent"] == "parent-session"
        assert result.data["second_parent"] == "parent-session"
        assert result.data["is_fork"] is True
        assert result.data["entry_parent"] == "parent-session"
        assert result.data["worktree_path"] == "/workspace"

    def test_force_same_dir_fork_rejects_unrelated_existing_session(
        self, manager_workspace: "WorktreeWorkspace"
    ) -> None:
        """Force same-dir fork must not delete a non-fork session that shares the target name."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session import SessionExistsError

manager = SessionManager(index_store=IndexStore())
manager.start_session(name='parent-session', worktree_path='/workspace')
manager.start_session(name='fork-session', worktree_path='/workspace')

error = None
try:
    manager.fork_session('parent-session', 'fork-session', force=True)
except SessionExistsError:
    error = 'SessionExistsError'

existing = manager.get_session('fork-session', forge_root='/workspace')
entry = manager.index_store.get_session('fork-session', forge_root='/workspace')

print(json.dumps({
    'error': error,
    'is_fork': existing.is_fork,
    'parent_session': existing.parent_session,
    'entry_parent': entry.parent_session,
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "SessionExistsError"
        assert result.data["is_fork"] is False
        assert result.data["parent_session"] is None
        assert result.data["entry_parent"] is None

    def test_fork_auto_generates_name(self, manager_workspace: "WorktreeWorkspace") -> None:
        """fork_session should auto-generate fork name if not provided."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(
    index_store=IndexStore(),
)

manager.start_session(
    name='parent-session',
    worktree_path='/workspace',
)

_, fork = manager.fork_session('parent-session')

print(json.dumps({
    'fork_name': fork.name,
    'has_hyphen': '-' in fork.name if fork.name else False
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["fork_name"] is not None
        assert result.data["has_hyphen"] is True  # adjective-noun format

    def test_fork_raises_for_missing_parent(self, manager_workspace: "WorktreeWorkspace") -> None:
        """fork_session should raise SessionNotFoundError for missing parent."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session import SessionNotFoundError

manager = SessionManager(
    index_store=IndexStore(),
)

error = None
try:
    manager.fork_session('nonexistent-parent', 'child')
except SessionNotFoundError:
    error = 'SessionNotFoundError'

print(json.dumps({'error': error}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "SessionNotFoundError"

    def test_fork_raises_for_incognito_parent(self, manager_workspace: "WorktreeWorkspace") -> None:
        """fork_session should raise CannotForkIncognitoError for incognito parent."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.exceptions import CannotForkIncognitoError

manager = SessionManager(
    index_store=IndexStore(),
)

# Create incognito parent session
manager.start_session(
    name='incognito-parent',
    worktree_path='/workspace',
    is_incognito=True,
)

error = None
try:
    manager.fork_session('incognito-parent', 'child')
except CannotForkIncognitoError:
    error = 'CannotForkIncognitoError'

print(json.dumps({'error': error}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "CannotForkIncognitoError"


class TestSessionManagerRelaunchSession:
    """Tests for SessionManager.relaunch_session()."""

    def test_relaunch_inherits_overrides(self, manager_workspace: "WorktreeWorkspace") -> None:
        """relaunch_session should preserve parent overrides for the child."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.store import SessionStore

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='parent-session',
    proxy_template='litellm-openai',
    proxy_base_url='http://localhost:8085',
    worktree_path='/workspace',
)

store = SessionStore('/workspace', 'parent-session')

def _set_overrides(manifest):
    manifest.overrides = {
        'verification': {'bypass': True},
        'proxy': {'base_url': 'http://localhost:9090'},
    }

store.update(timeout_s=5.0, mutate=_set_overrides)

_, child = manager.relaunch_session('parent-session', child_name='child-session')

print(json.dumps({'overrides': child.overrides}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["overrides"] == {
            "verification": {"bypass": True},
            "proxy": {"base_url": "http://localhost:9090"},
        }


class TestSessionManagerDeleteSession:
    """Tests for SessionManager.delete_session()."""

    def test_delete_removes_from_index(self, manager_workspace: "WorktreeWorkspace") -> None:
        """delete_session should remove session from index."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='delete-test',
    worktree_path='/workspace',
)

manager.delete_session('delete-test', force=True)
exists = manager.session_exists('delete-test')
print(json.dumps({'exists': exists}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["exists"] is False

    def test_delete_removes_manifest(self, manager_workspace: "WorktreeWorkspace") -> None:
        """delete_session should remove manifest file."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.store import SessionStore

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='manifest-delete-test',
    worktree_path='/workspace',
)

store = SessionStore('/workspace', 'manifest-delete-test')
exists_before = store.exists()

manager.delete_session('manifest-delete-test', force=True)

exists_after = store.exists()
print(json.dumps({
    'exists_before': exists_before,
    'exists_after': exists_after
}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["exists_before"] is True
        assert result.data["exists_after"] is False

    def test_delete_raises_when_not_found(self, manager_workspace: "WorktreeWorkspace") -> None:
        """delete_session should raise SessionNotFoundError."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session import SessionNotFoundError

manager = SessionManager(index_store=IndexStore())

error = None
try:
    manager.delete_session('nonexistent')
except SessionNotFoundError:
    error = 'SessionNotFoundError'

print(json.dumps({'error': error}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["error"] == "SessionNotFoundError"

    def test_delete_can_skip_transcripts(self, manager_workspace: "WorktreeWorkspace") -> None:
        """delete_session with delete_transcripts=False should skip cleanup."""
        result = manager_workspace.run_python(
            """
import json
from forge.session.index import IndexStore
from forge.session.manager import SessionManager

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='no-cleanup-test',
    worktree_path='/workspace',
)

# This should complete without error even though transcripts may not exist
manager.delete_session('no-cleanup-test', delete_transcripts=False, force=True)

exists = manager.session_exists('no-cleanup-test')
print(json.dumps({'exists': exists}))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["exists"] is False

    def test_delete_passes_rollover_session_ids_to_cleanup(self, manager_workspace: "WorktreeWorkspace") -> None:
        """delete_session should include rollover transcript UUIDs in cleanup."""
        result = manager_workspace.run_python(
            """
import json
import forge.session.claude.cleanup as cleanup_mod
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.store import SessionStore

captured = {}

def fake_cleanup_session(project_root, claude_session_id, artifact_session_ids=None):
    captured['project_root'] = project_root
    captured['claude_session_id'] = claude_session_id
    captured['artifact_session_ids'] = artifact_session_ids
    return cleanup_mod.CleanupResult()

cleanup_mod.cleanup_session = fake_cleanup_session

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='cleanup-test',
    worktree_path='/workspace',
)

store = SessionStore('/workspace', 'cleanup-test')

def _set_manifest_state(manifest):
    manifest.confirmed.claude_session_id = 'current-id'
    manifest.confirmed.artifacts = {
        'transcripts': [
            {'session_id': 'rollover-id', 'copied_path': '.forge/artifacts/cleanup-test/transcripts/rollover-id.jsonl'},
            {'session_id': 'current-id', 'copied_path': '.forge/artifacts/cleanup-test/transcripts/current-id.jsonl'},
            {'session_id': 'rollover-id', 'copied_path': '.forge/artifacts/cleanup-test/transcripts/rollover-id.jsonl'},
        ]
    }

store.update(timeout_s=5.0, mutate=_set_manifest_state)

manager.delete_session('cleanup-test', force=True)

print(json.dumps(captured))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["claude_session_id"] == "current-id"
        assert result.data["artifact_session_ids"] == ["rollover-id", "current-id"]

    def test_delete_checks_dirty_worktree_before_transcript_cleanup(
        self, manager_workspace: "WorktreeWorkspace"
    ) -> None:
        """Dirty-worktree preflight should happen before transcript cleanup."""
        result = manager_workspace.run_python(
            """
import json
import forge.session.claude.cleanup as cleanup_mod
import forge.session.worktree as worktree_mod
from forge.session.exceptions import DirtyWorktreeError
from forge.session.index import IndexStore
from forge.session.manager import SessionManager
from forge.session.store import SessionStore

captured = {"cleanup_called": False, "dirty_checked": False, "error": None}

def fake_cleanup_session(project_root, claude_session_id, artifact_session_ids=None):
    captured["cleanup_called"] = True
    return cleanup_mod.CleanupResult()

def fake_is_worktree_dirty(worktree_path):
    captured["dirty_checked"] = True
    return True

cleanup_mod.cleanup_session = fake_cleanup_session
worktree_mod.is_worktree_dirty = fake_is_worktree_dirty

manager = SessionManager(index_store=IndexStore())
manager.start_session(
    name='dirty-delete-test',
    worktree_path='/workspace',
)

store = SessionStore('/workspace', 'dirty-delete-test')

def _mark_worktree(manifest):
    assert manifest.worktree is not None
    manifest.worktree.is_worktree = True
    manifest.confirmed.claude_session_id = 'uuid-dirty'

store.update(timeout_s=5.0, mutate=_mark_worktree)

try:
    manager.delete_session('dirty-delete-test', force=False)
except DirtyWorktreeError as e:
    captured["error"] = e.path

print(json.dumps(captured))
""",
            home=HOME,
        )
        assert result.ok, f"Failed: {result.stderr}"
        assert result.data is not None
        assert result.data["dirty_checked"] is True
        assert result.data["cleanup_called"] is False
        assert result.data["error"] == "/workspace"
