"""Fixtures for CLI integration tests.

These fixtures provide Docker-based isolation for tests that touch protected paths
(~/.forge/, ~/.claude/, .claude/). Even if test code has bugs, it can't corrupt
host data.

Marker: @pytest.mark.docker_in

These tests exec commands inside a session-scoped container (not DinD).
"""

from __future__ import annotations

from collections.abc import Callable, Generator

import pytest

from tests.fixtures.docker import ContainerLike


@pytest.fixture
def mock_claude_workspace(
    clean_workspace: ContainerLike,
) -> Generator[ContainerLike, None, None]:
    """Per-test workspace with mock claude binary and forge in PATH.

    Creates:
    - Symlink /usr/local/bin/forge -> /forge/.venv/bin/forge
    - A mock `claude` script that logs invocations, env, and stdin
    - Cleans ~/.forge/ (global session index) for test isolation

    This allows testing forge CLI commands that invoke Claude without
    actually launching Claude Code.
    """
    # Create symlink for forge, mock claude script, and clean global state
    result = clean_workspace.exec("""
        # Remote templates need LITELLM_BASE_URL; direct Anthropic workers
        # need ANTHROPIC_API_KEY for preflight validation.
        # Write to /forge/.env (WORKDIR) so load_dotenv() picks it up.
        echo 'LITELLM_BASE_URL=https://litellm.test.example.com' >> /forge/.env
        echo 'LITELLM_API_KEY=sk-litellm-test-mock-key' >> /forge/.env
        echo 'ANTHROPIC_API_KEY=sk-ant-test-mock-key' >> /forge/.env

        # Clean global forge state (session index, pending-work, etc.)
        rm -rf ~/.forge ~/.claude

        # Symlink forge to PATH so it's available from any directory
        ln -sf /forge/.venv/bin/forge /usr/local/bin/forge

        # Create mock script in /tmp (works for both npm and native installs)
        cat > /tmp/claude-mock << 'SCRIPT'
#!/bin/bash
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

exit_code="${FORGE_MOCK_CLAUDE_EXIT_CODE:-0}"
exit "${exit_code}"
SCRIPT
        chmod +x /tmp/claude-mock

        # Backup and mock both possible Claude locations (npm and native)
        # npm install: /usr/local/bin/claude
        if [ -f /usr/local/bin/claude ]; then
            mv /usr/local/bin/claude /usr/local/bin/claude-real
            ln -sf /tmp/claude-mock /usr/local/bin/claude
        fi

        # native install: /root/.local/bin/claude
        if [ -f /root/.local/bin/claude ]; then
            mv /root/.local/bin/claude /root/.local/bin/claude-real
            ln -sf /tmp/claude-mock /root/.local/bin/claude
        fi

        # Clear capture files
        > /tmp/claude_invocations.log
        rm -f /tmp/claude_env_*.log /tmp/claude_stdin_*.log 2>/dev/null || true

        # Rule 1: forge session start requires .forge/.
        # Create project anchor so session commands work without explicit enable.
        mkdir -p /workspace/.forge /workspace/.claude
    """)
    if result.returncode != 0:
        pytest.fail(f"Failed to create mock claude: {result.stderr}")

    yield clean_workspace

    # Restore real claude after test (both locations)
    clean_workspace.exec("""
        # Restore npm installation
        if [ -f /usr/local/bin/claude-real ]; then
            rm -f /usr/local/bin/claude
            mv /usr/local/bin/claude-real /usr/local/bin/claude
        fi

        # Restore native installation
        if [ -f /root/.local/bin/claude-real ]; then
            rm -f /root/.local/bin/claude
            mv /root/.local/bin/claude-real /root/.local/bin/claude
        fi

        # Clean up mock
        rm -f /tmp/claude-mock
        rm -f /tmp/claude_env_*.log /tmp/claude_stdin_*.log /tmp/claude_invocations.log
    """)


@pytest.fixture
def claude_invocations(mock_claude_workspace: ContainerLike) -> Callable[[], list[str]]:
    """Read Claude invocations after test completes.

    Returns list of command lines that were passed to mock claude.
    Useful for asserting that forge invoked claude with correct args.

    Usage:
        def test_something(mock_claude_workspace, claude_invocations):
            mock_claude_workspace.exec("forge session start test")
            # claude_invocations is populated by the fixture's finalizer

    Note: This fixture must be evaluated AFTER test code runs to capture
    invocations. Use request.getfixturevalue() or call it at end of test.
    """

    # This is a factory fixture - call it to get current invocations
    def get_invocations() -> list[str]:
        result = mock_claude_workspace.exec("cat /tmp/claude_invocations.log 2>/dev/null || true")
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        # Extract command part (after timestamp)
        return [line.split(" ", 1)[1] if " " in line else line for line in lines if line]

    return get_invocations


@pytest.fixture
def claude_capture_file(mock_claude_workspace: ContainerLike) -> Callable[[str], str]:
    """Read the first captured Claude file matching a glob expression."""

    def read_capture(glob_expr: str) -> str:
        result = mock_claude_workspace.exec(f"ls -1 {glob_expr} 2>/dev/null | head -n 1")
        path = result.stdout.strip()
        assert path, f"No files matched: {glob_expr}"
        return mock_claude_workspace.read_file(path)

    return read_capture
