"""End-to-end tests for installer against real ~/.claude/ paths.

These tests run in Docker containers to validate installer operations
against real filesystem paths without risk to host machine.
"""

from __future__ import annotations

import pytest

from tests.fixtures.docker import ContainerLike

# Mark all tests as integration + docker_in
pytestmark = [pytest.mark.integration, pytest.mark.docker_in]


def _get_tracking_path(container: ContainerLike) -> str:
    """Return the tracking manifest path resolved by Forge inside the test environment."""
    result = container.exec("""
        cd /forge && uv run python -c "
from forge.install.tracking import get_tracking_path
print(get_tracking_path())
"
    """)
    assert result.returncode == 0, f"Tracking path probe failed: {result.stderr}"
    return result.stdout.strip()


class TestForgeInit:
    """Tests for forge init command."""

    def test_init_user_scope_creates_claude_dir(self, synced_container: ContainerLike) -> None:
        """Verify forge extensions enable --scope user creates ~/.claude/."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        result = synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile minimal")
        assert result.returncode == 0, f"Init failed: {result.stderr}"

        check = synced_container.exec("test -d ~/.claude && echo 'exists'")
        assert "exists" in check.stdout, "~/.claude/ directory not created"

    def test_init_user_scope_creates_tracking_file(self, synced_container: ContainerLike) -> None:
        """Verify forge extensions enable creates the tracking manifest."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        result = synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile minimal")
        assert result.returncode == 0

        tracking_path = _get_tracking_path(synced_container)
        check = synced_container.exec(f"test -f {tracking_path} && echo 'found'")
        assert "found" in check.stdout

    def test_init_standard_profile_adds_hooks(self, synced_container: ContainerLike) -> None:
        """Verify forge extensions enable --profile standard adds hooks to settings.json."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        result = synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile standard")
        assert result.returncode == 0

        # Parse settings.json and verify hooks key exists
        check = synced_container.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
settings = json.loads(Path.home().joinpath('.claude/settings.json').read_text())
assert 'hooks' in settings, 'hooks key missing'
print('hooks present')
"
        """)
        assert check.returncode == 0, f"Settings check failed: {check.stderr}"
        assert "hooks present" in check.stdout

    def test_init_is_idempotent(self, synced_container: ContainerLike) -> None:
        """Verify running extensions enable twice doesn't error."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        # First init
        result1 = synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile minimal")
        assert result1.returncode == 0

        # Second init (should succeed)
        result2 = synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile minimal")
        assert result2.returncode == 0

    def test_init_auto_detect_creates_project_anchor_under_home(self, synced_container: ContainerLike) -> None:
        """Auto-detect should create repo-local .claude/ instead of falling back to user scope."""
        synced_container.exec("rm -rf ~/.claude ~/.forge ~/repo-auto-detect")

        result = synced_container.exec("""
            mkdir -p ~/repo-auto-detect && cd ~/repo-auto-detect
            git init -b main
            git config user.email "test@forge.local"
            git config user.name "Forge Test"
            echo "# Auto Detect" > README.md
            git add . && git commit -m "init"
            /forge/.venv/bin/forge extensions enable --profile minimal
        """)
        assert result.returncode == 0, f"Auto-detect enable failed: {result.stderr}"

        repo_check = synced_container.exec("test -d ~/repo-auto-detect/.claude && echo repo-scope")
        assert "repo-scope" in repo_check.stdout, f"Repo-local .claude/ missing: {repo_check.stderr}"

        home_check = synced_container.exec("test ! -d ~/.claude/settings.json && echo no-user-fallback")
        assert "no-user-fallback" in home_check.stdout, f"Unexpected user-scope install: {home_check.stderr}"

    def test_enable_creates_forge_anchor(self, synced_container: ContainerLike) -> None:
        """forge extensions enable --scope local creates both .claude/ and .forge/ (Rule 1)."""
        synced_container.exec("rm -rf ~/.claude ~/.forge ~/repo-forge-anchor")

        result = synced_container.exec("""
            mkdir -p ~/repo-forge-anchor && cd ~/repo-forge-anchor
            git init -b main
            git config user.email "test@forge.local"
            git config user.name "Forge Test"
            echo "# Forge Anchor" > README.md
            git add . && git commit -m "init"
            /forge/.venv/bin/forge extensions enable --scope local --profile minimal
        """)
        assert result.returncode == 0, f"Enable failed: {result.stderr}"

        claude_check = synced_container.exec("test -d ~/repo-forge-anchor/.claude && echo claude-ok")
        assert "claude-ok" in claude_check.stdout, ".claude/ should exist after enable"

        forge_check = synced_container.exec("test -d ~/repo-forge-anchor/.forge && echo forge-ok")
        assert "forge-ok" in forge_check.stdout, ".forge/ should exist after enable (Rule 1 anchor)"

    def test_init_project_dry_run_does_not_create_claude_anchor(self, synced_container: ContainerLike) -> None:
        """--dry-run should not create .claude/ as a side effect."""
        synced_container.exec("rm -rf ~/.claude ~/.forge ~/repo-dry-run")

        result = synced_container.exec("""
            mkdir -p ~/repo-dry-run && cd ~/repo-dry-run
            git init -b main
            git config user.email "test@forge.local"
            git config user.name "Forge Test"
            echo "# Dry Run" > README.md
            git add . && git commit -m "init"
            /forge/.venv/bin/forge extensions enable --scope project --profile minimal --dry-run
        """)
        assert result.returncode == 0, f"Dry-run enable failed: {result.stderr}"

        anchor_check = synced_container.exec("test ! -e ~/repo-dry-run/.claude && echo no-anchor")
        assert (
            "no-anchor" in anchor_check.stdout
        ), f".claude/ should not be created during dry-run: {anchor_check.stderr}"


class TestForgeUpdate:
    """Tests for forge update command."""

    def test_update_requires_existing_installation(self, synced_container: ContainerLike) -> None:
        """Verify forge extensions sync fails without prior install."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        result = synced_container.exec("cd /forge && uv run forge extensions sync --scope user 2>&1")
        assert result.returncode != 0
        # Error message says "no Forge installation found" or similar
        assert "no forge installation" in result.stdout.lower() or "forge extensions enable" in result.stdout.lower()

    def test_update_preserves_user_settings(self, synced_container: ContainerLike) -> None:
        """Verify update doesn't clobber user customizations."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        # Init first
        synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile minimal")

        # Add user customization to settings (preserve existing structure)
        synced_container.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
settings_path = Path.home() / '.claude' / 'settings.json'
settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
settings['userCustomKey'] = 'preserved'
settings_path.write_text(json.dumps(settings, indent=2))
"
        """)

        # Update
        result = synced_container.exec("cd /forge && uv run forge extensions sync --scope user")
        assert result.returncode == 0

        # User key should still be there
        check = synced_container.exec("""
            cd /forge && uv run python -c "
import json
from pathlib import Path
settings = json.loads(Path.home().joinpath('.claude/settings.json').read_text())
assert settings.get('userCustomKey') == 'preserved', 'User key was lost'
print('preserved')
"
        """)
        assert "preserved" in check.stdout


class TestForgeUninstall:
    """Tests for forge uninstall command."""

    def test_uninstall_removes_tracked_files(self, synced_container: ContainerLike) -> None:
        """Verify forge extensions disable removes installed files."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        # Init first
        synced_container.exec("cd /forge && uv run forge extensions enable --scope user --profile minimal")

        # Verify installation exists
        check1 = synced_container.exec("test -d ~/.claude && echo 'exists'")
        assert "exists" in check1.stdout

        # Uninstall (--force to avoid confirmation prompt hanging)
        result = synced_container.exec("cd /forge && uv run forge extensions disable --scope user --force")
        assert result.returncode == 0

        # Verify tracking entry removed (file may still exist but scope entry gone)
        check2 = synced_container.exec("""
            cd /forge && uv run python -c "
import json
from forge.install.tracking import get_tracking_path
tracking_path = get_tracking_path()
if not tracking_path.exists():
    print('file gone')
else:
    manifest = json.loads(tracking_path.read_text())
    if 'user' not in manifest.get('installations', {}):
        print('entry removed')
    else:
        print('entry still exists')
"
        """)
        assert "entry removed" in check2.stdout or "file gone" in check2.stdout

    def test_uninstall_without_installation_is_noop(self, synced_container: ContainerLike) -> None:
        """Verify forge extensions disable on empty system is a graceful no-op."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        result = synced_container.exec("cd /forge && uv run forge extensions disable --scope user --force 2>&1")
        # CLI returns 0 and informs user - graceful no-op behavior
        assert result.returncode == 0
        assert "no forge installation" in result.stdout.lower()


class TestSymlinkMode:
    """Tests for symlink installation mode."""

    def test_symlink_mode_creates_symlinks(self, synced_container: ContainerLike) -> None:
        """Verify --symlink creates symlinks not copies."""
        synced_container.exec("rm -rf ~/.claude ~/.forge")

        result = synced_container.exec(
            "cd /forge && uv run forge extensions enable --scope user --profile standard --symlink"
        )
        assert result.returncode == 0

        # Check skills directory for symlinks (skills are always present in standard profile)
        check = synced_container.exec("""
            cd /forge && uv run python -c "
from pathlib import Path
skills_dir = Path.home() / '.claude' / 'skills'
skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
assert len(skill_dirs) > 0, 'No skill directories found'
md_files = list(skill_dirs[0].glob('*.md'))
assert len(md_files) > 0, f'No .md files in {skill_dirs[0]}'
assert md_files[0].is_symlink(), f'{md_files[0]} is not a symlink'
print('symlinks verified')
"
        """)
        assert check.returncode == 0, f"Symlink check failed: {check.stderr}"
        assert "symlinks verified" in check.stdout
