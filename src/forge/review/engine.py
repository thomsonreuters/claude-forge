"""Multi-model review engine with parallel fan-out.

Spawns N ``claude -p`` subprocesses in parallel via ThreadPoolExecutor,
one per model backend. Each subprocess runs in its own process group
(``start_new_session=True``) so that cleanup via ``os.killpg`` can
terminate orphaned children if the parent is interrupted.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from forge.core.auth.template_secrets import resolve_env_or_credential
from forge.core.reactive.env import (
    FORGE_SUBPROCESS_PROXY_VAR,
    build_claude_env,
    can_use_bare,
    should_spawn_subprocesses,
)
from forge.core.reactive.proxy import lookup_proxy_base_url
from forge.session.direct_model import direct_model_env

from .models import (
    DEFAULT_MODELS,
    ModelSpec,
    MultiReviewOutput,
    ReviewResult,
    check_model_availability,
)

_log = logging.getLogger(__name__)


def preflight_check(specs: list[ModelSpec]) -> list[str]:
    """Validate proxy reachability and auth before spawning workers.

    Delegates to check_model_availability() so discovery (list-models) and
    runtime (preflight) use identical health-check logic.

    Returns a list of error strings (empty means all OK).
    """
    availabilities = check_model_availability(specs)
    subprocess_proxy = os.environ.get(FORGE_SUBPROCESS_PROXY_VAR)
    errors: list[str] = []
    for avail in availabilities:
        if avail.status == "ready":
            continue
        if avail.spec.proxy:
            hint = f" Run 'forge proxy create {avail.spec.proxy}' to set it up."
        elif subprocess_proxy:
            hint = f" Run 'forge proxy start {subprocess_proxy}' to make the subprocess proxy available."
        else:
            hint = " Run 'forge auth login -c anthropic-api' or use --models to select only proxy-backed models."
        errors.append(f"{avail.spec.name}: {avail.reason}.{hint}")
    return errors


def run_multi_review(
    prompt: str,
    *,
    models: list[ModelSpec] | None = None,
    timeout_seconds: int = 600,
    cwd: str | None = None,
    resume_id: str | None = None,
) -> MultiReviewOutput:
    """Fan out a review prompt to multiple models in parallel.

    Args:
        prompt: The review prompt to send to each model.
        models: Model specs to use. Defaults to DEFAULT_MODELS values.
        timeout_seconds: Per-model timeout in seconds.
        cwd: Working directory for each subprocess.
        resume_id: If set, adds ``--resume <id>`` to each subprocess.

    Returns:
        MultiReviewOutput with per-model results in input order.
        Returns empty results if FORGE_DEPTH limit reached.
    """
    if not should_spawn_subprocesses():
        _log.debug("Skipping ensemble review at FORGE_DEPTH limit")
        return MultiReviewOutput(prompt=prompt)

    specs = models if models is not None else list(DEFAULT_MODELS.values())

    if not specs:
        return MultiReviewOutput(prompt=prompt)

    # Thread-safe list for tracking child processes
    children: list[subprocess.Popen[str]] = []
    children_lock = threading.Lock()

    def _cleanup() -> None:
        """Terminate and reap all running children. SIGTERM → wait → SIGKILL."""
        with children_lock:
            for proc in children:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
            # Reap children; escalate to SIGKILL if SIGTERM didn't work
            for proc in children:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        proc.wait(timeout=2)
                    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    pass

    def _run_single(spec: ModelSpec) -> ReviewResult:
        """Run a single model review. Called from a worker thread."""
        start = time.monotonic()
        if spec.prompt is None:
            worker_prompt = prompt
        elif spec.prompt_mode == "prefix":
            worker_prompt = f"{spec.prompt}\n\n{prompt}" if prompt else spec.prompt
        else:
            worker_prompt = spec.prompt

        extra_env: dict[str, str] = {}
        if not os.environ.get("ANTHROPIC_API_KEY"):
            ak = resolve_env_or_credential("ANTHROPIC_API_KEY")
            if ak:
                extra_env["ANTHROPIC_API_KEY"] = ak

        if spec.direct:
            if not spec.direct_model:
                duration = time.monotonic() - start
                return ReviewResult(
                    model_name=spec.effective_worker_id,
                    stdout="",
                    stderr="",
                    success=False,
                    duration_seconds=duration,
                    error=f"Direct model spec '{spec.name}' is missing direct_model",
                )
            try:
                extra_env.update(direct_model_env(spec.direct_model))
            except ValueError as e:
                duration = time.monotonic() - start
                return ReviewResult(
                    model_name=spec.effective_worker_id,
                    stdout="",
                    stderr="",
                    success=False,
                    duration_seconds=duration,
                    error=str(e),
                )
            base_url = None
            env = build_claude_env(direct=True, extra_vars=extra_env or None)
        else:
            try:
                base_url = lookup_proxy_base_url(spec.proxy)
            except Exception as e:
                duration = time.monotonic() - start
                return ReviewResult(
                    model_name=spec.effective_worker_id,
                    stdout="",
                    stderr="",
                    success=False,
                    duration_seconds=duration,
                    error=f"Proxy '{spec.proxy}' not found: {e}",
                )

            env = build_claude_env(base_url=base_url, extra_vars=extra_env or None)
            subprocess_proxy = env.get(FORGE_SUBPROCESS_PROXY_VAR)
            if not base_url and subprocess_proxy and not env.get("ANTHROPIC_BASE_URL"):
                duration = time.monotonic() - start
                return ReviewResult(
                    model_name=spec.effective_worker_id,
                    stdout="",
                    stderr="",
                    success=False,
                    duration_seconds=duration,
                    error=(
                        f"Subprocess proxy '{subprocess_proxy}' not available. "
                        f"Start it with: forge proxy start {subprocess_proxy}"
                    ),
                )
            if not base_url and not subprocess_proxy:
                # No explicit proxy or subprocess proxy. Scrub any inherited
                # ANTHROPIC_BASE_URL to keep direct workers direct.
                env.pop("ANTHROPIC_BASE_URL", None)

        cmd = ["claude", "-p"]
        if can_use_bare(env):
            cmd.append("--bare")
        if resume_id:
            cmd.extend(["--resume", resume_id])
        if spec.model_flag and not spec.direct:
            cmd.extend(["--model", spec.model_flag])

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            with children_lock:
                children.append(proc)

            stdout, stderr = proc.communicate(input=worker_prompt, timeout=timeout_seconds)
            duration = time.monotonic() - start

            if proc.returncode != 0:
                error_msg = stderr.strip() or f"Exit code {proc.returncode}"
                return ReviewResult(
                    model_name=spec.effective_worker_id,
                    stdout=stdout,
                    stderr=stderr,
                    success=False,
                    duration_seconds=duration,
                    error=error_msg,
                )

            return ReviewResult(
                model_name=spec.effective_worker_id,
                stdout=stdout.strip(),
                stderr=stderr,
                success=True,
                duration_seconds=duration,
            )

        except subprocess.TimeoutExpired:
            # Kill the process group and reap to avoid zombies
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass
            return ReviewResult(
                model_name=spec.effective_worker_id,
                stdout="",
                stderr="",
                success=False,
                duration_seconds=float(timeout_seconds),
                error=f"Timeout after {timeout_seconds}s",
            )

        except FileNotFoundError:
            duration = time.monotonic() - start
            return ReviewResult(
                model_name=spec.effective_worker_id,
                stdout="",
                stderr="",
                success=False,
                duration_seconds=duration,
                error="claude CLI not found in PATH",
            )

        except (OSError, subprocess.SubprocessError) as e:
            duration = time.monotonic() - start
            return ReviewResult(
                model_name=spec.effective_worker_id,
                stdout="",
                stderr="",
                success=False,
                duration_seconds=duration,
                error=str(e),
            )

    # Fan out with ThreadPoolExecutor, preserving input order and duplicate workers.
    result_map: dict[int, ReviewResult] = {}
    max_workers = min(len(specs), 5)

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {executor.submit(_run_single, spec): (idx, spec) for idx, spec in enumerate(specs)}
            for future in as_completed(future_to_item):
                idx, spec = future_to_item[future]
                wid = spec.effective_worker_id
                try:
                    result_map[idx] = future.result()
                except Exception as e:
                    result_map[idx] = ReviewResult(
                        model_name=wid,
                        stdout="",
                        stderr="",
                        success=False,
                        duration_seconds=0.0,
                        error=f"Thread error: {e}",
                    )
    finally:
        _cleanup()

    # Return in deterministic input order
    ordered = [result_map[idx] for idx in range(len(specs)) if idx in result_map]
    return MultiReviewOutput(prompt=prompt, results=ordered)
