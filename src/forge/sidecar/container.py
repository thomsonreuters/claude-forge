"""Container lifecycle management for sidecar Claude Code sessions.

Bundles proxy + Claude Code in a Docker container. The key function
`run_sidecar_session()` is the container equivalent of `invoke_claude()`
— it runs interactively with inherited stdin/stdout/stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from forge.sidecar.docker import _docker_name_filter


class ContainerExistsError(RuntimeError):
    """Raised when a container with the given name already exists."""

    def __init__(self, container_name: str) -> None:
        self.container_name = container_name
        super().__init__(f"Container '{container_name}' already exists. " f"Remove with: docker rm -f {container_name}")


def get_container_id(container_name: str) -> str | None:
    """Get container ID by name (for running containers only)."""
    result = subprocess.run(
        ["docker", "ps", "-q", "-f", _docker_name_filter(container_name)],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def container_exists(container_name: str) -> bool:
    """Check if a container exists by name (running OR stopped).

    Uses `docker ps -a` to detect ALL containers, including stopped/exited ones.
    """
    result = subprocess.run(
        ["docker", "ps", "-aq", "-f", _docker_name_filter(container_name)],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def run_sidecar_session(
    *,
    image: str,
    template: str,
    session_name: str,
    project_dir: Path,
    extra_mounts: list[tuple[str, str, str]] | None = None,
    context_limit: int = 200000,
    env_vars: dict[str, str] | None = None,
    claude_args: list[str] | None = None,
) -> int:
    """Run Claude + proxy in a Docker container. Returns exit code.

    Container lifecycle = Session lifecycle:
    - Container starts when this function is called
    - Container exits when Claude exits
    - Container auto-cleaned via --rm flag
    """
    container_name = f"forge-{session_name}"

    # Collision guard: detect both running AND stopped containers
    if container_exists(container_name):
        raise ContainerExistsError(container_name)

    cmd = [
        "docker",
        "run",
        "-it",
        "--rm",
        "--name",
        container_name,
        "-v",
        f"{project_dir}:/workspace",
        "-e",
        f"FORGE_TEMPLATE={template}",
        "-e",
        f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={context_limit}",
        "-e",
        f"FORGE_SESSION={session_name}",
        "-e",
        "FORGE_SIDECAR=1",
        "-e",
        "FORGE_LAUNCH_MODE=sidecar",
        "-w",
        "/workspace",
    ]

    if sys.platform == "linux":
        uid, gid = os.getuid(), os.getgid()
        cmd.extend(["--user", f"{uid}:{gid}"])

    if extra_mounts:
        for host_path, container_path, mode in extra_mounts:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])

    # Write env vars to temp file instead of CLI args to avoid
    # leaking secrets via `ps aux` (CR-022). Cleanup in finally.
    env_file_path: str | None = None
    try:
        if env_vars:
            fd, env_file_path = tempfile.mkstemp(prefix=".forge-env-", suffix=".env")
            with os.fdopen(fd, "w") as f:
                for k, v in env_vars.items():
                    f.write(f"{k}={v}\n")
            os.chmod(env_file_path, 0o600)
            cmd.extend(["--env-file", env_file_path])

        cmd.append(image)
        if claude_args:
            cmd.extend(claude_args)

        result = subprocess.run(cmd)
        return result.returncode
    finally:
        if env_file_path:
            try:
                os.unlink(env_file_path)
            except OSError:
                pass


def exec_in_container(container_name: str, command: list[str]) -> int:
    """Execute interactive command in running container."""
    cmd = ["docker", "exec", "-it", container_name, *command]
    result = subprocess.run(cmd)
    return result.returncode


def parse_mounts(mount_specs: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Parse --mount flag specifications into (host, container, mode) tuples.

    Format: "host_path:container_path[:ro|rw]"
    Default mode is "rw" if not specified.
    """
    mounts = []
    for spec in mount_specs:
        parts = spec.split(":")

        if len(parts) < 2:
            raise ValueError(f"Invalid mount specification: {spec}. Expected 'host:container[:ro|rw]'")

        host_path = parts[0]
        container_path = parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"

        if mode not in ("ro", "rw"):
            raise ValueError(f"Invalid mount mode: {mode}. Must be 'ro' or 'rw'")

        host_path = os.path.expanduser(host_path)
        mounts.append((host_path, container_path, mode))

    return mounts
