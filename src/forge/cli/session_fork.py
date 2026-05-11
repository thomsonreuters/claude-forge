"""Session fork command.

Extracted from session_lifecycle.py for file-size compliance.
Re-exported via session.py so patch("forge.cli.session.fork") works.
"""

from __future__ import annotations

import sys
import uuid as _uuid
from pathlib import Path

import click

from forge.cli.session_addendum import (
    resolve_addendum_content_for_proxy,
    write_managed_addendum,
)
from forge.core.paths import display_path
from forge.session import (
    LAUNCH_MODE_HOST,
    ForgeSessionError,
    SessionState,
)
from forge.session.direct_model import (
    apply_direct_model_env,
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


def _sess():  # type: ignore[return]
    return sys.modules["forge.cli.session"]


from forge.cli.session import (  # noqa: E402
    ResolvedRouting,
    _apply_routing_override_to_state,
    _combine_prompt_files,
    _get_effective_proxy_for_session,
    _get_launch_preferences,
    _get_runtime_base_url,
    _handle_error,
    _hint_cross_project_session,
    _persist_routing_override,
    _print_routing_summary,
    _resolve_session_artifact_root,
    _resolve_worktree_extension_root,
    console,
    logger,
)
from forge.cli.session_lifecycle import (  # noqa: E402
    _launch_claude_for_session,
    _persist_fork_handoff_derivation,
    _print_branch_exists_tip,
    _print_post_exit_tip,
    _resolve_manifest_prompt_file,
    _resume_tip_command,
    session,
)

__all__ = ["fork"]


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
@click.option(
    "--no-proxy",
    "direct",
    is_flag=True,
    help="Bypass the proxy and talk to Anthropic directly",
)
@click.option(
    "--direct",
    "direct_deprecated",
    is_flag=True,
    hidden=True,
    help="Deprecated alias for --no-proxy",
)
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
@click.option(
    "--supervisor-proxy",
    type=str,
    default=None,
    help="Proxy for supervisor routing (requires --supervise)",
)
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

        _wt_addendum = resolve_addendum_content_for_proxy(effective_proxy_id)
        _wt_prompt = prompt_file
        if _wt_addendum:
            _wt_forge_root = Path(fork_manifest.forge_root) if fork_manifest.forge_root else Path.cwd()
            _wt_addendum_path = write_managed_addendum(_wt_forge_root, fork_manifest.name, _wt_addendum)
            _wt_files: list[Path] = [_wt_addendum_path]
            if _wt_prompt:
                _wt_files.append(Path(_wt_prompt))
            _wt_prompt = _combine_prompt_files(
                worktree_path=worktree_path,
                session_name=fork_manifest.name,
                prompt_files=_wt_files,
            )

        def _invoke_fork() -> int:
            return _sess().invoke_claude(
                session_id=_fork_uuid,
                name=fork_manifest.name,
                model=None,
                system_prompt_file=_wt_prompt,
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
        _samedir_addendum = resolve_addendum_content_for_proxy(effective_proxy_id)
        _samedir_prompt: str | None = None
        if _samedir_addendum:
            _samedir_forge_root = Path(fork_manifest.forge_root) if fork_manifest.forge_root else Path.cwd()
            _samedir_prompt = str(write_managed_addendum(_samedir_forge_root, fork_manifest.name, _samedir_addendum))

        def _invoke_fork() -> int:
            return _sess().invoke_claude(
                resume_id=parent_session_id,
                fork_session=True,
                name=fork_manifest.name,
                model=None,
                system_prompt_file=_samedir_prompt,
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
