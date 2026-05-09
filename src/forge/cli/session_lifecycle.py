"""Session lifecycle commands: start, resume, fork, incognito.

Split from session.py for file-size compliance. All public and private
names are re-exported by session.py so that ``patch("forge.cli.session.XXX")``
continues to work.
"""

from __future__ import annotations

import shlex
import sys
import uuid as _uuid
from pathlib import Path
from typing import cast

import click

from forge.core.paths import display_path
from forge.core.state import now_iso
from forge.session import (
    LAUNCH_MODE_HOST,
    LAUNCH_MODE_SIDECAR,
    ForgeSessionError,
    SessionExistsError,
    SessionIndexEntry,
    SessionManager,
    SessionState,
    SessionStore,
)
from forge.session.claude import build_claude_args
from forge.session.direct_model import (
    apply_direct_model_env,
    resolve_direct_model_pin,
    token_estimate_multiplier_for_direct_model,
)
from forge.session.exceptions import (
    BranchExistsError,
    BranchInUseError,
    BranchNotMergedError,
    CannotForkIncognitoError,
    InvalidBranchNameError,
    SessionNotFoundError,
    WorktreePathExistsError,
)


# Names that tests patch on forge.cli.session (invoke_claude,
# run_with_active_session, SessionManager, generate_unique_name) must be
# accessed through the parent module at call time. We use _sess() to get
# the module from sys.modules (already loaded by the time any function runs).
def _sess():  # type: ignore[return]
    return sys.modules["forge.cli.session"]


from forge.cli.session import (  # noqa: E402
    ResolvedRouting,
    _apply_routing_override_to_state,
    _combine_prompt_files,
    _get_active_session_entry,
    _get_effective_proxy_for_session,
    _get_launch_preferences,
    _get_runtime_base_url,
    _handle_error,
    _hint_cross_project_session,
    _persist_routing_override,
    _print_routing_summary,
    _resolve_extension_detection_root,
    _resolve_launch_mode,
    _resolve_session_artifact_root,
    _resolve_worktree_extension_root,
    console,
    logger,
)
from forge.cli.session import session as _session_untyped  # noqa: E402

session = cast(click.Group, _session_untyped)  # type: ignore[has-type]  # circular re-export

# Functions below are accessed through _sess() because tests patch them
# on forge.cli.session. Direct imports would bypass those patches.
# _auto_install_extensions, _build_session_env, _cwd_forge_root,
# _detect_parent_extensions, _generate_parent_handoff_context,
# _prepare_sidecar_prompt_file, _resolve_context_limit

__all__ = [
    # Public functions
    "launch_new_session",
    # Click commands
    "start",
    "resume",
    "fork",
    "incognito",
    # Private helpers (needed for re-export to forge.cli.session namespace)
    "_launch_claude_for_session",
    "_launch_in_place",
    "_reconnect_in_place",
    "_launch_as_child",
    "_resume_fresh",
    "_resume_fresh_native",
    "_pick_session",
    "_print_context_path",
    "_print_post_exit_tip",
    "_resume_tip_command",
    "_print_branch_exists_tip",
    "_has_confirmed_claude_session",
    "_is_resumable_session",
    "_has_resumable_transcript",
    "_has_resumable_claude_session",
    "_get_deferred_same_dir_fork_resume_id",
    "_resolve_manifest_prompt_file",
    "_infer_launch_confirmation",
    "_persist_fork_handoff_derivation",
    "_warn_if_hooks_missing",
    "_warn_if_version_outdated",
]


def _has_confirmed_claude_session(state: SessionState) -> bool:
    """Whether this session has durable evidence of a resumable Claude conversation."""
    if not state.confirmed.claude_session_id:
        return False
    if state.confirmed.confirmed_by is not None:
        return True
    return _has_resumable_transcript(state)


def _is_resumable_session(state: SessionState) -> bool:
    """Whether this session has a resumable Claude conversation.

    Reconnect should allow the same fallback evidence as normal relaunch:
    either a hook-confirmed session or a transcript-backed session when the
    hook missed confirmation (for example, lock contention). Pre-seeded UUIDs
    without other evidence are still rejected.
    """
    return bool(state.confirmed.claude_session_id and _has_resumable_claude_session(state))


def _has_resumable_transcript(state: SessionState) -> bool:
    """Whether we can infer an existing Claude conversation from transcript state."""
    session_id = state.confirmed.claude_session_id
    if not session_id or state.confirmed.is_sandboxed:
        return False

    transcript_path = state.confirmed.transcript_path
    if transcript_path and Path(transcript_path).is_file():
        return True

    try:
        from forge.session.claude.paths import (
            get_transcript_path,
            resolve_claude_project_root,
        )

        # Check persisted launch root first, then computed root
        if state.confirmed.claude_project_root:
            if get_transcript_path(state.confirmed.claude_project_root, session_id).is_file():
                return True
        return get_transcript_path(resolve_claude_project_root(state), session_id).is_file()
    except Exception:
        return False


def _has_resumable_claude_session(state: SessionState) -> bool:
    """Whether Claude can be resumed for this session."""
    return _has_confirmed_claude_session(state) or _has_resumable_transcript(state)


def _get_deferred_same_dir_fork_resume_id(
    *,
    manager: SessionManager,
    manifest: SessionState,
) -> str | None:
    """Return the parent UUID when launching a never-started same-dir fork."""
    if not manifest.is_fork or not manifest.parent_session:
        return None

    if manifest.worktree and manifest.worktree.is_worktree:
        return None

    confirmed = manifest.confirmed
    if (
        confirmed.claude_session_id is not None
        or confirmed.transcript_path is not None
        or confirmed.confirmed_by is not None
    ):
        return None

    try:
        parent_state = manager.get_session(manifest.parent_session, forge_root=manifest.forge_root)
    except ForgeSessionError:
        return None

    return parent_state.confirmed.claude_session_id


def _warn_if_hooks_missing(project_path: Path) -> None:
    """Warn if no Forge hooks are installed before launching Claude.

    Args:
        project_path: Forge project root (where .claude/ lives). Use forge_root,
            not worktree/checkout root, so nested projects find the correct settings.
    """
    from forge.install.hooks import has_forge_hooks

    if has_forge_hooks(project_path):
        return

    console.print(
        "[yellow]Warning:[/yellow] Forge hooks are not installed. "
        "State tracking, policy enforcement, verification, and search indexing "
        "will not be active."
    )
    console.print("[dim]Tip: Run 'forge extension enable' to install hooks.[/dim]")


def _warn_if_version_outdated() -> None:
    """Warn if Claude Code version is below the minimum required by Forge."""
    from forge.install.version import check_minimum_version

    result = check_minimum_version()
    if result.ok or result.version is None:
        return  # Don't warn if we can't detect (hooks warning covers that)

    console.print(
        f"[yellow]Warning:[/yellow] Claude Code {result.version} is below "
        f"minimum {result.minimum}. Some features may not work correctly."
    )
    console.print("[dim]Tip: Run 'claude update' to upgrade.[/dim]")


def _infer_launch_confirmation(
    *,
    store: "SessionStore",
    manifest: SessionState,
    session_id: str | None,
) -> None:
    """Backfill transcript/runtime confirmation after a successful host launch."""
    if session_id is None or manifest.confirmed.is_sandboxed:
        return

    try:
        from forge.session.claude.paths import (
            get_transcript_path,
            resolve_claude_project_root,
        )
    except ImportError:
        return

    # Prefer persisted launch root; fall back to computed root
    if manifest.confirmed.claude_project_root:
        transcript_path = get_transcript_path(manifest.confirmed.claude_project_root, session_id)
    else:
        transcript_path = get_transcript_path(resolve_claude_project_root(manifest), session_id)
    if not transcript_path.is_file():
        return

    def _mutate(state: SessionState) -> None:
        # 1:1 model: overwrite UUID directly (no accumulation)
        state.confirmed.claude_session_id = session_id
        state.confirmed.transcript_path = str(transcript_path)
        state.confirmed.confirmed_at = now_iso()
        if state.confirmed.confirmed_by is None:
            state.confirmed.confirmed_by = "cli:launch:inferred"

    store.update(timeout_s=5.0, mutate=_mutate)


def _resolve_manifest_prompt_file(manifest: SessionState) -> Path | None:
    """Resolve a session's configured system prompt file, if any."""
    if manifest.intent.system_prompt is None or manifest.intent.system_prompt.file is None:
        return None
    prompt_path = Path(manifest.intent.system_prompt.file).expanduser()
    return prompt_path.resolve() if prompt_path.exists() else None


def _persist_fork_handoff_derivation(
    *,
    manifest: SessionState,
    strategy: str,
    context_path: Path | None,
) -> SessionState:
    """Persist handoff-specific derivation details for a worktree fork."""
    worktree_path = Path(manifest.worktree.path) if manifest.worktree else Path.cwd()
    forge_root = Path(manifest.forge_root) if manifest.forge_root else worktree_path

    context_file: str | None = None
    if context_path is not None:
        try:
            context_file = str(context_path.relative_to(worktree_path))
        except ValueError:
            context_file = str(context_path)

    def _mutate(m: SessionState) -> None:
        if m.confirmed.derivation is None:
            from forge.session.models import Derivation

            m.confirmed.derivation = Derivation(parent_session=m.parent_session or "")
        m.confirmed.derivation.resume_mode = "handoff"
        m.confirmed.derivation.strategy = strategy
        m.confirmed.derivation.context_file = context_file

    return SessionStore(str(forge_root), manifest.name).update(timeout_s=5.0, mutate=_mutate)


def _launch_claude_for_session(
    *,
    manifest: SessionState,
    session_id: str | None,
    resume_id: str | None,
    effective_template: str | None,
    runtime_base_url: str | None,
    context_limit: int,
    use_sidecar: bool,
    mounts: tuple[str, ...] = (),
    image: str | None = None,
    fork_session: bool = False,
    register_fork: bool = False,
    system_prompt_file: str | None = None,
    name: str | None = None,
    extra_args: list[str] | None = None,
    proxy_id: str | None = None,
) -> int:
    """Launch Claude for a session, handling sidecar/host split."""
    worktree_path = Path(manifest.worktree.path) if manifest.worktree else Path.cwd()
    # State lives under forge_root (may differ from worktree_path in nested projects)
    forge_root = Path(manifest.forge_root) if manifest.forge_root else worktree_path
    # Claude Code project root: where Claude finds .claude/ and stores conversations.
    # For nested projects this is forge_root; for root-level worktrees it's worktree_path.
    from forge.session.claude.paths import resolve_claude_project_root

    launch_root = Path(resolve_claude_project_root(manifest))

    # Prefer persisted launch root (set by SessionStart hook) over computed
    # root. This handles sessions created before the nested-project CWD fix
    # (7a1bbe9) where the conversation lives under the old checkout-root
    # namespace. The persisted value is authoritative; the computed root is
    # the fallback for sessions that predate the field.
    if manifest.confirmed.claude_project_root:
        launch_root = Path(manifest.confirmed.claude_project_root)

    register_fork_env = fork_session or register_fork
    fork_name = manifest.name if register_fork_env else None
    parent_session = manifest.parent_session if register_fork_env else None

    env_vars, unset_env_vars = _sess()._build_session_env(
        session_name=manifest.name,
        context_limit=context_limit,
        template=effective_template,
        base_url=runtime_base_url,
        fork_name=fork_name,
        parent_session=parent_session,
        forge_root=manifest.forge_root,
        subprocess_proxy=manifest.intent.subprocess_proxy,
    )

    _sess()._warn_if_hooks_missing(forge_root)
    _sess()._warn_if_version_outdated()

    from forge.session import SessionStore

    store = SessionStore(str(forge_root), manifest.name)

    # Persist launch root on first launch so reconnect can use the exact CWD
    if not manifest.confirmed.claude_project_root:
        _lr = str(launch_root)
        store.update(
            timeout_s=5.0,
            mutate=lambda m: setattr(m.confirmed, "claude_project_root", _lr),
        )

    if use_sidecar:
        if effective_template is None or runtime_base_url is None:
            console.print("[red]Error:[/red] Direct sessions are not supported with --sidecar")
            sys.exit(1)

        # Recover proxy_id from base_url when not explicitly provided (relaunch paths)
        if proxy_id is None and runtime_base_url is not None:
            try:
                from forge.proxy.proxies import ProxyRegistryStore as _PStore

                _entry = _PStore().find_by_base_url(runtime_base_url)
                if _entry is not None:
                    proxy_id = _entry.proxy_id
            except Exception:
                pass  # Best-effort; falls back to template scan

        from forge.sidecar import get_secrets_for_template, run_sidecar_session
        from forge.sidecar.container import ContainerExistsError, parse_mounts
        from forge.sidecar.docker import is_docker_available

        if not is_docker_available():
            console.print("[red]Error:[/red] Docker is not available or not running")
            sys.exit(1)

        store.update(timeout_s=5.0, mutate=lambda m: setattr(m.confirmed, "is_sandboxed", True))

        try:
            extra_mounts = parse_mounts(mounts) if mounts else []
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        claude_dir = launch_root / ".claude"
        forge_dir = launch_root / ".forge"
        sidecar_home = forge_dir / "sidecar-home"
        claude_dir.mkdir(parents=True, exist_ok=True)
        forge_dir.mkdir(parents=True, exist_ok=True)
        sidecar_home.mkdir(parents=True, exist_ok=True)
        sidecar_prompt_file, prompt_mounts = _sess()._prepare_sidecar_prompt_file(
            worktree_path=launch_root,
            system_prompt_file=system_prompt_file,
        )
        standard_mounts = [
            (str(claude_dir), "/workspace/.claude", "rw"),
            (str(forge_dir), "/workspace/.forge", "rw"),
            (str(sidecar_home), "/root/.claude", "rw"),
        ]
        all_mounts = standard_mounts + prompt_mounts + extra_mounts
        claude_args = build_claude_args(
            session_id=session_id,
            resume_id=resume_id,
            fork_session=fork_session,
            name=name,
            model=None,
            system_prompt_file=sidecar_prompt_file,
            extra_args=extra_args,
        )

        secrets = get_secrets_for_template(effective_template)
        container_env = {**env_vars, **secrets}

        if "LITELLM_BASE_URL" not in container_env:
            try:
                from forge.config.loader import load_proxy_instance_config
                from forge.proxy.proxies import ProxyRegistryStore as _Store
                from forge.proxy.proxies import resolve_proxy_optional

                _resolved_pid = proxy_id
                if not _resolved_pid and effective_template:
                    _registry = _Store().read()
                    _resolved = resolve_proxy_optional(_registry, effective_template)
                    if _resolved:
                        _resolved_pid = _resolved.proxy_id

                if _resolved_pid:
                    _pcfg = load_proxy_instance_config(_resolved_pid)
                    if _pcfg and _pcfg.upstream_base_url:
                        container_env["LITELLM_BASE_URL"] = _pcfg.upstream_base_url
            except Exception:
                pass  # Best-effort; user can export LITELLM_BASE_URL manually

        from forge.runtime_config import get_runtime_config

        sidecar_image = image or get_runtime_config().sidecar_image
        console.print("[cyan]Starting sidecar session in container[/cyan]")
        console.print(f"  Image: {sidecar_image}")
        console.print()

        try:
            return _sess().run_with_active_session(
                session_name=manifest.name,
                worktree_path=worktree_path,
                launch_mode=LAUNCH_MODE_SIDECAR,
                forge_root=manifest.forge_root,
                claude_session_id=session_id,
                runner=lambda: run_sidecar_session(
                    image=sidecar_image,
                    template=effective_template,
                    session_name=manifest.name,
                    project_dir=launch_root,
                    extra_mounts=all_mounts,
                    context_limit=context_limit,
                    env_vars=container_env,
                    claude_args=claude_args,
                ),
            )
        except ContainerExistsError as e:
            store.update(
                timeout_s=5.0,
                mutate=lambda m: setattr(m.confirmed, "is_sandboxed", False),
            )
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)
        except Exception:
            store.update(
                timeout_s=5.0,
                mutate=lambda m: setattr(m.confirmed, "is_sandboxed", False),
            )
            raise

    store.update(timeout_s=5.0, mutate=lambda m: setattr(m.confirmed, "is_sandboxed", False))

    # Best-effort: recover proxy_id from base_url for host launches (resume/reconnect
    # paths don't pass proxy_id explicitly). Falls back to no proxy_id, which means
    # model_alternatives won't apply on this launch.
    if proxy_id is None and runtime_base_url is not None:
        try:
            from forge.proxy.proxies import ProxyRegistryStore as _PRS

            _entry = _PRS().find_by_base_url(runtime_base_url)
            if _entry is not None:
                proxy_id = _entry.proxy_id
        except Exception:
            logger.debug("proxy_id recovery from base_url failed", exc_info=True)

    if runtime_base_url is None:
        # Direct mode: apply explicit --model or fall back to default_direct_model
        from forge.runtime_config import get_default_direct_model

        direct_model = manifest.intent.launch.direct_model if manifest.intent.launch else None
        direct_model = direct_model or get_default_direct_model()
        error = apply_direct_model_env(env_vars, direct_model)
        if error:
            console.print(f"[red]Error:[/red] {error}")
            return 1
    elif manifest.intent.launch and manifest.intent.launch.direct_model and proxy_id:
        # Proxy mode with explicit --model: apply model pin so Claude Code sends
        # the right model name in requests (proxy resolves via model_alternatives).
        # Only apply if the proxy actually configures alternatives for this model.
        from forge.config.loader import load_proxy_instance_config

        proxy_cfg = load_proxy_instance_config(proxy_id)
        if proxy_cfg and proxy_cfg.model_alternatives:
            dm = manifest.intent.launch.direct_model
            pin = resolve_direct_model_pin(dm)
            alt_models = proxy_cfg.model_alternatives.get(pin.tier, {})
            if pin.canonical_model in alt_models:
                error = apply_direct_model_env(env_vars, dm)
                if error:
                    console.print(f"[red]Error:[/red] {error}")
                    return 1

    exit_code = _sess().run_with_active_session(
        session_name=manifest.name,
        worktree_path=worktree_path,
        launch_mode=LAUNCH_MODE_HOST,
        forge_root=manifest.forge_root,
        claude_session_id=session_id,
        runner=lambda: _sess().invoke_claude(
            session_id=session_id,
            resume_id=resume_id,
            fork_session=fork_session,
            name=name,
            model=None,
            system_prompt_file=system_prompt_file,
            env_vars=env_vars,
            unset_env_vars=unset_env_vars,
            extra_args=extra_args,
            cwd=str(launch_root),
        ),
    )
    if exit_code == 0 and not fork_session:
        _sess()._infer_launch_confirmation(store=store, manifest=manifest, session_id=resume_id or session_id)

    _print_post_exit_tip(manifest)

    return exit_code


def _print_post_exit_tip(manifest: SessionState) -> None:
    """Print session tips after Claude exits.

    Printed from the parent launcher process (not a hook) because Claude
    Code suppresses SessionEnd hook output (anthropics/claude-code#9090).
    """
    if manifest.is_incognito or not manifest.name:
        return
    # Claude sometimes leaves the cursor mid-line on exit, so clear the
    # current line before printing the Forge-owned tip.
    try:
        console.file.write("\r\x1b[2K")
        console.file.flush()
    except Exception:
        logger.debug("Terminal line clear failed before post-exit tip", exc_info=True)
    resume_cmd = _resume_tip_command(manifest)
    console.print(f"\n[dim]Tip: Reconnect to this conversation with:[/dim]\n" f"[dim]  {resume_cmd}[/dim]")


def _resume_tip_command(manifest: SessionState) -> str:
    """Return the shell command to resume a session from the correct directory."""
    assert manifest.name  # callers guard on manifest.name first

    resume_cmd = f"forge session resume {shlex.quote(manifest.name)}"
    if not manifest.worktree or not manifest.worktree.is_worktree:
        return resume_cmd

    resume_root = manifest.forge_root
    if not resume_root:
        from forge.session.claude.paths import resolve_claude_project_root

        resume_root = resolve_claude_project_root(manifest)

    return f"cd {shlex.quote(display_path(resume_root))} && {resume_cmd}"


def _print_branch_exists_tip(e: BranchExistsError) -> None:
    """Print contextual tip for a branch that already exists."""
    console.print(f"[red]Error:[/red] {e}")
    if e.worktree:
        console.print("\n[dim]Tip: Use --branch to specify a different branch name.[/dim]")
    else:
        console.print(
            f"\n[dim]Tip: Delete with `git branch -d {e.branch}` or use --branch to specify a different name.[/dim]"
        )


def _resume_token_estimate_multiplier(
    *,
    parent_state: SessionState,
    effective_proxy_ref: str | None,
) -> float:
    """Return a model-specific heuristic multiplier for fresh full-resume checks."""
    if effective_proxy_ref is not None:
        # v1 only applies tokenizer safety margins to direct Claude pins. Avoid
        # proxy config I/O in the resume hot path until proxy-routed 4.7 needs it.
        return 1.0

    from forge.runtime_config import get_default_direct_model

    direct_model = parent_state.intent.launch.direct_model if parent_state.intent.launch else None
    direct_model = direct_model or get_default_direct_model()
    if not direct_model:
        return 1.0
    try:
        return token_estimate_multiplier_for_direct_model(direct_model)
    except ValueError:
        return 1.0


# --- Shared session creation + launch ---


def launch_new_session(
    *,
    name: str,
    template: str | None = None,
    base_url: str | None = None,
    direct: bool = False,
    incognito: bool = False,
    system_prompt: str | None = None,
    system_prompt_file: str | None = None,
    worktree: bool = False,
    branch: str | None = None,
    sidecar: bool = False,
    host_proxy: bool = False,
    mounts: tuple[str, ...] = (),
    image: str | None = None,
    no_launch: bool = False,
    extensions: bool | None = None,
    extra_args: list[str] | None = None,
    context_limit_override: int | None = None,
    proxy_display: str | None = None,
    proxy_id: str | None = None,
    supervise_target: str | None = None,
    supervisor_proxy: str | None = None,
    supervisor_direct: bool = False,
    subprocess_proxy: str | None = None,
    direct_model: str | None = None,
) -> int:
    """Create a new session and launch Claude.

    This is the shared implementation behind ``forge session start``,
    ``forge session incognito``, and ``forge claude start``.

    Returns the Claude exit code (0 on success).  Never calls ``sys.exit``
    so callers can wrap with cleanup (incognito) or other post-processing.
    """
    # --- flag validation ---
    if branch and not worktree:
        console.print("[red]Error:[/red] --branch requires --worktree")
        return 1
    if sidecar and host_proxy:
        console.print("[red]Error:[/red] --sidecar and --host-proxy are mutually exclusive")
        return 1
    if direct and (template or base_url):
        console.print("[red]Error:[/red] --no-proxy cannot be combined with --template or --base-url")
        return 1
    if direct and sidecar:
        console.print("[red]Error:[/red] --no-proxy cannot be combined with --sidecar")
        return 1
    if direct and host_proxy:
        console.print("[red]Error:[/red] --no-proxy cannot be combined with --host-proxy")
        return 1
    if direct_model and sidecar:
        console.print("[red]Error:[/red] --model cannot be combined with --sidecar")
        return 1
    if direct_model and host_proxy:
        console.print("[red]Error:[/red] --model cannot be combined with --host-proxy")
        return 1
    if incognito and no_launch:
        console.print("[red]Error:[/red] --incognito and --no-launch are mutually exclusive")
        return 1
    if no_launch and (system_prompt or system_prompt_file):
        console.print("[red]Error:[/red] --system-prompt is launch-only and lost with --no-launch")
        return 1

    launch_mode = LAUNCH_MODE_HOST if direct else _resolve_launch_mode(sidecar=sidecar, host_proxy=host_proxy)
    use_sidecar = launch_mode == LAUNCH_MODE_SIDECAR
    manager = _sess().SessionManager()

    normalized_direct_model: str | None = None
    direct_model_pin = None
    if direct_model:
        try:
            direct_model_pin = resolve_direct_model_pin(direct_model)
            normalized_direct_model = direct_model_pin.env_model
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            return 1

    # Validate --model against proxy model_alternatives when in proxy mode
    if direct_model_pin and proxy_id and not direct:
        from forge.config.loader import load_proxy_instance_config

        try:
            proxy_cfg = load_proxy_instance_config(proxy_id)
            if proxy_cfg is None:
                raise FileNotFoundError(proxy_id)
        except Exception:
            console.print(f"[red]Error:[/red] Could not load proxy config for '{proxy_id}'")
            return 1
        tier = direct_model_pin.tier
        # Strip [1m] suffix for alternative lookup (context pinning, not routing)
        lookup_model = direct_model_pin.canonical_model
        alt_models = proxy_cfg.model_alternatives.get(tier, {})
        if lookup_model not in alt_models:
            available = ", ".join(sorted(alt_models.keys())) if alt_models else "(none configured)"
            console.print(
                f"[red]Error:[/red] Proxy '{proxy_id}' does not configure model alternative "
                f"for '{lookup_model}' in tier '{tier}'. Available alternatives: {available}"
            )
            return 1

    # Resolve system prompt to absolute path BEFORE worktree creation
    # (worktree changes cwd so relative paths would break).
    prompt_file: str | None = None
    if system_prompt_file:
        prompt_file = str(Path(system_prompt_file).resolve())
    elif system_prompt:
        claude_dir = Path.cwd() / ".claude"
        claude_dir.mkdir(exist_ok=True)
        prompt_file_path = claude_dir / "forge.system-prompt.generated.md"
        prompt_file_path.write_text(system_prompt)
        prompt_file = str(prompt_file_path)

    # Validate supervisor target and proxy BEFORE creating the session to avoid half-created state
    _supervisor_source_state = None
    if supervise_target:
        from forge.guard.semantic.supervisor import validate_supervisor_target

        try:
            _supervisor_source_state = validate_supervisor_target(
                supervise_target, forge_root=_sess()._cwd_forge_root()
            )
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            return 1
    if supervisor_proxy:
        from forge.guard.semantic.supervisor import preflight_supervisor_proxy

        try:
            supervisor_proxy = preflight_supervisor_proxy(supervisor_proxy)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            return 1

    pre_seeded_uuid = str(_uuid.uuid4())
    try:
        manifest = manager.start_session(
            name=name,
            proxy_template=template,
            proxy_base_url=base_url,
            direct=direct,
            is_incognito=incognito,
            create_worktree=worktree,
            branch=branch,
            launch_mode=launch_mode,
            sidecar_mounts=list(mounts) if use_sidecar else None,
            sidecar_image=image if use_sidecar else None,
            direct_model=normalized_direct_model,
            claude_session_id=pre_seeded_uuid,
        )
    except SessionExistsError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print(f"\n[dim]Tip: Use 'forge session resume {name}' to continue,[/dim]")
        console.print(f"[dim]or 'forge session delete {name}' to remove it first.[/dim]")
        return 1
    except BranchExistsError as e:
        _print_branch_exists_tip(e)
        return 1
    except WorktreePathExistsError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: Remove the directory or use a different session name.[/dim]")
        return 1
    except InvalidBranchNameError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except ForgeSessionError as e:
        console.print(f"[red]Error:[/red] {e}", style="red")
        return 1
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}", style="red")
        return 1

    # --- set subprocess proxy (if requested) ---
    if subprocess_proxy:
        manifest.intent.subprocess_proxy = subprocess_proxy
        _sp_forge_root = manifest.forge_root or str(Path.cwd())
        from forge.session.store import SessionStore as _SPStore

        _SPStore(_sp_forge_root, manifest.name).update(
            timeout_s=5.0,
            mutate=lambda m: setattr(m.intent, "subprocess_proxy", subprocess_proxy),
        )
        manifest = _SPStore(_sp_forge_root, manifest.name).read()

    # --- wire supervisor (if requested) ---
    if supervise_target and _supervisor_source_state is not None:
        from forge.guard.semantic.supervisor import (
            apply_supervisor_routing,
            apply_supervisor_to_intent,
        )
        from forge.session.models import SupervisorConfig
        from forge.session.store import SessionStore

        _sup_forge_root = manifest.forge_root or (manifest.worktree.path if manifest.worktree else str(Path.cwd()))
        sup_config = SupervisorConfig(
            resume_id=supervise_target,
            forge_root=_supervisor_source_state.forge_root or _sup_forge_root,
        )
        apply_supervisor_routing(
            sup_config,
            _supervisor_source_state,
            supervisor_proxy=supervisor_proxy,
            supervisor_direct=supervisor_direct,
            current_proxy_id=proxy_id,
            current_template=template,
            current_direct=direct,
        )

        forge_root = _sup_forge_root
        store = SessionStore(forge_root, manifest.name)
        store.update(timeout_s=5.0, mutate=lambda m: apply_supervisor_to_intent(m, sup_config))
        manifest = store.read()

    # --- compute launch parameters ---
    effective_template = manifest.intent.proxy.template if manifest.intent.proxy else None
    effective_url = manifest.intent.proxy.base_url if manifest.intent.proxy else None

    context_limit = (
        context_limit_override
        if context_limit_override is not None
        else _sess()._resolve_context_limit(effective_template)
    )
    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=effective_url)

    # --- output ---
    label = "incognito session" if incognito else "session"
    console.print(f"Created {label} [green]{manifest.name}[/green]")
    if proxy_display:
        console.print(f"  Proxy: {proxy_display} ({effective_template}) @ {runtime_base_url}")
    else:
        _print_routing_summary(template=effective_template, base_url=runtime_base_url)
    if manifest.worktree and manifest.worktree.is_worktree:
        console.print(f"  Worktree: {display_path(manifest.worktree.path)}")
        console.print(f"  Branch:   {manifest.worktree.branch}")
    if supervise_target:
        console.print(f"  Supervisor: {supervise_target}")
    if incognito:
        console.print("[yellow]  (will auto-delete on exit)[/yellow]")

    # --- extensions ---
    if manifest.worktree and manifest.worktree.is_worktree:
        extension_root = _resolve_worktree_extension_root(manifest)
        if extension_root is not None:
            _sess()._auto_install_extensions(
                install_root=extension_root,
                parent_project_root=_resolve_extension_detection_root(Path.cwd()),
                force_extensions=extensions,
            )
    elif extensions is True:
        console.print("[dim]Tip: --extensions only applies with --worktree.[/dim]")
    console.print()

    # --- no-launch early exit ---
    if no_launch:
        console.print("[dim]Session created (--no-launch: Claude not started)[/dim]")
        return 0

    # --- launch Claude ---
    # Incognito cleanup wraps only the launch phase so that validation/creation
    # failures do NOT trigger deletion of a potentially pre-existing session.
    if incognito:
        exit_code = 0
        try:
            exit_code = _launch_claude_for_session(
                manifest=manifest,
                session_id=pre_seeded_uuid,
                resume_id=None,
                effective_template=effective_template,
                runtime_base_url=runtime_base_url,
                context_limit=context_limit,
                use_sidecar=use_sidecar,
                mounts=mounts,
                image=image,
                system_prompt_file=prompt_file,
                name=manifest.name,
                extra_args=extra_args,
                proxy_id=proxy_id,
            )
        finally:
            console.print(f"\n[dim]Cleaning up incognito session '{manifest.name}'...[/dim]")
            try:
                _sess().SessionManager().delete_session(
                    manifest.name,
                    delete_transcripts=True,
                    force=True,
                    forge_root=manifest.forge_root,
                )
                console.print("[green]Cleanup complete.[/green]")
            except ForgeSessionError as e:
                console.print(f"[yellow]Cleanup warning:[/yellow] {e}")
        return exit_code

    return _launch_claude_for_session(
        manifest=manifest,
        session_id=pre_seeded_uuid,
        resume_id=None,
        effective_template=effective_template,
        runtime_base_url=runtime_base_url,
        context_limit=context_limit,
        use_sidecar=use_sidecar,
        mounts=mounts,
        image=image,
        system_prompt_file=prompt_file,
        name=manifest.name,
        extra_args=extra_args,
        proxy_id=proxy_id,
    )


@session.command()
@click.argument("name", required=False)
@click.option(
    "--proxy",
    "proxy_name",
    type=str,
    default=None,
    help="Proxy to use (proxy_id or template name)",
)
@click.option("--no-proxy", "direct", is_flag=True, help="Bypass the proxy and talk to Anthropic directly")
@click.option("--direct", "direct_deprecated", is_flag=True, hidden=True, help="Deprecated alias for --no-proxy")
@click.option("--incognito", "-i", is_flag=True, help="Auto-delete session on exit")
@click.option("--system-prompt", "-s", help="Append system prompt text")
@click.option(
    "--system-prompt-file",
    "-S",
    type=click.Path(exists=True),
    help="Append system prompt from file",
)
@click.option("--worktree", "-w", is_flag=True, help="Create git worktree for session isolation")
@click.option("--branch", "-b", help="Override branch name (requires --worktree)")
@click.option(
    "--model",
    "direct_model",
    type=str,
    default=None,
    help="Pin the Claude model for direct sessions (for example: claude-opus-4-7 or claude-sonnet-4-6[1m])",
)
@click.option("--sidecar", is_flag=True, help="Run with bundled proxy in Docker container")
@click.option("--host-proxy", is_flag=True, help="Use host proxy (overrides config)")
@click.option("--mount", "mounts", multiple=True, help="Extra mounts (host:container[:ro|rw])")
@click.option("--image", default=None, help="Docker image for sidecar mode")
@click.option(
    "--no-launch",
    is_flag=True,
    help="Create session without launching Claude",
)
@click.option(
    "--extensions/--no-extensions",
    default=None,
    help="Auto-install extensions in worktree (default: inherit from parent)",
)
@click.option(
    "--supervise",
    "supervise_target",
    type=str,
    default=None,
    help="Session name to use as plan supervisor (enables policy enforcement)",
)
@click.option("--supervisor-proxy", type=str, default=None, help="Proxy for supervisor routing (requires --supervise)")
@click.option(
    "--no-supervisor-proxy",
    "supervisor_direct",
    is_flag=True,
    default=False,
    help="Force supervisor to use direct Anthropic routing (requires --supervise)",
)
@click.option(
    "--subprocess-proxy",
    "subprocess_proxy",
    type=str,
    default=None,
    help="Route subprocesses (supervisor, panel, handoff) through this proxy while main session is direct",
)
def start(
    name: str | None,
    proxy_name: str | None,
    direct: bool,
    incognito: bool,
    system_prompt: str | None,
    system_prompt_file: str | None,
    worktree: bool,
    branch: str | None,
    direct_model: str | None,
    sidecar: bool,
    host_proxy: bool,
    mounts: tuple[str, ...],
    image: str | None,
    no_launch: bool,
    extensions: bool | None,
    supervise_target: str | None,
    supervisor_proxy: str | None,
    supervisor_direct: bool,
    subprocess_proxy: str | None,
    direct_deprecated: bool,
) -> None:
    """Create and start a new session.

    With --worktree/-w, creates an isolated git worktree for the session.
    This enables parallel work without manifest conflicts.

    With --sidecar, runs Claude Code and proxy inside a Docker container
    with lifecycle coupling. The project directory is mounted at /workspace.

    For resuming existing sessions, use ``forge session resume``.

    \b
    Examples:
        forge session start                                      # Auto-named, no proxy
        forge session start my-feature                           # Named session, no proxy
        forge session start my-feature --proxy litellm-gemini    # With proxy routing
        forge session start my-feature --worktree                # Isolated worktree
        forge session start my-feature --supervise planner       # With plan supervision
    """
    direct = direct or direct_deprecated
    if direct and proxy_name:
        console.print("[red]Error:[/red] --no-proxy and --proxy are mutually exclusive")
        sys.exit(1)
    if supervisor_proxy and supervisor_direct:
        console.print("[red]Error:[/red] --supervisor-proxy and --no-supervisor-proxy are mutually exclusive")
        sys.exit(1)
    if (supervisor_proxy or supervisor_direct) and not supervise_target:
        console.print("[red]Error:[/red] --supervisor-proxy/--no-supervisor-proxy require --supervise")
        sys.exit(1)
    if subprocess_proxy and proxy_name:
        console.print(
            "[red]Error:[/red] --subprocess-proxy is for direct-mode sessions; use --proxy alone for full proxy routing"
        )
        sys.exit(1)

    # Default to direct mode when neither --proxy nor --no-proxy is given,
    # unless --sidecar or --host-proxy is specified (both imply proxy mode).
    if not proxy_name and not direct and not sidecar and not host_proxy:
        direct = True

    routing: ResolvedRouting | None = None
    if proxy_name:
        routing = _sess()._resolve_routing_from_cli(proxy_name=proxy_name, direct=False)

    # CWD validation: must be at repo root; --worktree requires main repo
    from forge.cli.guards import require_main_repo_root, require_repo_root

    if worktree:
        require_main_repo_root()
    else:
        require_repo_root()

    if name is None:
        _fr = _sess()._cwd_forge_root()
        existing = {n for n, _ in _sess().SessionManager().list_sessions(forge_root_filter=_fr)}
        name = _sess().generate_unique_name(existing)

    sys.exit(
        launch_new_session(
            name=name,
            template=routing.template if routing else None,
            base_url=routing.base_url if routing else None,
            direct=direct,
            incognito=incognito,
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            worktree=worktree,
            branch=branch,
            sidecar=sidecar,
            host_proxy=host_proxy,
            mounts=mounts,
            image=image,
            no_launch=no_launch,
            extensions=extensions,
            proxy_id=routing.proxy_id if routing else None,
            proxy_display=routing.proxy_id if routing else None,
            context_limit_override=routing.context_limit if routing else None,
            supervise_target=supervise_target,
            supervisor_proxy=supervisor_proxy,
            supervisor_direct=supervisor_direct,
            subprocess_proxy=subprocess_proxy,
            direct_model=direct_model,
        )
    )


@session.command()
@click.argument("name", required=False)
@click.option(
    "--proxy",
    "proxy_name",
    type=str,
    default=None,
    help="Proxy to use (proxy_id or template name)",
)
@click.option(
    "--no-proxy", "direct", is_flag=True, default=False, help="Bypass the proxy and talk to Anthropic directly"
)
@click.option("--direct", "direct_deprecated", is_flag=True, hidden=True, help="Deprecated alias for --no-proxy")
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    help="Start a fresh Claude conversation with context assembled from the session's history",
)
@click.option(
    "--child-name",
    "-n",
    "child_name",
    help="Name for the derived session (only with --fresh, auto-generated if not provided)",
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(["minimal", "structured", "full", "ai-curated"]),
    default="structured",
    help="Context assembly strategy (only with --fresh, default: structured)",
)
@click.option(
    "--depth",
    "-d",
    type=int,
    default=1,
    help="Lineage traversal depth (only with --fresh, 1=parent only)",
)
@click.option(
    "--resume-mode",
    "resume_mode",
    type=click.Choice(["native", "handoff"]),
    default=None,
    help="Context transfer: native (full conversation via --fork-session) or handoff (assembled summary). Default: handoff.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Bypass active-session guard (launches as new child)",
)
def resume(
    name: str | None,
    proxy_name: str | None,
    direct: bool,
    fresh: bool,
    child_name: str | None,
    strategy: str,
    depth: int,
    resume_mode: str | None,
    force: bool,
    direct_deprecated: bool,
) -> None:
    """Resume a session.

    By default, reattaches to the existing Claude conversation (Ctrl+C
    recovery). If the session was never launched, launches it in-place.

    Use --fresh to start a new Claude conversation with context assembled
    from the session's history. This is useful when context approaches
    limits and you want a clean slate with a summary of what happened.

    Use --fresh --resume-mode native to carry full conversation history
    via --fork-session (lossless but lost on /compact).

    \b
    Examples:
      forge session resume my-session                    # Reattach to conversation
      forge session resume my-session --fresh            # Fresh conversation with context
      forge session resume my-session --fresh -s full    # Full transcript in context
      forge session resume my-session --fresh --resume-mode native  # Full conversation history
      forge session resume my-session --proxy my-proxy   # Reattach with different routing
      forge session resume my-session --fresh --no-proxy # Fresh conversation, direct mode
    """
    direct = direct or direct_deprecated
    if direct and proxy_name:
        console.print("[red]Error:[/red] --no-proxy and --proxy are mutually exclusive")
        sys.exit(1)

    if resume_mode and not fresh:
        console.print("[red]Error:[/red] --resume-mode requires --fresh")
        sys.exit(1)

    if not fresh and child_name:
        console.print("[red]Error:[/red] --child-name requires --fresh")
        sys.exit(1)

    routing: ResolvedRouting | None = None
    if proxy_name:
        routing = _sess()._resolve_routing_from_cli(proxy_name=proxy_name, direct=False)

    manager = _sess().SessionManager()

    if name is None:
        sessions = manager.list_sessions(include_incognito=True)
        if not sessions:
            console.print("[dim]No sessions to resume.[/dim]")
            console.print("\n[dim]Tip: Run 'forge session start <name>'.[/dim]")
            return

        name = _pick_session(sessions, manager, prompt="Select session to resume")
        if name is None:
            console.print("[dim]Cancelled[/dim]")
            sys.exit(0)

    _fr = _sess()._cwd_forge_root()
    try:
        manifest = manager.get_session(name, forge_root=_fr)
    except SessionNotFoundError:
        if not _hint_cross_project_session(name, _fr):
            console.print(f"[red]Error:[/red] session '{name}' not found")
        sys.exit(1)
    except ForgeSessionError as e:
        _handle_error(e)
        return

    if fresh:
        effective_resume_mode = resume_mode or "handoff"

        # Warn about handoff-only flags with native mode
        if effective_resume_mode == "native":
            ctx = click.get_current_context()
            if ctx.get_parameter_source("strategy") == click.core.ParameterSource.COMMANDLINE:
                console.print("[dim]Tip: --strategy is ignored with --resume-mode native.[/dim]")
            if ctx.get_parameter_source("depth") == click.core.ParameterSource.COMMANDLINE:
                console.print("[dim]Tip: --depth is ignored with --resume-mode native.[/dim]")

        if effective_resume_mode == "native":
            # Native requires a hook-confirmed session (UUID + confirmed_by/transcript evidence).
            # A pre-seeded UUID alone is not enough — there must be a real conversation to resume.
            if not _is_resumable_session(manifest):
                console.print(
                    "[red]Error:[/red] --resume-mode native requires a parent with a confirmed "
                    "Claude session (hook-confirmed or transcript-backed). "
                    "Use --resume-mode handoff for transcript-artifact-based resume."
                )
                sys.exit(1)
            _resume_fresh_native(
                manager=manager,
                parent=name,
                parent_state=manifest,
                child_name=child_name,
                routing=routing,
                direct=direct,
            )
        else:
            _resume_fresh(
                manager=manager,
                parent=name,
                parent_state=manifest,
                child_name=child_name,
                strategy=strategy,
                depth=depth,
                routing=routing,
                direct=direct,
            )
    elif not _has_confirmed_claude_session(manifest):
        _launch_in_place(
            manager=manager,
            name=name,
            manifest=manifest,
            routing=routing,
            direct=direct,
        )
    elif _is_resumable_session(manifest):
        active_entry = _get_active_session_entry(name, forge_root=manifest.forge_root)
        if active_entry is not None and not force:
            console.print(
                f"[red]Error:[/red] Cannot reconnect: session [bold]{name}[/bold] appears to still be active."
            )
            console.print(f"  Launch mode: {active_entry.launch_mode}")
            if active_entry.launcher_pid is not None:
                console.print(f"  Launcher PID: {active_entry.launcher_pid}")
            if active_entry.container_name:
                console.print(f"  Container: {active_entry.container_name}")
            console.print(
                "[dim]Tip: Reconnect is only available after the previous launch has exited."
                " Return to that launch if it is still running, or stop it cleanly and retry.[/dim]"
            )
            sys.exit(1)
        elif active_entry is not None and force:
            console.print(
                f"[yellow]Warning:[/yellow] Session [bold]{name}[/bold] appears active "
                f"(PID {active_entry.launcher_pid}). Launching as new child (--force)."
            )
            _launch_as_child(
                manager=manager,
                parent_name=name,
                parent=manifest,
                routing=routing,
                direct=direct,
            )
        else:
            _reconnect_in_place(
                manager=manager,
                name=name,
                manifest=manifest,
                routing=routing,
                direct=direct,
            )
    else:
        _launch_as_child(
            manager=manager,
            parent_name=name,
            parent=manifest,
            routing=routing,
            direct=direct,
        )


def _launch_in_place(
    *,
    manager: SessionManager,
    name: str,
    manifest: SessionState,
    routing: ResolvedRouting | None = None,
    direct: bool = False,
) -> None:
    """Launch a never-used session in-place (satisfies 1:1)."""
    manager.switch_session(name, forge_root=manifest.forge_root)

    worktree_path = Path(manifest.worktree.path) if manifest.worktree else Path.cwd()
    _apply_routing_override_to_state(state=manifest, routing=routing, direct=direct)
    _persist_routing_override(
        forge_root=Path(manifest.forge_root) if manifest.forge_root else worktree_path,
        session_name=manifest.name,
        routing=routing,
        direct=direct,
    )

    effective_template, effective_url, effective_proxy_id = _get_effective_proxy_for_session(manifest)
    context_limit = _sess()._resolve_context_limit(effective_proxy_id or effective_template)
    use_sidecar, mounts, image = _get_launch_preferences(manifest)
    prompt_files: list[Path] = []

    configured_prompt = _resolve_manifest_prompt_file(manifest)
    if configured_prompt is not None:
        prompt_files.append(configured_prompt)

    # Check for deferred same-dir fork (never-started fork should resume parent)
    fork_session = False
    resume_id: str | None = None
    session_id: str | None = None
    prompt_warnings: list[str] = []
    parent_resume_id = _get_deferred_same_dir_fork_resume_id(manager=manager, manifest=manifest)
    if parent_resume_id is not None:
        resume_id = parent_resume_id
        fork_session = True
        launch_action = "Fork parent Claude conversation"
    else:
        session_id = str(_uuid.uuid4())
        fork_context, prompt_warnings = _sess()._generate_parent_handoff_context(manager=manager, manifest=manifest)
        if fork_context is not None:
            prompt_files.append(fork_context)
            launch_action = "Start fresh Claude session with parent context"
        else:
            launch_action = "Start fresh Claude session"

    # Write pre-seeded UUID to manifest + index (after worktree_path is resolved)
    forge_root_path = Path(manifest.forge_root) if manifest.forge_root else worktree_path
    if session_id is not None:
        try:
            from forge.session import SessionStore

            store = SessionStore(str(forge_root_path), manifest.name)
            store.update(
                timeout_s=5.0,
                mutate=lambda m: setattr(m.confirmed, "claude_session_id", session_id),
            )
            manager.index_store.sync_uuid_from_state(manifest.name, store.read())
        except Exception:
            logger.debug("Pre-seed UUID write failed (hook will reconcile)", exc_info=True)
    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=effective_url)
    prompt_file = _combine_prompt_files(
        worktree_path=worktree_path,
        session_name=manifest.name,
        prompt_files=prompt_files,
    )

    console.print(f"Launching session [green]{manifest.name}[/green]")
    _print_routing_summary(template=effective_template, base_url=runtime_base_url)
    console.print(f"  Action:   {launch_action}")
    if manifest.worktree and manifest.worktree.is_worktree:
        console.print(f"  Worktree: {display_path(worktree_path)}")
        console.print(f"  Branch:   {manifest.worktree.branch}")
    if prompt_file:
        _print_context_path(prompt_file, worktree_path)
    for w in prompt_warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")
    console.print()

    exit_code = _launch_claude_for_session(
        manifest=manifest,
        session_id=session_id,
        resume_id=resume_id,
        effective_template=effective_template,
        runtime_base_url=runtime_base_url,
        context_limit=context_limit,
        use_sidecar=use_sidecar,
        mounts=mounts,
        image=image,
        fork_session=fork_session,
        system_prompt_file=prompt_file,
        name=manifest.name,
    )
    sys.exit(exit_code)


def _reconnect_in_place(
    *,
    manager: SessionManager,
    name: str,
    manifest: SessionState,
    routing: ResolvedRouting | None = None,
    direct: bool = False,
) -> None:
    """Reconnect to the same Claude conversation without creating a child.

    Advanced escape hatch for resuming in-place after the previous launch has
    fully ended. Relaxes the 1:1 invariant (new process invocation on the same
    Forge session) but is gated: a resumable conversation must exist.

    The caller is responsible for the active-session check (see resume()
    dispatch) -- this function assumes the session is not active.
    """
    if not _is_resumable_session(manifest):
        console.print("[red]Error:[/red] Cannot reconnect: no resumable Claude conversation was found.")
        console.print(
            f"[dim]Tip: Use 'forge session resume {name}' to reattach, or --fresh to start a new conversation.[/dim]"
        )
        sys.exit(1)

    claude_session_id = manifest.confirmed.claude_session_id
    assert claude_session_id is not None  # _is_resumable_session guarantees this

    manager.switch_session(name, forge_root=manifest.forge_root)

    worktree_path = Path(manifest.worktree.path) if manifest.worktree else Path.cwd()
    _apply_routing_override_to_state(state=manifest, routing=routing, direct=direct)
    _persist_routing_override(
        forge_root=Path(manifest.forge_root) if manifest.forge_root else worktree_path,
        session_name=manifest.name,
        routing=routing,
        direct=direct,
    )

    effective_template, effective_url, effective_proxy_id = _get_effective_proxy_for_session(manifest)
    context_limit = _sess()._resolve_context_limit(effective_proxy_id or effective_template)
    use_sidecar, mounts, image = _get_launch_preferences(manifest)
    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=effective_url)

    console.print(f"Reconnecting to session [green]{name}[/green]")
    _print_routing_summary(template=effective_template, base_url=runtime_base_url)
    console.print("  Action:   Reconnect to existing Claude conversation")
    console.print(f"  UUID:     {claude_session_id[:8]}...")
    if manifest.worktree and manifest.worktree.is_worktree:
        console.print(f"  Worktree: {display_path(worktree_path)}")
        console.print(f"  Branch:   {manifest.worktree.branch}")
    console.print()

    exit_code = _launch_claude_for_session(
        manifest=manifest,
        session_id=None,
        resume_id=claude_session_id,
        effective_template=effective_template,
        runtime_base_url=runtime_base_url,
        context_limit=context_limit,
        use_sidecar=use_sidecar,
        mounts=mounts,
        image=image,
        fork_session=False,
        name=manifest.name,
    )
    sys.exit(exit_code)


def _launch_as_child(
    *,
    manager: SessionManager,
    parent_name: str,
    parent: SessionState,
    routing: ResolvedRouting | None = None,
    direct: bool = False,
) -> None:
    """Create a child session and resume the parent's Claude conversation.

    Routes through _launch_claude_for_session() so sidecar sessions relaunch
    through the sidecar path with stored mounts/image settings.
    """
    try:
        parent, child = manager.relaunch_session(parent_name, forge_root=parent.forge_root)
    except ForgeSessionError as e:
        _handle_error(e)
        return

    worktree_path = Path(child.worktree.path) if child.worktree else Path.cwd()
    _apply_routing_override_to_state(state=child, routing=routing, direct=direct)
    _persist_routing_override(
        forge_root=Path(child.forge_root) if child.forge_root else worktree_path,
        session_name=child.name,
        routing=routing,
        direct=direct,
    )

    effective_template, effective_url, effective_proxy_id = _get_effective_proxy_for_session(child)
    context_limit = _sess()._resolve_context_limit(effective_proxy_id or effective_template)
    use_sidecar, mounts, image = _get_launch_preferences(child)

    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=effective_url)

    console.print(f"Relaunching [green]{parent_name}[/green] as [green]{child.name}[/green]")
    _print_routing_summary(template=effective_template, base_url=runtime_base_url)
    console.print("  Action:   Resume parent conversation in new session")
    console.print(f"  Parent:   {parent_name}")
    if child.worktree and child.worktree.is_worktree:
        console.print(f"  Worktree: {display_path(worktree_path)}")
        console.print(f"  Branch:   {child.worktree.branch}")
    console.print()

    # Child is a same-dir fork: use --resume --fork-session with parent's UUID
    exit_code = _launch_claude_for_session(
        manifest=child,
        session_id=None,
        resume_id=parent.confirmed.claude_session_id,
        effective_template=effective_template,
        runtime_base_url=runtime_base_url,
        context_limit=context_limit,
        use_sidecar=use_sidecar,
        mounts=mounts,
        image=image,
        fork_session=True,
        name=child.name,
        proxy_id=effective_proxy_id,
    )
    sys.exit(exit_code)


def _print_context_path(prompt_file: str, worktree_path: Path) -> None:
    """Print context file path, relative if possible."""
    prompt_path = Path(prompt_file)
    try:
        console.print(f"  Context:  {prompt_path.relative_to(worktree_path)}")
    except ValueError:
        console.print(f"  Context:  {display_path(prompt_path)}")


def _pick_session(
    sessions: list[tuple[str, SessionIndexEntry]],
    manager: SessionManager,
    prompt: str = "Select a session",
) -> str | None:
    """Interactive session picker using Rich.

    Args:
        sessions: List of (name, entry) tuples.
        manager: SessionManager for looking up manifest details.
        prompt: Prompt text to display.

    Returns:
        Selected session name, or None if cancelled.
    """
    from rich.table import Table

    from forge.cli.session import _format_relative_time

    if not sessions:
        return None

    console.print(f"\n[bold]{prompt}:[/bold]\n")

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("#", justify="right", width=3)
    table.add_column("NAME")
    table.add_column("TEMPLATE")
    table.add_column("LAST USED")

    for i, (session_name, entry) in enumerate(sessions, 1):
        proxy_template = "direct"
        try:
            manifest = manager.get_session(session_name, forge_root=entry.forge_root)
            if manifest.intent.proxy:
                proxy_template = manifest.intent.proxy.template
        except ForgeSessionError:
            pass

        last_used = _format_relative_time(entry.last_accessed_at)

        table.add_row(str(i), session_name, proxy_template, last_used)

    console.print(table)
    console.print()

    try:
        choice = click.prompt("Enter number (or 'q' to cancel)", default="1")
        if choice.lower() in ("q", "quit", "cancel"):
            return None

        choice_int = int(choice)
        if choice_int < 1 or choice_int > len(sessions):
            console.print("[red]Invalid choice[/red]")
            return None

        return sessions[choice_int - 1][0]
    except (ValueError, click.Abort):
        return None


def _resume_fresh(
    *,
    manager: SessionManager,
    parent: str,
    parent_state: SessionState,
    child_name: str | None,
    strategy: str,
    depth: int,
    routing: ResolvedRouting | None,
    direct: bool,
) -> None:
    """Create a fresh child session with context assembled from parent.

    This is the --fresh path of ``forge session resume``. Creates a new
    derived session with a context summary, then launches Claude fresh.
    """
    # Routing for context limit: --proxy/--no-proxy override > parent's effective routing.
    if routing:
        effective_proxy_ref = routing.proxy_id
    elif direct:
        effective_proxy_ref = None
    else:
        effective_template, _, effective_proxy_id = _get_effective_proxy_for_session(parent_state)
        effective_proxy_ref = effective_proxy_id or effective_template

    context_limit = _sess()._resolve_context_limit(effective_proxy_ref)
    token_multiplier = _resume_token_estimate_multiplier(
        parent_state=parent_state,
        effective_proxy_ref=effective_proxy_ref,
    )

    try:
        child_manifest, handoff_result = manager.resume_session(
            parent,
            child_name=child_name,
            strategy=strategy,
            depth=depth,
            context_limit=context_limit,
            token_estimate_multiplier=token_multiplier,
            forge_root=parent_state.forge_root,
        )
    except ForgeSessionError as e:
        _handle_error(e)
        return

    child_worktree_path = Path(child_manifest.worktree.path) if child_manifest.worktree else Path.cwd()
    _persist_routing_override(
        forge_root=Path(child_manifest.forge_root) if child_manifest.forge_root else child_worktree_path,
        session_name=child_manifest.name,
        routing=routing,
        direct=direct,
    )
    _apply_routing_override_to_state(state=child_manifest, routing=routing, direct=direct)

    console.print(f"[dim]Context assembled: {handoff_result.context_file_rel}[/dim]")
    if handoff_result.warnings:
        for warning in handoff_result.warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")
    console.print()

    console.print(f"Created derived session [green]{child_manifest.name}[/green] from [cyan]{parent}[/cyan]")
    console.print(f"[dim]Strategy: {strategy}, Depth: {depth}[/dim]")
    console.print()

    # Launch Claude as a NEW session (not resuming parent's conversation)
    child_worktree = Path(child_manifest.worktree.path) if child_manifest.worktree else Path.cwd()
    prompt_files: list[Path] = []
    configured_prompt = _resolve_manifest_prompt_file(child_manifest)
    if configured_prompt is not None:
        prompt_files.append(configured_prompt)
    if handoff_result.context_file is not None:
        prompt_files.append(handoff_result.context_file.resolve())
    prompt_file = _combine_prompt_files(
        worktree_path=child_worktree,
        session_name=child_manifest.name,
        prompt_files=prompt_files,
    )

    launch_template, launch_base_url, launch_proxy_id = _get_effective_proxy_for_session(child_manifest)

    use_sidecar, mounts, image = _get_launch_preferences(child_manifest)
    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=launch_base_url)

    pre_seeded_uuid = str(_uuid.uuid4())
    try:
        from forge.session import SessionStore

        _store_root = Path(child_manifest.forge_root) if child_manifest.forge_root else child_worktree_path
        _store = SessionStore(str(_store_root), child_manifest.name)
        _store.update(
            timeout_s=5.0,
            mutate=lambda m: setattr(m.confirmed, "claude_session_id", pre_seeded_uuid),
        )
        manager.index_store.sync_uuid_from_state(child_manifest.name, _store.read())
    except Exception:
        logger.debug("Pre-seed UUID write failed (hook will reconcile)", exc_info=True)

    _print_routing_summary(template=launch_template, base_url=runtime_base_url)
    console.print()

    exit_code = _launch_claude_for_session(
        manifest=child_manifest,
        session_id=pre_seeded_uuid,
        resume_id=None,
        effective_template=launch_template,
        runtime_base_url=runtime_base_url,
        context_limit=context_limit,
        use_sidecar=use_sidecar,
        mounts=mounts,
        image=image,
        fork_session=False,
        system_prompt_file=prompt_file,
        name=child_manifest.name,
        proxy_id=launch_proxy_id,
    )

    sys.exit(exit_code)


def _resume_fresh_native(
    *,
    manager: SessionManager,
    parent: str,
    parent_state: SessionState,
    child_name: str | None,
    routing: ResolvedRouting | None,
    direct: bool,
) -> None:
    """Create a child session with native conversation resume.

    Uses --resume --fork-session to carry full conversation history into a new
    Forge session. No context assembly or system_prompt_file generation.

    Requires the parent to have a confirmed claude_session_id (caller validates).
    """
    # Routing for context limit: --proxy/--no-proxy override > parent's effective routing.
    if routing:
        effective_proxy_ref = routing.proxy_id
    elif direct:
        effective_proxy_ref = None
    else:
        effective_template, _, effective_proxy_id = _get_effective_proxy_for_session(parent_state)
        effective_proxy_ref = effective_proxy_id or effective_template

    context_limit = _sess()._resolve_context_limit(effective_proxy_ref)

    try:
        child_manifest, _handoff = manager.resume_session(
            parent,
            child_name=child_name,
            resume_mode="native",
            forge_root=parent_state.forge_root,
        )
    except ForgeSessionError as e:
        _handle_error(e)
        return

    child_worktree_path = Path(child_manifest.worktree.path) if child_manifest.worktree else Path.cwd()
    _persist_routing_override(
        forge_root=Path(child_manifest.forge_root) if child_manifest.forge_root else child_worktree_path,
        session_name=child_manifest.name,
        routing=routing,
        direct=direct,
    )
    _apply_routing_override_to_state(state=child_manifest, routing=routing, direct=direct)

    parent_uuid = parent_state.confirmed.claude_session_id
    assert parent_uuid is not None  # caller validated

    console.print(f"Created derived session [green]{child_manifest.name}[/green] from [cyan]{parent}[/cyan]")
    console.print("[dim]Mode: Native resume (full conversation history via --fork-session)[/dim]")
    console.print()

    launch_template, launch_base_url, launch_proxy_id = _get_effective_proxy_for_session(child_manifest)
    use_sidecar, mounts, image = _get_launch_preferences(child_manifest)
    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=launch_base_url)

    _print_routing_summary(template=launch_template, base_url=runtime_base_url)
    console.print()

    exit_code = _launch_claude_for_session(
        manifest=child_manifest,
        session_id=None,
        resume_id=parent_uuid,
        effective_template=launch_template,
        runtime_base_url=runtime_base_url,
        context_limit=context_limit,
        use_sidecar=use_sidecar,
        mounts=mounts,
        image=image,
        fork_session=True,
        name=child_manifest.name,
        proxy_id=launch_proxy_id,
    )

    sys.exit(exit_code)


@session.command()
@click.argument("parent")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Name for the fork (auto-generated if not provided)",
)
@click.option(
    "--proxy",
    "proxy_name",
    type=str,
    default=None,
    help="Proxy to use (proxy_id or template name)",
)
@click.option("--no-proxy", "direct", is_flag=True, help="Bypass the proxy and talk to Anthropic directly")
@click.option("--direct", "direct_deprecated", is_flag=True, hidden=True, help="Deprecated alias for --no-proxy")
@click.option("--incognito", "-i", is_flag=True, help="Auto-delete fork on exit")
@click.option("--worktree", "-w", is_flag=True, help="Create git worktree for fork isolation")
@click.option("--branch", "-b", help="Override branch name (implies --worktree)")
@click.option("--no-launch", is_flag=True, help="Create fork without launching Claude")
@click.option(
    "--extensions/--no-extensions",
    default=None,
    help="Auto-install extensions in worktree (default: inherit from parent)",
)
@click.option(
    "--strategy",
    type=click.Choice(["minimal", "structured", "full", "ai-curated"]),
    default="structured",
    help="Context assembly strategy for worktree forks (default: structured)",
)
@click.option(
    "--inline-plan",
    is_flag=True,
    default=False,
    help="Inline the approved plan content in handoff context",
)
@click.option(
    "--into",
    "into_path",
    type=click.Path(exists=True),
    default=None,
    help="Fork into an existing non-main worktree directory",
)
@click.option(
    "--supervise",
    "supervise_target",
    is_flag=True,
    default=False,
    help="Set parent as plan supervisor for the fork (enables policy enforcement)",
)
@click.option("--supervisor-proxy", type=str, default=None, help="Proxy for supervisor routing (requires --supervise)")
@click.option(
    "--no-supervisor-proxy",
    "supervisor_direct",
    is_flag=True,
    default=False,
    help="Force supervisor to use direct Anthropic routing (requires --supervise)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Replace existing branch/worktree and skip budget preflight",
)
def fork(
    parent: str,
    name: str | None,
    proxy_name: str | None,
    direct: bool,
    direct_deprecated: bool,
    incognito: bool,
    worktree: bool,
    branch: str | None,
    no_launch: bool,
    extensions: bool | None,
    strategy: str,
    inline_plan: bool,
    into_path: str | None,
    supervise_target: bool,
    supervisor_proxy: str | None,
    supervisor_direct: bool,
    force: bool,
) -> None:
    """Fork an existing session.

    By default the fork shares the parent's directory so Claude's
    conversation carries over via --fork-session.  Use --worktree for
    code isolation in a separate git worktree, or --into for an existing
    non-main worktree.

    Use --no-proxy to bypass the proxy, or --proxy to route through
    a specific proxy instead of the parent's.

    \b
    Examples:
        forge session fork parent-session                      # Fork, same directory
        forge session fork parent-session --worktree           # Fork with worktree
        forge session fork parent-session -n child-session     # Custom fork name
        forge session fork parent-session --no-proxy           # Fork, bypass proxy
    """
    direct = direct or direct_deprecated
    if direct and proxy_name:
        console.print("[red]Error:[/red] --no-proxy and --proxy are mutually exclusive")
        sys.exit(1)
    if supervisor_proxy and supervisor_direct:
        console.print("[red]Error:[/red] --supervisor-proxy and --no-supervisor-proxy are mutually exclusive")
        sys.exit(1)
    if (supervisor_proxy or supervisor_direct) and not supervise_target:
        console.print("[red]Error:[/red] --supervisor-proxy/--no-supervisor-proxy require --supervise")
        sys.exit(1)

    if branch:
        worktree = True

    # --into validation
    into_resolved: str | None = None
    into_branch: str | None = None
    into_target_common: str | None = None
    if into_path is not None:
        if worktree:
            console.print("[red]Error:[/red] --into and --worktree are mutually exclusive")
            sys.exit(1)
        if branch:
            console.print("[red]Error:[/red] --into and --branch are mutually exclusive")
            sys.exit(1)

        import subprocess as _sp

        try:
            into_resolved = _sp.run(
                ["git", "-C", into_path, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except _sp.CalledProcessError:
            console.print(f"[red]Error:[/red] '{display_path(into_path)}' is not inside a git repository")
            sys.exit(1)

        # Resolve git-common-dir for the target (absolute, to avoid .git relative path bug)
        try:
            target_common_raw = _sp.run(
                ["git", "-C", into_resolved, "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # git returns relative paths from the checkout root; resolve against it
            target_common = str((Path(into_resolved) / target_common_raw).resolve())
        except _sp.CalledProcessError:
            console.print("[red]Error:[/red] Failed to resolve git repository for --into target")
            sys.exit(1)

        # Store for deferred comparison after parent session is loaded
        into_target_common = target_common

        # Reject main checkout: the main checkout's --show-toplevel == its own path
        # A real worktree has a different toplevel than the main repo
        try:
            # Use git-common-dir to find the main repo's toplevel
            main_git_dir = _sp.run(
                ["git", "-C", into_resolved, "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            main_git_dir_abs = (Path(into_resolved) / main_git_dir).resolve()
            # Main repo root is the parent of the .git directory
            main_repo_root = main_git_dir_abs.parent if main_git_dir_abs.name == ".git" else main_git_dir_abs
            if Path(into_resolved).resolve() == main_repo_root:
                console.print(
                    "[red]Error:[/red] --into targets existing worktrees, not the main checkout. "
                    "Use a same-directory fork instead."
                )
                sys.exit(1)
        except _sp.CalledProcessError:
            pass  # Can't determine; allow

        try:
            into_branch = _sp.run(
                ["git", "-C", into_resolved, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except _sp.CalledProcessError:
            into_branch = None

    # CWD validation (skip for --into, which has its own path resolution)
    if into_path is None:
        from forge.cli.guards import require_main_repo_root, require_repo_root

        if worktree:
            require_main_repo_root()
        else:
            require_repo_root()

    ctx = click.get_current_context()
    _strategy_explicit = ctx.get_parameter_source("strategy") == click.core.ParameterSource.COMMANDLINE
    _inline_plan_explicit = ctx.get_parameter_source("inline_plan") == click.core.ParameterSource.COMMANDLINE

    manager = _sess().SessionManager()
    _fr = _sess()._cwd_forge_root()

    # --into cross-repo preflight: reject before fork_session() to avoid orphaned sessions
    if into_resolved is not None and into_target_common is not None:
        import subprocess as _sp2

        try:
            parent_state_pre = manager.get_session(parent, forge_root=_fr)
            parent_wt_pre = parent_state_pre.worktree.path if parent_state_pre.worktree else None
            if parent_wt_pre:
                parent_common_raw = _sp2.run(
                    ["git", "-C", parent_wt_pre, "rev-parse", "--git-common-dir"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                parent_common = str((Path(parent_wt_pre) / parent_common_raw).resolve())
                if into_target_common != parent_common:
                    console.print(
                        "[red]Error:[/red] --into target is not part of the same repository as the parent session"
                    )
                    sys.exit(1)
        except _sp2.CalledProcessError:
            pass  # Can't resolve parent repo; allow
        except ForgeSessionError:
            pass  # Parent not found; fork_session() will raise the right error

    # Budget preflight for --strategy full (before fork_session to avoid orphaned sessions/worktrees)
    # Use the child's effective routing: --no-proxy means no proxy, --proxy overrides parent
    is_cross_dir = worktree or into_resolved is not None
    # Resolve --proxy early for preflight (reuses routing resolved later for launch)
    _preflight_routing: ResolvedRouting | None = None
    if proxy_name:
        _preflight_routing = _sess()._resolve_routing_from_cli(proxy_name=proxy_name, direct=False)
    if is_cross_dir and strategy == "full" and not direct:
        try:
            from forge.session.artifacts import resolve_artifact_path

            parent_state = manager.get_session(parent, forge_root=_fr)
            # --proxy override > parent's proxy for budget check
            if _preflight_routing:
                preflight_ref = _preflight_routing.proxy_id
            else:
                child_template = parent_state.intent.proxy.template if parent_state.intent.proxy else None
                preflight_ref = child_template
            context_limit_preflight = _sess()._resolve_context_limit(preflight_ref)
            if context_limit_preflight is not None:
                from forge.session.handoff import estimate_transcript_tokens

                artifact_root = _resolve_session_artifact_root(manager=manager, state=parent_state)
                transcripts = parent_state.confirmed.artifacts.get("transcripts", [])
                if transcripts and isinstance(transcripts, list):
                    latest = transcripts[-1]
                    if isinstance(latest, dict):
                        copied_path = latest.get("copied_path")
                        if isinstance(copied_path, str):
                            transcript_path = resolve_artifact_path(artifact_root, copied_path)
                            if transcript_path is not None and transcript_path.is_file():
                                token_est = estimate_transcript_tokens(transcript_path)
                                if token_est > context_limit_preflight:
                                    if force:
                                        console.print(
                                            f"[yellow]Warning:[/yellow] Parent transcript ({token_est:,} tokens) "
                                            f"exceeds context limit ({context_limit_preflight:,}). "
                                            "Proceeding anyway (--force)."
                                        )
                                    else:
                                        console.print(
                                            f"[red]Error:[/red] Parent transcript ({token_est:,} tokens) exceeds "
                                            f"context limit ({context_limit_preflight:,})."
                                        )
                                        console.print(
                                            "[dim]Tip: Use --strategy structured or --strategy ai-curated instead.[/dim]"
                                        )
                                        sys.exit(1)
        except ForgeSessionError:
            pass  # Parent not found; fork_session() will raise the right error

    # Preflight supervisor proxy BEFORE fork_session() to avoid half-created state
    if supervisor_proxy:
        from forge.guard.semantic.supervisor import preflight_supervisor_proxy

        try:
            supervisor_proxy = preflight_supervisor_proxy(supervisor_proxy)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    try:
        parent_manifest, fork_manifest = manager.fork_session(
            parent_name=parent,
            fork_name=name,
            direct=direct,
            is_incognito=incognito,
            create_worktree=worktree,
            branch=into_branch if into_resolved else branch,
            into_path=into_resolved,
            forge_root=_fr,
            force=force,
        )
    except CannotForkIncognitoError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: Incognito sessions cannot be forked.[/dim]")
        sys.exit(1)
    except BranchExistsError as e:
        _print_branch_exists_tip(e)
        sys.exit(1)
    except BranchInUseError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: The branch is checked out in another worktree. Remove that worktree first.[/dim]")
        sys.exit(1)
    except BranchNotMergedError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: Merge or delete the branch manually before using --force.[/dim]")
        sys.exit(1)
    except WorktreePathExistsError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: Remove the directory or use a different fork name.[/dim]")
        sys.exit(1)
    except InvalidBranchNameError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except SessionNotFoundError:
        if not _hint_cross_project_session(parent, _fr):
            console.print(f"[red]Error:[/red] session '{parent}' not found")
        sys.exit(1)
    except ForgeSessionError as e:
        _handle_error(e)
        return

    # Persist routing override to manifest (ensures --no-launch retains proxy choice)
    fork_worktree_path = Path(fork_manifest.worktree.path) if fork_manifest.worktree else Path.cwd()
    _persist_routing_override(
        forge_root=Path(fork_manifest.forge_root) if fork_manifest.forge_root else fork_worktree_path,
        session_name=fork_manifest.name,
        routing=_preflight_routing,
        direct=direct,
    )
    _apply_routing_override_to_state(state=fork_manifest, routing=_preflight_routing, direct=direct)

    # --- wire supervisor (if --supervise flag set) ---
    if supervise_target:
        from forge.guard.semantic.supervisor import (
            apply_supervisor_routing,
            apply_supervisor_to_intent,
        )
        from forge.session.models import SupervisorConfig
        from forge.session.store import SessionStore

        fork_forge_root = fork_manifest.forge_root or str(fork_worktree_path)
        sup_config = SupervisorConfig(
            resume_id=parent,
            forge_root=parent_manifest.forge_root or fork_forge_root,
        )
        apply_supervisor_routing(
            sup_config,
            parent_manifest,
            supervisor_proxy=supervisor_proxy,
            supervisor_direct=supervisor_direct,
            current_proxy_id=_preflight_routing.proxy_id if _preflight_routing else None,
            current_template=_preflight_routing.template if _preflight_routing else None,
            current_direct=direct,
        )
        fork_store = SessionStore(fork_forge_root, fork_manifest.name)
        fork_store.update(timeout_s=5.0, mutate=lambda m: apply_supervisor_to_intent(m, sup_config))
        fork_manifest = fork_store.read()

    if _preflight_routing:
        effective_template = _preflight_routing.template
        effective_url = _preflight_routing.base_url
        effective_proxy_id = _preflight_routing.proxy_id
    elif proxy_name:
        routing = _sess()._resolve_routing_from_cli(proxy_name=proxy_name, direct=False)
        effective_template = routing.template
        effective_url = routing.base_url
        effective_proxy_id = routing.proxy_id
    else:
        effective_template, effective_url, effective_proxy_id = _get_effective_proxy_for_session(fork_manifest)

    # Compute context limit (uses exact proxy_id when available for deterministic result)
    context_limit = _sess()._resolve_context_limit(effective_proxy_id or effective_template)

    console.print(f"Forked [cyan]{parent}[/cyan] -> [green]{fork_manifest.name}[/green]")
    _print_routing_summary(template=effective_template, base_url=effective_url)
    if fork_manifest.worktree and fork_manifest.worktree.is_worktree:
        console.print(f"  Worktree: {display_path(fork_manifest.worktree.path)}")
        console.print(f"  Branch:   {fork_manifest.worktree.branch}")
    if supervise_target:
        console.print(f"  Supervisor: {parent}")
    if incognito:
        console.print("[yellow]  (will auto-delete on exit)[/yellow]")
    console.print()

    parent_session_id = parent_manifest.confirmed.claude_session_id
    if not parent_session_id:
        console.print("[red]Error:[/red] Parent session has no UUID")
        console.print("The parent session may not have been started yet.")
        sys.exit(1)

    # Set env vars for fork registration (hook uses FORGE_FORK_NAME for fork detection)
    env_vars, unset_env_vars = _sess()._build_session_env(
        session_name=fork_manifest.name,
        context_limit=context_limit,
        template=effective_template,
        base_url=effective_url,
        fork_name=fork_manifest.name,
        parent_session=parent,
        forge_root=fork_manifest.forge_root,
        subprocess_proxy=fork_manifest.intent.subprocess_proxy,
    )
    fork_name = fork_manifest.name  # Capture for cleanup
    is_worktree_fork = bool(fork_manifest.worktree and fork_manifest.worktree.is_worktree)
    if effective_url is None:
        from forge.runtime_config import get_default_direct_model

        fork_direct_model = fork_manifest.intent.launch.direct_model if fork_manifest.intent.launch else None
        fork_direct_model = fork_direct_model or get_default_direct_model()
        error = apply_direct_model_env(env_vars, fork_direct_model)
        if error:
            console.print(f"[red]Error:[/red] {error}")
            sys.exit(1)

    # Warn about --strategy/--inline-plan on same-directory forks (only if user explicitly set them)
    if not is_worktree_fork and (_strategy_explicit or _inline_plan_explicit):
        console.print(
            "[dim]Tip: --strategy/--inline-plan only apply to worktree forks "
            "(ignored for same-directory forks).[/dim]"
        )

    # Worktree forks: Claude Code stores sessions at ~/.claude/projects/<encoded-cwd>/,
    # so --resume --fork-session cannot find the parent's conversation from a different
    # directory. Tested 2026-04-02 with Claude Code 2.1.90: all cross-CWD scenarios fail
    # with "No conversation found." See scripts/experiments/native-resume/.
    # Use handoff (assembled context via --append-system-prompt-file) instead.
    if is_worktree_fork:
        worktree_path = Path(fork_manifest.worktree.path)  # type: ignore[union-attr]
        fork_context, prompt_warnings = _sess()._generate_parent_handoff_context(
            manager=manager,
            manifest=fork_manifest,
            parent_state=parent_manifest,
            strategy=strategy,
            inline_plan=inline_plan,
        )
        prompt_files: list[Path] = []
        if fork_context is not None:
            prompt_files.append(fork_context)
        configured_prompt = _resolve_manifest_prompt_file(fork_manifest)
        if configured_prompt is not None:
            prompt_files.append(configured_prompt)
        prompt_file = _combine_prompt_files(
            worktree_path=worktree_path,
            session_name=fork_manifest.name,
            prompt_files=prompt_files,
        )
        if prompt_file:
            prompt_path = Path(prompt_file)
            try:
                console.print(f"  Context:  {prompt_path.relative_to(worktree_path)}")
            except ValueError:
                console.print(f"  Context:  {display_path(prompt_path)}")
        for warning in prompt_warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")

        try:
            fork_manifest = _persist_fork_handoff_derivation(
                manifest=fork_manifest,
                strategy=strategy,
                context_path=fork_context,
            )
        except Exception:
            logger.warning("Failed to persist fork derivation handoff details", exc_info=True)

        _fork_uuid = str(_uuid.uuid4())
        try:
            from forge.session import SessionStore as _ForkStore

            _fork_wt = Path(fork_manifest.worktree.path) if fork_manifest.worktree else Path.cwd()
            _fork_store_root = Path(fork_manifest.forge_root) if fork_manifest.forge_root else _fork_wt
            _fork_store = _ForkStore(str(_fork_store_root), fork_manifest.name)
            from forge.session.claude.paths import (
                resolve_claude_project_root as _resolve_fork_root_preseed,
            )

            _fork_cwd_preseed = _resolve_fork_root_preseed(fork_manifest)

            def _preseed_mutate(m: SessionState) -> None:
                m.confirmed.claude_session_id = _fork_uuid
                m.confirmed.claude_project_root = _fork_cwd_preseed

            _fork_store.update(timeout_s=5.0, mutate=_preseed_mutate)
            manager.index_store.sync_uuid_from_state(fork_manifest.name, _fork_store.read())
        except Exception:
            logger.debug("Pre-seed UUID write failed (hook will reconcile)", exc_info=True)

        from forge.session.claude.paths import (
            resolve_claude_project_root as _resolve_fork_root,
        )

        _fork_cwd = _resolve_fork_root(fork_manifest)

        def _invoke_fork() -> int:
            return _sess().invoke_claude(
                session_id=_fork_uuid,
                name=fork_manifest.name,
                model=None,
                system_prompt_file=prompt_file,
                env_vars=env_vars,
                unset_env_vars=unset_env_vars,
                cwd=_fork_cwd,
            )

    # Same-directory forks: --resume --fork-session works natively.
    else:
        from forge.session.claude.paths import (
            resolve_claude_project_root as _resolve_fork_root,
        )

        _fork_cwd = _resolve_fork_root(fork_manifest)

        def _invoke_fork() -> int:
            return _sess().invoke_claude(
                resume_id=parent_session_id,
                fork_session=True,
                name=fork_manifest.name,
                model=None,
                env_vars=env_vars,
                unset_env_vars=unset_env_vars,
                cwd=_fork_cwd,
            )

    # Auto-install extensions in worktree forks (before no_launch check so --no-launch still prepares the worktree)
    if is_worktree_fork:
        extension_root = _resolve_worktree_extension_root(fork_manifest)
        # For --into, skip if the target already has a local Forge install
        _skip_extensions = False
        if into_resolved is not None and extension_root is not None:
            try:
                from forge.install.tracking import TrackingStore as _TSCheck

                if _TSCheck().get_installation("local", str(extension_root)) is not None:
                    _skip_extensions = True
                    logger.debug("Skipping auto-install: target worktree has existing local install")
            except Exception:
                pass

        if not _skip_extensions and extension_root is not None:
            # Use forge_root (where .claude/ and .forge/ live), not checkout_root.
            # The tracking store keys by forge_root, so get_repo_root() misses when
            # forge_root != checkout_root (e.g., nested .claude/ in a subdirectory).
            _parent_forge_root = Path(
                parent_manifest.forge_root
                or (parent_manifest.worktree.path if parent_manifest.worktree else str(Path.cwd()))
            )
            _sess()._auto_install_extensions(
                install_root=extension_root,
                parent_project_root=_parent_forge_root,
                force_extensions=extensions,
            )
    elif extensions is True:
        console.print("[dim]Tip: --extensions only applies with --worktree.[/dim]")

    if no_launch:
        console.print("[dim]Fork created (--no-launch: Claude not started)[/dim]")
        if is_worktree_fork:
            console.print(f"\n[dim]Tip: {_resume_tip_command(fork_manifest)}[/dim]")
        sys.exit(0)

    use_sidecar, mounts, image = _get_launch_preferences(fork_manifest)
    runtime_base_url = _get_runtime_base_url(use_sidecar=use_sidecar, effective_url=effective_url)

    if use_sidecar:
        exit_code = 0
        try:
            exit_code = _launch_claude_for_session(
                manifest=fork_manifest,
                session_id=_fork_uuid if is_worktree_fork else None,
                resume_id=None if is_worktree_fork else parent_session_id,
                effective_template=effective_template,
                runtime_base_url=runtime_base_url,
                context_limit=context_limit,
                use_sidecar=True,
                mounts=mounts,
                image=image,
                fork_session=not is_worktree_fork,
                register_fork=is_worktree_fork,
                system_prompt_file=prompt_file if is_worktree_fork else None,
                name=fork_manifest.name,
                proxy_id=effective_proxy_id,
            )
        finally:
            if incognito:
                console.print(f"\n[dim]Cleaning up incognito fork '{fork_name}'...[/dim]")
                try:
                    manager.delete_session(
                        fork_name,
                        delete_transcripts=True,
                        force=True,
                        forge_root=fork_manifest.forge_root,
                    )
                    console.print("[green]Cleanup complete.[/green]")
                except ForgeSessionError as e:
                    console.print(f"[yellow]Cleanup warning:[/yellow] {e}")
        sys.exit(exit_code)

    fork_worktree = Path(fork_manifest.worktree.path) if fork_manifest.worktree else Path.cwd()
    # Check hooks from forge_root (where .claude/ lives), not checkout root
    _fork_forge_root = Path(fork_manifest.forge_root) if fork_manifest.forge_root else fork_worktree
    _sess()._warn_if_hooks_missing(_fork_forge_root)
    _sess()._warn_if_version_outdated()
    active_claude_session_id = _fork_uuid if is_worktree_fork else None

    if incognito:
        exit_code = 0
        try:
            exit_code = _sess().run_with_active_session(
                session_name=fork_name,
                worktree_path=fork_worktree,
                launch_mode=LAUNCH_MODE_HOST,
                forge_root=fork_manifest.forge_root,
                claude_session_id=active_claude_session_id,
                runner=_invoke_fork,
            )
        finally:
            console.print(f"\n[dim]Cleaning up incognito fork '{fork_name}'...[/dim]")
            try:
                manager.delete_session(
                    fork_name,
                    delete_transcripts=True,
                    force=True,
                    forge_root=fork_manifest.forge_root,
                )
                console.print("[green]Cleanup complete.[/green]")
            except ForgeSessionError as e:
                console.print(f"[yellow]Cleanup warning:[/yellow] {e}")
        sys.exit(exit_code)
    else:
        exit_code = _sess().run_with_active_session(
            session_name=fork_name,
            worktree_path=fork_worktree,
            launch_mode=LAUNCH_MODE_HOST,
            forge_root=fork_manifest.forge_root,
            claude_session_id=active_claude_session_id,
            runner=_invoke_fork,
        )
        _print_post_exit_tip(fork_manifest)
        sys.exit(exit_code)


@session.command()
@click.argument("name", required=False)
@click.option(
    "--proxy",
    "proxy_name",
    type=str,
    default=None,
    help="Proxy to use (proxy_id or template name)",
)
@click.option("--no-proxy", "direct", is_flag=True, help="Bypass the proxy and talk to Anthropic directly")
@click.option("--direct", "direct_deprecated", is_flag=True, hidden=True, help="Deprecated alias for --no-proxy")
@click.option("--system-prompt", "-s", help="Append system prompt text")
@click.option(
    "--system-prompt-file",
    "-S",
    type=click.Path(exists=True),
    help="Append system prompt from file",
)
@click.option("--worktree", "-w", is_flag=True, help="Create git worktree for session isolation")
@click.option("--branch", "-b", help="Override branch name (requires --worktree)")
@click.option("--sidecar", is_flag=True, help="Run with bundled proxy in Docker container")
@click.option("--host-proxy", is_flag=True, help="Use host proxy (overrides config)")
@click.option("--mount", "mounts", multiple=True, help="Extra mounts (host:container[:ro|rw])")
@click.option("--image", default=None, help="Docker image for sidecar mode")
@click.option(
    "--extensions/--no-extensions",
    default=None,
    help="Auto-install extensions in worktree (default: inherit from parent)",
)
def incognito(
    name: str | None,
    proxy_name: str | None,
    direct: bool,
    direct_deprecated: bool,
    system_prompt: str | None,
    system_prompt_file: str | None,
    worktree: bool,
    branch: str | None,
    sidecar: bool,
    host_proxy: bool,
    mounts: tuple[str, ...],
    image: str | None,
    extensions: bool | None,
) -> None:
    """Start an incognito session.

    Shortcut for ``forge session start --incognito``. The session is
    automatically deleted when exited.

    \b
    Examples:
        forge session incognito                          # Auto-named
        forge session incognito --proxy litellm-gemini   # With proxy
        forge session incognito my-test                  # Custom name
    """
    direct = direct or direct_deprecated
    if direct and proxy_name:
        console.print("[red]Error:[/red] --no-proxy and --proxy are mutually exclusive")
        sys.exit(1)

    # Default to direct mode when neither --proxy nor --no-proxy is given,
    # unless --sidecar or --host-proxy is specified (both imply proxy mode).
    if not proxy_name and not direct and not sidecar and not host_proxy:
        direct = True

    routing: ResolvedRouting | None = None
    if proxy_name:
        routing = _sess()._resolve_routing_from_cli(proxy_name=proxy_name, direct=False)

    from forge.cli.guards import require_repo_root

    require_repo_root()

    if name is None:
        _fr = _sess()._cwd_forge_root()
        existing = {n for n, _ in _sess().SessionManager().list_sessions(forge_root_filter=_fr)}
        name = _sess().generate_unique_name(existing)

    # Incognito cleanup is handled inside launch_new_session() so that
    # validation/creation failures don't trigger deletion of existing sessions.
    sys.exit(
        launch_new_session(
            name=name,
            template=routing.template if routing else None,
            base_url=routing.base_url if routing else None,
            direct=direct,
            incognito=True,
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            worktree=worktree,
            branch=branch,
            sidecar=sidecar,
            host_proxy=host_proxy,
            mounts=mounts,
            image=image,
            no_launch=False,
            extensions=extensions,
            proxy_id=routing.proxy_id if routing else None,
            proxy_display=routing.proxy_id if routing else None,
            context_limit_override=routing.context_limit if routing else None,
        )
    )
