"""Claude subprocess management for headless (-p) mode.

Provides a unified interface for running ``claude -p`` as a subprocess
with structured result handling. Used by the semantic supervisor
(``claude -p --resume``) and handoff agent (``claude -p``).

For interactive sessions (stdin/stdout inherited), use
``forge.session.claude.invoke.invoke_claude()`` instead.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from forge.core.reactive.env import (
    FORGE_SUBPROCESS_PROXY_VAR,
    build_claude_env,
    can_use_bare,
)

_log = logging.getLogger(__name__)


@dataclass
class SessionResult:
    """Result from a ``claude -p`` invocation.

    The runner never raises — all errors are captured in the ``error`` field.
    Callers inspect ``success`` and ``error`` to decide their own fail
    behavior (fail-open warnings for supervisor, return False for handoff).
    """

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        """True if the subprocess completed successfully."""
        return self.returncode == 0 and not self.timed_out and self.error is None


def run_claude_session(
    prompt: str,
    *,
    resume_id: str | None = None,
    fork_session: bool = False,
    bare: bool | None = None,
    base_url: str | None = None,
    direct: bool = False,
    timeout_seconds: int = 60,
    cwd: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> SessionResult:
    """Run ``claude -p`` as a headless subprocess.

    Builds the command, environment, and runs ``subprocess.run`` with
    ``capture_output=True``. All exceptions are caught and reported
    via ``SessionResult.error``.

    Args:
        prompt: Text sent to claude via stdin.
        resume_id: If set, adds ``--resume <id>`` to continue a session.
        fork_session: If True and resume_id is set, adds ``--fork-session``
            to create an ephemeral fork instead of appending to the
            original conversation.
        bare: If True, adds ``--bare`` to skip hooks/LSP/plugins.
            None (default) auto-detects: uses ``--bare`` only when
            ANTHROPIC_API_KEY is present (``--bare`` disables OAuth).
        base_url: Proxy URL (sets ANTHROPIC_BASE_URL in environment).
        timeout_seconds: Maximum seconds to wait for completion.
        cwd: Working directory for the subprocess.
        extra_env: Additional environment variables.

    Returns:
        SessionResult with stdout/stderr/returncode or error details.
    """
    env = build_claude_env(base_url=base_url, extra_vars=extra_env, direct=direct)

    use_bare = bare if bare is not None else can_use_bare(env)
    cmd = ["claude", "-p"]
    if use_bare:
        cmd.append("--bare")
    if resume_id:
        cmd.extend(["--resume", resume_id])
        if fork_session:
            cmd.append("--fork-session")

    # Guard: fail if subprocess proxy was configured but didn't resolve.
    # Prevents silent fallback to direct mode (which would burn subscription quota).
    subprocess_proxy = env.get(FORGE_SUBPROCESS_PROXY_VAR)
    if subprocess_proxy and not base_url and not direct and not env.get("ANTHROPIC_BASE_URL"):
        msg = (
            f"Subprocess proxy '{subprocess_proxy}' not available. "
            f"Start it with: forge proxy start {subprocess_proxy}"
        )
        _log.warning(msg)
        return SessionResult(stdout="", stderr="", returncode=-1, error=msg)

    # Guard: fail with actionable error if --bare was requested but no API key.
    # Without this, the subprocess would fail with a cryptic Claude CLI error.
    # Only fires when bare mode was explicitly requested (bare=True) — when
    # bare=None and no key exists, can_use_bare() returns False and Claude
    # falls through to OAuth (which may be intentional).
    if bare and not env.get("ANTHROPIC_BASE_URL") and not env.get("ANTHROPIC_API_KEY"):
        try:
            from forge.core.auth.capabilities import (
                CREDENTIALS,
                format_missing_credential_error,
            )
            from forge.runtime_config import get_runtime_config

            env_ignored = get_runtime_config().auth_ignore_env
            cred = CREDENTIALS.get("anthropic-api")
            if cred:
                msg = format_missing_credential_error(
                    cred,
                    missing_vars=["ANTHROPIC_API_KEY"],
                    context="Forge subprocess (claude -p)",
                    extra_hint="Or use --subprocess-proxy to route through an existing proxy.",
                    env_ignored=env_ignored,
                )
                _log.warning(msg)
                return SessionResult(stdout="", stderr="", returncode=-1, error=msg)
        except Exception as e:
            _log.debug("Could not format missing Anthropic subprocess credential error: %s", e)

    try:
        _log.debug(
            "Running claude session: cmd=%s, resume=%s, cwd=%s",
            cmd,
            resume_id and resume_id[:16],
            cwd,
        )

        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
            env=env,
        )

        if result.returncode != 0:
            _log.warning("claude -p returned non-zero exit code: %d", result.returncode)

        return SessionResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )

    except subprocess.TimeoutExpired:
        _log.warning("claude -p timed out after %ds", timeout_seconds)
        return SessionResult(
            stdout="",
            stderr="",
            returncode=-1,
            timed_out=True,
            error=f"Timed out after {timeout_seconds}s",
        )

    except FileNotFoundError:
        _log.error("claude CLI not found in PATH")
        return SessionResult(
            stdout="",
            stderr="",
            returncode=-1,
            error="claude CLI not found in PATH",
        )

    except Exception as e:
        _log.warning("claude -p failed: %s", e)
        return SessionResult(
            stdout="",
            stderr="",
            returncode=-1,
            error=str(e),
        )
