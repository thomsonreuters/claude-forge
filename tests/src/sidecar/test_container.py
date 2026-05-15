"""Unit tests for sidecar container lifecycle functions.

Tests container command building, mount parsing, and error handling.
Docker interactions are mocked to enable fast, deterministic testing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.sidecar.container import (
    ContainerExistsError,
    container_exists,
    exec_in_container,
    get_container_id,
    parse_mounts,
    run_sidecar_session,
)
from forge.sidecar.docker import (
    is_container_running,
    is_docker_available,
    remove_container,
    stop_container,
)


class TestParseMounts:
    """Tests for mount specification parsing."""

    def test_parse_simple_mount(self) -> None:
        """Parse basic host:container mount."""
        mounts = parse_mounts(("/home/user/code:/workspace",))
        assert mounts == [("/home/user/code", "/workspace", "rw")]

    def test_parse_mount_with_ro_mode(self) -> None:
        """Parse mount with read-only mode."""
        mounts = parse_mounts(("/home/user/.ssh:/root/.ssh:ro",))
        assert mounts == [("/home/user/.ssh", "/root/.ssh", "ro")]

    def test_parse_mount_with_rw_mode(self) -> None:
        """Parse mount with explicit read-write mode."""
        mounts = parse_mounts(("/data:/mnt/data:rw",))
        assert mounts == [("/data", "/mnt/data", "rw")]

    def test_parse_multiple_mounts(self) -> None:
        """Parse multiple mount specifications."""
        mounts = parse_mounts(
            (
                "/code:/workspace",
                "/home/user/.aws:/root/.aws:ro",
            )
        )
        assert len(mounts) == 2
        assert mounts[0] == ("/code", "/workspace", "rw")
        assert mounts[1] == ("/home/user/.aws", "/root/.aws", "ro")

    def test_parse_tilde_expansion(self) -> None:
        """Tilde in host path is expanded."""
        mounts = parse_mounts(("~/.ssh:/root/.ssh:ro",))
        assert mounts[0][0] != "~/.ssh"  # Should be expanded
        assert mounts[0][0].startswith("/")  # Should be absolute

    def test_parse_invalid_mount_missing_container(self) -> None:
        """Reject mount with only host path."""
        with pytest.raises(ValueError, match="Invalid mount specification"):
            parse_mounts(("/host/only",))

    def test_parse_invalid_mount_bad_mode(self) -> None:
        """Reject mount with invalid mode."""
        with pytest.raises(ValueError, match="Invalid mount mode"):
            parse_mounts(("/host:/container:xx",))


class TestGetContainerId:
    """Tests for container ID lookup."""

    def test_get_container_id_found(self) -> None:
        """Return container ID when found."""
        with patch("forge.sidecar.container.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123def456\n", returncode=0)
            result = get_container_id("forge-test")

            assert result == "abc123def456"
            mock_run.assert_called_once()
            # Verify exact name match filter
            call_args = mock_run.call_args[0][0]
            assert "name=^forge\\-test$" in call_args

    def test_get_container_id_not_found(self) -> None:
        """Return None when container not running."""
        with patch("forge.sidecar.container.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            result = get_container_id("forge-nonexistent")

            assert result is None


class TestContainerExists:
    """Tests for container existence check (running OR stopped)."""

    def test_container_exists_running(self) -> None:
        """Return True for running container."""
        with patch("forge.sidecar.container.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123\n", returncode=0)
            result = container_exists("forge-test")

            assert result is True
            # Verify uses -a flag (all containers)
            call_args = mock_run.call_args[0][0]
            assert "-aq" in call_args
            assert "name=^forge\\-test$" in call_args

    def test_container_exists_stopped(self) -> None:
        """Return True for stopped (exited) container."""
        with patch("forge.sidecar.container.subprocess.run") as mock_run:
            # docker ps -a returns stopped containers too
            mock_run.return_value = MagicMock(stdout="def456\n", returncode=0)
            result = container_exists("forge-orphan")

            assert result is True

    def test_container_exists_not_found(self) -> None:
        """Return False when no container exists."""
        with patch("forge.sidecar.container.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            result = container_exists("forge-nonexistent")

            assert result is False


class TestIsContainerRunning:
    """Tests for container running status check."""

    def test_is_container_running_true(self) -> None:
        """Return True for running container."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123\n", returncode=0)
            assert is_container_running("forge-test") is True

    def test_is_container_running_false(self) -> None:
        """Return False when no container running."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            assert is_container_running("forge-test") is False


class TestIsDockerAvailable:
    """Tests for Docker availability check."""

    def test_docker_available(self) -> None:
        """Return True when docker info succeeds."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert is_docker_available() is True

    def test_docker_not_available(self) -> None:
        """Return False when docker info fails."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert is_docker_available() is False

    def test_docker_not_installed(self) -> None:
        """Return False when docker command not found."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert is_docker_available() is False


class TestRunSidecarSession:
    """Tests for sandboxed session execution."""

    def test_run_sidecar_session_builds_correct_command(self) -> None:
        """Verify docker run command construction."""
        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test-session",
                project_dir=Path("/home/user/code"),
                context_limit=300000,
            )

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]

            # Verify key flags
            assert "docker" in cmd
            assert "run" in cmd
            assert "-it" in cmd
            assert "--rm" in cmd
            assert "--name" in cmd
            assert "forge-test-session" in cmd
            assert "/home/user/code:/workspace" in " ".join(cmd)
            assert "FORGE_TEMPLATE=litellm-openai" in " ".join(cmd)
            assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000" in " ".join(cmd)
            assert "FORGE_SESSION=test-session" in " ".join(cmd)
            assert "FORGE_SIDECAR=1" in " ".join(cmd)
            assert "FORGE_LAUNCH_MODE=sidecar" in " ".join(cmd)
            assert "forge-sidecar:latest" in cmd

    def test_run_sidecar_session_with_extra_mounts(self) -> None:
        """Verify extra mounts are added."""
        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test",
                project_dir=Path("/code"),
                extra_mounts=[
                    ("/home/user/.ssh", "/root/.ssh", "ro"),
                    ("/home/user/.aws", "/root/.aws", "ro"),
                ],
            )

            cmd = " ".join(mock_run.call_args[0][0])
            assert "/home/user/.ssh:/root/.ssh:ro" in cmd
            assert "/home/user/.aws:/root/.aws:ro" in cmd

    def test_run_sidecar_session_appends_claude_args_after_image(self) -> None:
        """Claude arguments should be passed through to the container entrypoint."""
        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test",
                project_dir=Path("/code"),
                claude_args=["--resume", "parent-uuid", "--fork-session"],
            )

            cmd = mock_run.call_args[0][0]
            assert cmd[-4:] == [
                "forge-sidecar:latest",
                "--resume",
                "parent-uuid",
                "--fork-session",
            ]

    def test_run_sidecar_session_uses_env_file_not_cli_args(self) -> None:
        """Env vars passed via --env-file, not -e KEY=VALUE (CR-022)."""
        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test",
                project_dir=Path("/code"),
                env_vars={"LITELLM_API_KEY": "secret123"},
            )

            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(cmd)

            # Secrets must NOT appear as CLI args
            assert "secret123" not in cmd_str
            assert "LITELLM_API_KEY=secret123" not in cmd_str

            # Must use --env-file instead
            assert "--env-file" in cmd

    def test_run_sidecar_session_env_file_cleanup(self) -> None:
        """Env file is deleted after subprocess completes."""
        import os

        env_file_paths: list[str] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> MagicMock:
            # Find the --env-file path in the command
            for i, arg in enumerate(cmd):
                if arg == "--env-file" and i + 1 < len(cmd):
                    env_file_paths.append(cmd[i + 1])
            return MagicMock(returncode=0)

        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run", side_effect=capture_cmd),
        ):
            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test",
                project_dir=Path("/code"),
                env_vars={"KEY": "val"},
            )

            # File should have been created and then cleaned up
            assert len(env_file_paths) == 1
            assert not os.path.exists(env_file_paths[0])

    def test_run_sidecar_session_env_file_cleanup_on_error(self) -> None:
        """Env file is cleaned up even when subprocess raises."""
        import os

        env_file_paths: list[str] = []

        def capture_and_raise(cmd: list[str], **kwargs: object) -> None:
            for i, arg in enumerate(cmd):
                if arg == "--env-file" and i + 1 < len(cmd):
                    env_file_paths.append(cmd[i + 1])
            raise OSError("docker crashed")

        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run", side_effect=capture_and_raise),
        ):
            with pytest.raises(OSError, match="docker crashed"):
                run_sidecar_session(
                    image="forge-sidecar:latest",
                    template="litellm-openai",
                    session_name="test",
                    project_dir=Path("/code"),
                    env_vars={"KEY": "val"},
                )

            assert len(env_file_paths) == 1
            assert not os.path.exists(env_file_paths[0])

    def test_run_sidecar_session_env_file_permissions(self) -> None:
        """Env file created with restrictive permissions (0600)."""
        import os
        import stat

        captured_perms: list[int] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> MagicMock:
            for i, arg in enumerate(cmd):
                if arg == "--env-file" and i + 1 < len(cmd):
                    path = cmd[i + 1]
                    captured_perms.append(stat.S_IMODE(os.stat(path).st_mode))
            return MagicMock(returncode=0)

        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run", side_effect=capture_cmd),
        ):
            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test",
                project_dir=Path("/code"),
                env_vars={"SECRET": "value"},
            )

            assert len(captured_perms) == 1
            assert captured_perms[0] == 0o600

    def test_run_sidecar_session_no_env_file_when_no_vars(self) -> None:
        """No --env-file flag when env_vars is empty or None."""
        with (
            patch("forge.sidecar.container.container_exists", return_value=False),
            patch("forge.sidecar.container.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            run_sidecar_session(
                image="forge-sidecar:latest",
                template="litellm-openai",
                session_name="test",
                project_dir=Path("/code"),
            )

            cmd = mock_run.call_args[0][0]
            assert "--env-file" not in cmd

    def test_run_sidecar_session_raises_on_existing_container(self) -> None:
        """Raise error when container already exists (running or stopped)."""
        with patch("forge.sidecar.container.container_exists", return_value=True):
            with pytest.raises(ContainerExistsError) as exc_info:
                run_sidecar_session(
                    image="forge-sidecar:latest",
                    template="litellm-openai",
                    session_name="test",
                    project_dir=Path("/code"),
                )

            assert "forge-test" in str(exc_info.value)
            assert "docker rm -f" in str(exc_info.value)


class TestExecInContainer:
    """Tests for exec into container."""

    def test_exec_in_container_calls_docker_exec(self) -> None:
        """Verify docker exec command."""
        with patch("forge.sidecar.container.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = exec_in_container("forge-test", ["/bin/bash"])

            assert result == 0
            cmd = mock_run.call_args[0][0]
            assert cmd == ["docker", "exec", "-it", "forge-test", "/bin/bash"]


class TestStopContainer:
    """Tests for container stop."""

    def test_stop_container_success(self) -> None:
        """Return True on successful stop."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = stop_container("forge-test")

            assert result is True
            mock_run.assert_called_once()
            assert mock_run.call_args[0][0] == ["docker", "stop", "forge-test"]

    def test_stop_container_not_running(self) -> None:
        """Return False when container not running."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            result = stop_container("forge-nonexistent")

            assert result is False


class TestRemoveContainer:
    """Tests for container removal."""

    def test_remove_container_basic(self) -> None:
        """Remove container without force."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = remove_container("forge-test")

            assert result is True
            assert mock_run.call_args[0][0] == ["docker", "rm", "forge-test"]

    def test_remove_container_force(self) -> None:
        """Remove container with force flag."""
        with patch("forge.sidecar.docker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = remove_container("forge-test", force=True)

            assert result is True
            assert mock_run.call_args[0][0] == ["docker", "rm", "-f", "forge-test"]
