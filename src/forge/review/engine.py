"""Multi-model review engine with parallel fan-out.

Spawns N ``claude -p`` subprocesses in parallel via ThreadPoolExecutor,
one per model backend. Each subprocess runs in its own process group
(``start_new_session=True``) so that cleanup via ``os.killpg`` can
terminate orphaned children if the parent is interrupted.

Routing is pre-resolved: the engine receives a ``WorkerRoutingPlan``
and passes each worker its ``RoutingResult``. No per-worker registry
lookups during fan-out.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from forge.core.auth.capabilities import CREDENTIALS, format_missing_credential_error
from forge.core.auth.template_secrets import resolve_env_or_credential
from forge.core.reactive.env import (
    build_claude_env,
    can_use_bare,
    should_spawn_subprocesses,
)
from forge.core.reactive.routing import RoutingResult
from forge.review.routing import (
    WorkerRoutingPlan,
    resolve_invocation_routing,
    resolve_model_flag,
)
from forge.session.direct_model import direct_model_env

from .models import (
    DEFAULT_MODELS,
    ModelSpec,
    MultiReviewOutput,
    ReviewResult,
)

_log = logging.getLogger(__name__)


def preflight_check(
    specs: list[ModelSpec],
    routing_plan: WorkerRoutingPlan | None = None,
) -> list[str]:
    """Validate routing before spawning workers.

    When a routing_plan is provided, validates each result has a route.
    Otherwise falls back to check_model_availability().

    Returns a list of error strings (empty means all OK).
    """
    if routing_plan is not None:
        errors: list[str] = []
        for spec, result in zip(specs, routing_plan.routes):
            if result.route is None:
                reason = result.warning or "No compatible route found"
                errors.append(f"{spec.name}: {reason}")
                continue

            credential_error = _credential_preflight_error(spec, result)
            if credential_error:
                errors.append(credential_error)
        return errors

    from .models import check_model_availability

    availabilities = check_model_availability(specs)
    errors = []
    for avail in availabilities:
        if avail.status == "ready":
            continue
        if avail.spec.preferred_proxy:
            hint = f" Run 'forge proxy create {avail.spec.preferred_proxy}' to set it up."
        else:
            hint = " Run 'forge auth login -c anthropic-api' or use --models to select only proxy-backed models."
        errors.append(f"{avail.spec.name}: {avail.reason}.{hint}")
    return errors


def _credential_preflight_error(spec: ModelSpec, result: RoutingResult) -> str | None:
    """Return an actionable missing-credential error for direct workflow routes."""
    route = result.route
    if route is None or route.provider != "direct":
        return None

    credential = CREDENTIALS.get(route.credential)
    if credential is None:
        return None

    missing_vars = [
        env_var.name
        for env_var in credential.env_vars
        if env_var.required and not resolve_env_or_credential(env_var.name)
    ]
    if not missing_vars:
        return None

    return format_missing_credential_error(
        credential,
        missing_vars=missing_vars,
        context=f"Workflow model '{spec.name}'",
    )


def run_multi_review(
    prompt: str,
    *,
    models: list[ModelSpec] | None = None,
    routing_plan: WorkerRoutingPlan | None = None,
    timeout_seconds: int = 600,
    cwd: str | None = None,
    resume_id: str | None = None,
) -> MultiReviewOutput:
    """Fan out a review prompt to multiple models in parallel.

    Args:
        prompt: The review prompt to send to each model.
        models: Model specs to use. Defaults to DEFAULT_MODELS values.
        routing_plan: Pre-resolved routing for all workers. When None,
            resolves routing once at the top before the thread pool.
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

    # Resolve routing once if not provided by caller
    if routing_plan is None:
        try:
            routing_plan = resolve_invocation_routing(specs)
        except Exception as e:
            _log.warning("Routing resolution failed: %s", e)
            return MultiReviewOutput(
                prompt=prompt,
                results=[
                    ReviewResult(
                        model_name=s.effective_worker_id,
                        stdout="",
                        stderr="",
                        success=False,
                        duration_seconds=0.0,
                        error=str(e),
                    )
                    for s in specs
                ],
            )

    # Thread-safe list for tracking child processes
    children: list[subprocess.Popen[str]] = []
    children_lock = threading.Lock()

    def _cleanup() -> None:
        """Terminate and reap all running children. SIGTERM -> wait -> SIGKILL."""
        with children_lock:
            for proc in children:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
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

    def _run_single(spec: ModelSpec, routing_result: RoutingResult) -> ReviewResult:
        """Run a single model review with pre-resolved routing."""
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

        route = routing_result.route
        if route is None:
            duration = time.monotonic() - start
            return ReviewResult(
                model_name=spec.effective_worker_id,
                stdout="",
                stderr="",
                success=False,
                duration_seconds=duration,
                error=f"No route resolved for '{spec.name}'",
            )

        if route.provider == "direct":
            try:
                extra_env.update(direct_model_env(route.model_ref))
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
            env = build_claude_env(direct=True, extra_vars=extra_env or None)
        else:
            env = build_claude_env(base_url=routing_result.base_url, extra_vars=extra_env or None)

        cmd = ["claude", "-p"]
        if can_use_bare(env):
            cmd.append("--bare")
        if resume_id:
            cmd.extend(["--resume", resume_id])

        model_flag = resolve_model_flag(route)
        if model_flag:
            cmd.extend(["--model", model_flag])

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
            future_to_item = {
                executor.submit(_run_single, spec, routing_plan.routes[idx]): (idx, spec)
                for idx, spec in enumerate(specs)
            }
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
