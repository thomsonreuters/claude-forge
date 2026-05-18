"""Docker-based integration tests for `forge claude start`."""

from __future__ import annotations

import pytest

from tests.fixtures.docker import ContainerLike

pytestmark = [pytest.mark.integration, pytest.mark.docker_in]


class TestClaudeStartBareLauncher:
    """Bare launcher integration coverage."""

    def test_direct_launch_honors_default_model_without_session_state(
        self,
        mock_claude_workspace: ContainerLike,
        claude_capture_file,
    ) -> None:
        """Direct bare launch should pin the configured model and avoid session state."""
        mkdir_result = mock_claude_workspace.mkdir("$HOME/.forge")
        assert mkdir_result.returncode == 0, mkdir_result.stderr

        write_result = mock_claude_workspace.write_file(
            "$HOME/.forge/config.yaml",
            "default_direct_model: claude-sonnet-4-6\n",
        )
        assert write_result.returncode == 0, write_result.stderr

        result = mock_claude_workspace.exec("cd /workspace && forge claude start --no-proxy")
        assert result.returncode == 0, f"Direct bare launch failed: {result.stderr}"

        invocations = mock_claude_workspace.read_file("/tmp/claude_invocations.log")
        assert "--model" not in invocations, invocations

        env_capture = claude_capture_file("/tmp/claude_env_*.log")
        assert "ANTHROPIC_MODEL=sonnet" in env_capture
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6" in env_capture
        assert "FORGE_SESSION=" not in env_capture
        assert "ANTHROPIC_BASE_URL=" not in env_capture
        assert "ACTIVE_TEMPLATE=" not in env_capture

        # Bare launcher must not create session state (no sessions dir, no index)
        assert not mock_claude_workspace.file_exists("/workspace/.forge/sessions")
        assert not mock_claude_workspace.file_exists("$HOME/.forge/sessions/index.json")
