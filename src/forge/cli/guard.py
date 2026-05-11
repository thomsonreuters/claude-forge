"""Guard CLI commands for policy management.

Commands for managing policy enforcement:
- enable: Enable policy bundles for the current session
- disable: Disable policy enforcement
- status: Show current policy configuration and state
- check: Evaluate policies on demand against a file or diff
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from forge.core.paths import display_path
from forge.guard.queries import (
    find_sessions_supervised_by,
    read_scoped_supervisor_target,
)
from forge.session import SessionStore
from forge.session.effective import compute_effective_intent
from forge.session.hooks.session_start import ENV_SESSION
from forge.session.models import PolicyIntent, SessionState
from forge.session.store import HOOK_LOCK_TIMEOUT_S, MANIFEST_FILENAME, get_sessions_dir

console = Console()


def _resolve_session_name(cwd: Path) -> str | None:
    """Resolve current session: FORGE_SESSION env var, or auto-detect if exactly one exists."""
    name = os.environ.get(ENV_SESSION)
    if name:
        return name

    sessions_dir = get_sessions_dir(cwd)
    if not sessions_dir.is_dir():
        return None

    candidates = [d.name for d in sessions_dir.iterdir() if d.is_dir() and (d / MANIFEST_FILENAME).exists()]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_forge_root(cwd: Path) -> str:
    """Resolve forge_root from CWD (falls back to CWD itself)."""
    try:
        from forge.core.ops.context import find_forge_root

        fr = find_forge_root(cwd)
        return str(fr) if fr else str(cwd)
    except Exception:
        return str(cwd)


def _resolve_session_for_display(
    name: str,
    cwd: Path,
) -> tuple[SessionStore, SessionState]:
    """Resolve a named session, repo-scoped with current-project preference.

    Delegates to the shared two-tier resolver in core.ops.resolution.
    """
    from forge.core.ops.resolution import resolve_session_repo_wide

    resolved = resolve_session_repo_wide(name, _resolve_forge_root(cwd))
    return resolved.store, resolved.state


@click.group()
def guard() -> None:
    """Manage policy enforcement for the current session.

    \b
    Examples:
        forge guard enable --bundle tdd        # Enable TDD policy
        forge guard status                     # Show policy state
        forge guard check --bundle tdd -f src/foo.py  # On-demand check
    """
    pass


@guard.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_bundles(as_json: bool) -> None:
    """List available policy bundles and their rules."""
    from forge.guard.deterministic.registry import BUNDLES, get_bundle_policies

    if as_json:
        import json

        data = []
        for bundle_name in sorted(BUNDLES):
            policies = get_bundle_policies(bundle_name)
            data.append(
                {
                    "name": bundle_name,
                    "policies": [
                        {"policy_id": p.policy_id, "description": getattr(p, "description", None)} for p in policies
                    ],
                }
            )
        click.echo(json.dumps(data, indent=2, default=str))
        return

    for bundle_name in sorted(BUNDLES):
        policies = get_bundle_policies(bundle_name)
        console.print(f"[bold cyan]{bundle_name}[/bold cyan]")
        for p in policies:
            console.print(f"  {p.policy_id}")
            if hasattr(p, "description") and p.description:
                console.print(f"    [dim]{p.description}[/dim]")
        console.print()


@guard.command(name="enable")
@click.option(
    "--bundle",
    "-b",
    "bundles",
    multiple=True,
    type=click.Choice(["tdd", "coding_standards"]),
    help="Policy bundles to enable (can be repeated)",
)
@click.option(
    "--fail-mode",
    type=click.Choice(["open", "closed"]),
    default="open",
    help="Behavior on policy errors (default: open)",
)
@click.option(
    "--permissive",
    is_flag=True,
    default=False,
    help="TDD permissive mode: warn instead of deny (sets bundle_config.tdd.strict=false)",
)
def enable(bundles: tuple[str, ...], fail_mode: str, permissive: bool) -> None:
    """Enable policy enforcement for the current session.

    \b
    Examples:
        forge guard enable --bundle tdd --bundle coding_standards
        forge guard enable --bundle tdd --permissive
    """
    if not bundles:
        console.print("[yellow]Warning:[/yellow] No bundles specified. Use --bundle to enable policies.")
        console.print("Available bundles: tdd, coding_standards")
        return

    cwd = Path.cwd().resolve()
    session_name = _resolve_session_name(cwd)
    if not session_name:
        console.print(f"[red]Error:[/red] No session found in {display_path(cwd)}")
        console.print("  Run 'forge session start' first to create a session.")
        sys.exit(1)

    store = SessionStore(_resolve_forge_root(cwd), session_name)

    try:
        store.read()  # Verify session exists
    except Exception:
        console.print(f"[red]Error:[/red] No session found in {display_path(cwd)}")
        console.print("  Run 'forge session start' first to create a session.")
        sys.exit(1)

    bundle_config: dict[str, dict[str, object]] = {}
    if permissive and "tdd" in bundles:
        bundle_config["tdd"] = {"strict": False}

    def _mutate(m: object) -> None:
        if not isinstance(m, SessionState):
            raise TypeError(f"Expected SessionState, got {type(m)}")

        m.intent.policy = PolicyIntent(
            enabled=True,
            fail_mode=fail_mode,  # type: ignore[arg-type]  # click Choice returns str, not Literal
            bundles=list(bundles),
            bundle_config=bundle_config,
        )

    try:
        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to update session: {e}")
        sys.exit(1)

    console.print(f"[green]Policy enabled[/green] with bundles: {', '.join(bundles)}")
    console.print(f"  Fail mode: {fail_mode}")

    from forge.install.hooks import has_forge_hook

    if not has_forge_hook(cwd, "PreToolUse", "forge hook policy-check"):
        console.print(
            "\n[yellow]Warning:[/yellow] Policy configured but PreToolUse hook is not installed. "
            "Enforcement will not be active."
        )
        console.print("[dim]Tip: Run 'forge extension enable' to install hooks.[/dim]")
    if bundle_config:
        for bundle, cfg in bundle_config.items():
            cfg_str = ", ".join(f"{k}={v}" for k, v in cfg.items())
            console.print(f"  {bundle}: {cfg_str}")

    from forge.guard.deterministic.registry import get_policy_ids_for_bundle

    rules = []
    for bundle in bundles:
        rules.extend(get_policy_ids_for_bundle(bundle))

    if rules:
        console.print("  Active rules:")
        for rule in rules:
            console.print(f"    - {rule}")


@guard.command(name="disable")
def disable() -> None:
    """Disable policy enforcement for the current session."""
    cwd = Path.cwd().resolve()
    session_name = _resolve_session_name(cwd)
    if not session_name:
        console.print(f"[red]Error:[/red] No session found in {display_path(cwd)}")
        sys.exit(1)

    store = SessionStore(_resolve_forge_root(cwd), session_name)

    try:
        store.read()  # Verify session exists
    except Exception:
        console.print(f"[red]Error:[/red] No session found in {display_path(cwd)}")
        sys.exit(1)

    def _mutate(m: object) -> None:
        if not isinstance(m, SessionState):
            raise TypeError(f"Expected SessionState, got {type(m)}")

        if m.intent.policy:
            m.intent.policy.enabled = False
        else:
            m.intent.policy = PolicyIntent(enabled=False)

    try:
        store.update(timeout_s=HOOK_LOCK_TIMEOUT_S, mutate=_mutate)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to update session: {e}")
        sys.exit(1)

    console.print("[green]Policy enforcement disabled[/green]")


@guard.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--session", "-s", "session_name", help="Target session (default: auto-detect)")
def status(as_json: bool, session_name: str | None) -> None:
    """Show current policy configuration and state."""
    cwd = Path.cwd().resolve()

    if session_name:
        from forge.session.exceptions import ForgeSessionError

        try:
            store, manifest = _resolve_session_for_display(session_name, cwd)
        except ForgeSessionError as e:
            console.print(f"[red]Error:[/red] Session '{session_name}' not found: {e}")
            sys.exit(1)
    else:
        name = _resolve_session_name(cwd)
        if not name:
            console.print(f"[red]Error:[/red] No session found in {display_path(cwd)}")
            sys.exit(1)
        store = SessionStore(_resolve_forge_root(cwd), name)
        try:
            manifest = store.read()
        except Exception:
            console.print(f"[red]Error:[/red] No session found in {display_path(cwd)}")
            sys.exit(1)

    try:
        effective = compute_effective_intent(manifest)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to compute effective config: {exc}")
        sys.exit(1)

    if as_json:
        import json

        policy_data: dict[str, object] = {"session_name": manifest.name}
        if effective.policy:
            sup = effective.policy.supervisor
            sup_data = None
            if sup:
                sup_data = {
                    "resume_id": sup.resume_id,
                    "suspended": sup.suspended,
                    "plan_override_path": sup.plan_override_path,
                    "proxy": sup.proxy,
                    "direct": sup.direct,
                    "fork_session": sup.fork_session,
                    "timeout_seconds": sup.timeout_seconds,
                    "throttle_seconds": sup.throttle_seconds,
                    "resolved_uuid": None,
                    "source_model": None,
                }
                if sup.resume_id:
                    ts = read_scoped_supervisor_target(sup.resume_id, sup.forge_root, manifest.forge_root)
                    if ts is not None:
                        sup_data["resolved_uuid"] = ts.confirmed.claude_session_id
                        swp = ts.confirmed.started_with_proxy
                        if swp and swp.template:
                            sup_data["source_model"] = swp.template
            policy_data["policy"] = {
                "enabled": effective.policy.enabled,
                "fail_mode": effective.policy.fail_mode or "open",
                "bundles": effective.policy.bundles or [],
                "bundle_config": effective.policy.bundle_config or {},
                "supervisor": sup_data,
            }
        else:
            policy_data["policy"] = None

        confirmed_policy = manifest.confirmed.policy
        if confirmed_policy:
            policy_data["confirmed"] = {
                "decisions_count": len(confirmed_policy.decisions or []),
                "policy_states_count": len(confirmed_policy.policy_states or {}),
            }
        else:
            policy_data["confirmed"] = None

        supervised = find_sessions_supervised_by(
            manifest.name, manifest.confirmed.claude_session_id, manifest.forge_root
        )
        if supervised:
            policy_data["supervised_sessions"] = supervised

        click.echo(json.dumps(policy_data, indent=2, default=str))
        return

    table = Table(title=f"Policy Status: {manifest.name}", show_header=False)
    table.add_column("Key", style="cyan")
    table.add_column("Value")

    if effective.policy:
        table.add_row("Enabled", "Yes" if effective.policy.enabled else "No")
        table.add_row("Fail Mode", effective.policy.fail_mode or "open")
        table.add_row(
            "Bundles",
            ", ".join(effective.policy.bundles) if effective.policy.bundles else "None",
        )
        if effective.policy.bundle_config:
            for bundle, cfg in effective.policy.bundle_config.items():
                cfg_str = ", ".join(f"{k}={v}" for k, v in cfg.items())
                table.add_row(f"  {bundle}", cfg_str)

        if effective.policy.supervisor:
            sup = effective.policy.supervisor
            status = "Suspended" if sup.suspended else "Configured"
            table.add_row("Supervisor", status)
            if sup.resume_id:
                table.add_row("  Target", sup.resume_id)
                ts = read_scoped_supervisor_target(sup.resume_id, sup.forge_root, manifest.forge_root)
                if ts is not None:
                    uuid = ts.confirmed.claude_session_id
                    if uuid:
                        table.add_row("  Claude UUID", uuid[:16] + "...")
                    swp = ts.confirmed.started_with_proxy
                    if swp and swp.template:
                        table.add_row("  Source model", swp.template)
            if sup.proxy:
                table.add_row("  Routing", f"proxy: {sup.proxy}")
            elif sup.direct:
                table.add_row("  Routing", "direct (no proxy)")
            table.add_row("  Fork session", "Yes" if sup.fork_session else "No")
            table.add_row("  Timeout", f"{sup.timeout_seconds}s")
            table.add_row("  Throttle", f"{sup.throttle_seconds}s")
            if sup.plan_override_path:
                table.add_row("  Plan override", sup.plan_override_path)
        else:
            table.add_row("Supervisor", "Not configured")
    else:
        table.add_row("Enabled", "No (not configured)")

    console.print(table)

    if manifest.confirmed.policy:
        confirmed = manifest.confirmed.policy
        console.print()
        state_table = Table(title="Policy State (from hooks)", show_header=False)
        state_table.add_column("Key", style="cyan")
        state_table.add_column("Value")

        state_table.add_row("Decisions Logged", str(len(confirmed.decisions or [])))
        state_table.add_row("Policy States", str(len(confirmed.policy_states or {})))

        console.print(state_table)

        if confirmed.policy_states:
            for policy_id, state in confirmed.policy_states.items():
                items = ", ".join(f"{k}: {len(v) if isinstance(v, (list, dict)) else v}" for k, v in state.items())
                console.print(f"  [dim]{policy_id}[/dim]: {items}")

    # Supervised-sessions tip (always, not gated on "no supervisor" — chains are valid)
    supervised = find_sessions_supervised_by(manifest.name, manifest.confirmed.claude_session_id, manifest.forge_root)
    if supervised:
        names = ", ".join(supervised)
        console.print(
            f"\n[dim]Tip: This session supervises: {names}. "
            f"Check with: forge guard status --session {supervised[0]}[/dim]"
        )


_DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+?)(?:\t.*)?$", re.MULTILINE)


def _extract_path_from_diff(diff: str) -> str | None:
    """Extract the first file path from a unified diff.

    Parses ``+++ b/<path>`` lines, stripping trailing tab-delimited
    metadata (timestamps, etc.). Returns None if no path found.
    """
    m = _DIFF_PATH_RE.search(diff)
    if m:
        path = m.group(1).strip()
        return path if path and path != "/dev/null" else None
    return None


@guard.command(name="check")
@click.option(
    "--bundle",
    "-b",
    "bundles",
    multiple=True,
    required=True,
    type=click.Choice(["tdd", "coding_standards"]),
    help="Policy bundles to evaluate (can be repeated)",
)
@click.option(
    "--file",
    "-f",
    "file_path",
    type=click.Path(exists=True),
    help="File to evaluate policies against",
)
@click.option(
    "--diff",
    "use_diff",
    is_flag=True,
    help="Read git diff from stdin",
)
@click.option(
    "--fail-mode",
    type=click.Choice(["open", "closed"]),
    default="closed",
    help="Behavior on policy errors (default: closed for on-demand checks)",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Output structured JSON",
)
def check(
    bundles: tuple[str, ...],
    file_path: str | None,
    use_diff: bool,
    fail_mode: str,
    json_output: bool,
) -> None:
    """Evaluate policies on demand against a file or diff.

    Unlike hook-triggered checks, this runs explicitly and defaults to
    fail-mode=closed (violations are reported, not swallowed).

    \b
    Examples:
        forge guard check --bundle tdd --file src/foo.py
        forge guard check --bundle tdd --bundle coding_standards -f src/foo.py --json
        git diff | forge guard check --bundle coding_standards --diff
    """
    from forge.guard.engine import build_engine
    from forge.guard.types import ActionContext, extract_added_lines

    if not file_path and not use_diff:
        console.print("[red]Error:[/red] Provide --file or --diff")
        sys.exit(2)

    cwd = Path.cwd().resolve()

    if use_diff:
        if sys.stdin.isatty():
            console.print("[red]Error:[/red] --diff requires input on stdin (e.g., git diff | forge guard check ...)")
            sys.exit(2)
        raw_input = sys.stdin.read()
        tool_name = "Edit"
        target_path = _extract_path_from_diff(raw_input)
        new_content = extract_added_lines(raw_input)
    else:
        assert file_path is not None
        target = Path(file_path)
        try:
            raw_input = target.read_text()
        except Exception as e:
            console.print(f"[red]Error:[/red] Failed to read {display_path(file_path)}: {e}")
            sys.exit(2)
        tool_name = "Write"
        new_content = raw_input
        try:
            target_path = str(target.resolve().relative_to(cwd))
        except ValueError:
            target_path = str(target)

    context = ActionContext(
        event="OnDemand.Check",
        tool_name=tool_name,
        tool_args={"file_path": file_path or "", "content": new_content[:200]},
        repo_root=str(cwd),
        session_name="on-demand",
        target_path=target_path,
        new_content=new_content[:5000] if new_content else None,
        raw_diff=raw_input[:5000] if use_diff and raw_input else None,
    )

    try:
        engine = build_engine(list(bundles), fail_mode=fail_mode)  # type: ignore[arg-type]
        result = engine.evaluate(context)
    except Exception as e:
        if json_output:
            click.echo(json.dumps({"error": str(e), "passed": False}))
        else:
            console.print(f"[red]Error:[/red] Policy evaluation failed: {e}")
        sys.exit(2)

    # Determine exit code: allow and warn both exit 0 (warn = advisory)
    passed = result.final_decision in ("allow", "warn")
    exit_code = 0 if passed else 1

    if json_output:
        # Build violations with intent from their parent decisions
        violations_json = []
        for d in result.decisions:
            if d.decision != "deny":
                continue
            for v in d.violations:
                entry: dict[str, str | None] = {
                    "rule_id": v.rule_id,
                    "message": v.message,
                    "severity": v.severity,
                    "suggested_fix": v.suggested_fix,
                }
                if d.intent:
                    entry["intent"] = d.intent
                violations_json.append(entry)
        output = {
            "passed": passed,
            "clean": result.final_decision == "allow",
            "final_decision": result.final_decision,
            "violations": violations_json,
            "warnings": result.all_warnings,
            "policies_evaluated": [d.policy_id for d in result.decisions],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        if result.final_decision == "allow":
            console.print("[green]All policies passed[/green]")
        elif result.final_decision == "warn":
            console.print("[yellow]Passed with warnings[/yellow]")
            for w in result.all_warnings:
                console.print(f"  ⚠︎ {w}", style="yellow")
        else:
            console.print(f"[red]Policy check failed ({result.final_decision})[/red]")
            for d in result.decisions:
                if d.decision != "deny":
                    continue
                table = Table(show_header=True)
                table.add_column("Rule", style="cyan")
                table.add_column("Severity", style="red")
                table.add_column("Message")
                table.add_column("Fix", style="dim")
                for v in d.violations:
                    table.add_row(v.rule_id, v.severity, v.message, v.suggested_fix or "")
                if d.intent:
                    table.add_row("", "", f"[dim]Intent: {d.intent}[/dim]", "")
                console.print(table)

        if result.all_warnings and result.final_decision != "warn":
            for w in result.all_warnings:
                console.print(f"  [dim]⚠︎ {w}[/dim]")

    sys.exit(exit_code)


# Prefixes that invoke_supervisor() uses in warnings when it fails open.
# Used by the CLI to convert allow→exit(2).
_INFRA_FAILURE_PREFIXES = ("Supervisor error:", "Supervisor skipped")


@guard.command(name="supervisor")
@click.option(
    "--file",
    "-f",
    "file_path",
    type=click.Path(exists=True),
    required=True,
    help="File to evaluate against the plan",
)
@click.option(
    "--resume-id",
    "-r",
    required=True,
    help="Claude session UUID for --resume, or a Forge session name to resolve",
)
@click.option(
    "--proxy",
    "proxy_name",
    type=str,
    default=None,
    help="Proxy (proxy_id or template name) for base_url resolution",
)
@click.option("--no-proxy", "direct", is_flag=True, default=False, help="Force direct Anthropic routing (bypass proxy)")
@click.option("--direct", "direct_deprecated", is_flag=True, hidden=True, help="Deprecated alias for --no-proxy")
@click.option(
    "--timeout",
    "-t",
    type=int,
    default=45,
    help="Supervisor timeout in seconds (default: 45)",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Output structured JSON",
)
def supervisor_cmd(
    file_path: str,
    resume_id: str,
    proxy_name: str | None,
    direct: bool,
    direct_deprecated: bool,
    timeout: int,
    json_output: bool,
) -> None:
    """Evaluate a single file against a supervisor plan (one-shot).

    For persistent supervisor configuration, use 'forge guard supervise' instead.

    Fail-closed: exit 0 (aligned), exit 1 (divergent), exit 2 (could not evaluate).

    \b
    Examples:
        forge guard supervisor -f src/foo.py -r abc-123 --json
        forge guard supervisor -f src/foo.py -r planning-session --json
        forge guard supervisor -f src/foo.py -r abc-123 --proxy openrouter-openai
        forge guard supervisor -f src/foo.py -r abc-123 --no-proxy
    """
    direct = direct or direct_deprecated
    if direct and proxy_name:
        console.print("[red]Error:[/red] --no-proxy and --proxy are mutually exclusive")
        sys.exit(1)

    from forge.guard.semantic.supervisor import SUPERVISOR_INTENT, invoke_supervisor
    from forge.guard.types import ActionContext
    from forge.session.models import SupervisorConfig

    target = Path(file_path)
    try:
        file_content = target.read_text()
    except Exception as e:
        if json_output:
            click.echo(json.dumps({"error": str(e), "passed": False}))
        else:
            console.print(f"[red]Error:[/red] Failed to read {display_path(file_path)}: {e}")
        sys.exit(2)

    cwd = Path.cwd().resolve()
    try:
        target_path = str(target.resolve().relative_to(cwd))
    except ValueError:
        target_path = str(target)

    config = SupervisorConfig(
        resume_id=resume_id,
        proxy=proxy_name,
        direct=direct,
        timeout_seconds=timeout,
        fork_session=True,
    )

    context = ActionContext(
        event="OnDemand.Supervisor",
        tool_name="Write",
        tool_args={"file_path": file_path, "content": file_content[:200]},
        repo_root=str(cwd),
        session_name="on-demand",
        target_path=target_path,
        new_content=file_content[:5000],
    )

    try:
        decision = invoke_supervisor(config, context, intent=SUPERVISOR_INTENT)
    except Exception as e:
        if json_output:
            click.echo(json.dumps({"error": str(e), "passed": False}))
        else:
            console.print(f"[red]Error:[/red] Supervisor invocation failed: {e}")
        sys.exit(2)

    # Detect infra failures hidden behind fail-open allow decisions
    infra_failure = decision.decision == "allow" and any(
        w.startswith(prefix) for w in (decision.warnings or []) for prefix in _INFRA_FAILURE_PREFIXES
    )

    if infra_failure:
        passed = False
        exit_code = 2
    elif decision.decision == "deny":
        passed = False
        exit_code = 1
    else:
        passed = True
        exit_code = 0

    if json_output:
        violations_list = []
        for v in decision.violations:
            v_entry: dict[str, str | None] = {
                "rule_id": v.rule_id,
                "severity": v.severity,
                "message": v.message,
                "evidence": v.evidence,
                "suggested_fix": v.suggested_fix,
            }
            if decision.intent:
                v_entry["intent"] = decision.intent
            violations_list.append(v_entry)
        output = {
            "passed": passed,
            "clean": decision.decision == "allow" and not infra_failure,
            "final_decision": decision.decision if not infra_failure else "error",
            "policy_id": decision.policy_id,
            "violations": violations_list,
            "warnings": decision.warnings or [],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        if exit_code == 0:
            if decision.decision == "allow":
                console.print("[green]Aligned with plan[/green]")
            else:
                console.print("[yellow]Aligned with warnings[/yellow]")
                for w in decision.warnings or []:
                    console.print(f"  ⚠︎ {w}", style="yellow")
        elif exit_code == 1:
            console.print("[red]Divergent from plan[/red]")
            for w in decision.warnings or []:
                console.print(f"  [red]{w}[/red]")
        else:
            console.print("[red]Could not evaluate[/red]")
            for w in decision.warnings or []:
                console.print(f"  [dim]{w}[/dim]")

    sys.exit(exit_code)


@guard.command(name="supervise")
@click.argument("target", required=False)
@click.option("--off", is_flag=True, help="Suspend supervisor (preserves config)")
@click.option("--on", "on_flag", is_flag=True, help="Resume suspended supervisor")
@click.option("--remove", is_flag=True, help="Remove supervisor configuration entirely")
@click.option("--reload", "reload_auto", is_flag=True, help="Reload latest relevant approved plan")
@click.option("--reload-from", "reload_path", default=None, help="Reload plan from explicit file path")
@click.option("--session", "-s", "session_name", help="Target session (default: auto-detect)")
@click.option("--supervisor-proxy", type=str, default=None, help="Proxy for supervisor routing (proxy_id or template)")
@click.option(
    "--no-supervisor-proxy",
    "supervisor_direct",
    is_flag=True,
    default=False,
    help="Force supervisor to use direct Anthropic routing",
)
def supervise_cmd(
    target: str | None,
    off: bool,
    on_flag: bool,
    remove: bool,
    reload_auto: bool,
    reload_path: str | None,
    session_name: str | None,
    supervisor_proxy: str | None,
    supervisor_direct: bool,
) -> None:
    """Configure the semantic supervisor for the current session.

    Sets durable plan supervision that persists through session resume.
    Use 'forge guard supervisor' for one-shot file evaluation instead.

    \b
    Examples:
        forge guard supervise planner           # Set planner as supervisor
        forge guard supervise --off             # Suspend (preserves config)
        forge guard supervise --on              # Resume
        forge guard supervise --remove          # Remove entirely
        forge guard supervise --reload          # Reload latest relevant approved plan
        forge guard supervise --reload-from p   # Reload plan from explicit file
        forge guard supervise                   # Show current config
    """
    if supervisor_proxy and supervisor_direct:
        console.print("[red]Error:[/red] --supervisor-proxy and --no-supervisor-proxy are mutually exclusive")
        sys.exit(1)
    if (supervisor_proxy or supervisor_direct) and not target:
        console.print("[red]Error:[/red] --supervisor-proxy/--no-supervisor-proxy require a target argument")
        sys.exit(1)
    actions = sum([bool(off), bool(on_flag), bool(remove), bool(reload_auto), bool(reload_path), bool(target)])
    if actions > 1:
        console.print(
            "[red]Error:[/red] Specify only one action (target, --off, --on, --remove, --reload, --reload-from)"
        )
        sys.exit(1)
    cwd = Path.cwd().resolve()
    name = session_name or _resolve_session_name(cwd)
    if not name:
        console.print("[red]Error:[/red] No session found. Start or specify one with --session.")
        sys.exit(1)

    from forge.session.exceptions import ForgeSessionError

    if session_name:
        try:
            store, _ = _resolve_session_for_display(name, cwd)
        except ForgeSessionError as e:
            console.print(f"[red]Error:[/red] Session '{name}' not found: {e}")
            sys.exit(1)
    else:
        store = SessionStore(_resolve_forge_root(cwd), name)

    try:
        store.read()
    except (ForgeSessionError, FileNotFoundError):
        console.print(f"[red]Error:[/red] Session '{name}' not found")
        sys.exit(1)

    if off:
        manifest = store.read()
        has_sup = (
            manifest.intent.policy and manifest.intent.policy.supervisor and manifest.intent.policy.supervisor.resume_id
        )
        if not has_sup:
            console.print("No supervisor configured.")
            return

        def _suspend(m: SessionState) -> None:
            if m.intent.policy and m.intent.policy.supervisor:
                m.intent.policy.supervisor.suspended = True

        store.update(timeout_s=5.0, mutate=_suspend)
        console.print(f"Supervisor suspended for session [cyan]{name}[/cyan]")
        console.print("[dim]Tip: Use --on to resume, --remove to delete.[/dim]")
        return

    if on_flag:
        manifest = store.read()
        has_sup = (
            manifest.intent.policy and manifest.intent.policy.supervisor and manifest.intent.policy.supervisor.resume_id
        )
        if not has_sup:
            console.print("No supervisor configured. Use 'forge guard supervise <target>' to set one.")
            return

        def _resume_sup(m: SessionState) -> None:
            if m.intent.policy and m.intent.policy.supervisor:
                m.intent.policy.supervisor.suspended = False

        store.update(timeout_s=5.0, mutate=_resume_sup)
        console.print(f"Supervisor resumed for session [cyan]{name}[/cyan]")
        return

    if remove:
        manifest = store.read()
        has_sup = manifest.intent.policy and manifest.intent.policy.supervisor
        if not has_sup:
            console.print("No supervisor configured.")
            return

        def _remove_sup(m: SessionState) -> None:
            if m.intent.policy and m.intent.policy.supervisor:
                m.intent.policy.supervisor = None

        store.update(timeout_s=5.0, mutate=_remove_sup)
        console.print(f"Supervisor removed from session [cyan]{name}[/cyan]")
        return

    if reload_auto or reload_path:
        manifest = store.read()
        effective = compute_effective_intent(manifest)
        if not effective.policy or not effective.policy.supervisor or not effective.policy.supervisor.resume_id:
            console.print("[red]Error:[/red] No supervisor configured.")
            sys.exit(1)

        if reload_path:
            resolved = Path(reload_path)
            if not resolved.is_absolute():
                resolved = (cwd / resolved).resolve()
            if not resolved.is_file():
                console.print(f"[red]Error:[/red] Plan file not found: {resolved}")
                sys.exit(1)
            plan_path = str(resolved)
            source_desc = str(resolved)
        else:
            from forge.guard.semantic.supervisor import (
                resolve_supervisor_reload_plan_path,
            )

            result = resolve_supervisor_reload_plan_path(effective.policy.supervisor, manifest)
            if result is None:
                console.print("[red]Error:[/red] No approved plan found for supervisor target or related sessions.")
                sys.exit(1)
            plan_path = result.path
            source_map = {
                "self": "current session",
                "fork": f"review fork '{result.session_name}'",
                "target": "supervisor target",
            }
            source_desc = source_map.get(result.source, result.source)

        def _set_plan(m: SessionState) -> None:
            if m.intent.policy and m.intent.policy.supervisor:
                m.intent.policy.supervisor.plan_override_path = plan_path

        store.update(timeout_s=5.0, mutate=_set_plan)
        console.print(f"Supervisor plan updated from {source_desc}")
        return

    if target:
        from forge.guard.semantic.supervisor import (
            apply_supervisor_routing,
            apply_supervisor_to_intent,
            preflight_supervisor_proxy,
            validate_supervisor_target,
        )
        from forge.session.models import SupervisorConfig

        if supervisor_proxy:
            try:
                supervisor_proxy = preflight_supervisor_proxy(supervisor_proxy)
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                sys.exit(1)

        manifest = store.read()
        # Validate supervisor target in the selected session's scope, not CWD.
        # When --session points to a cross-worktree session, _resolve_forge_root(cwd)
        # would search the wrong project.
        _guard_fr = manifest.forge_root or _resolve_forge_root(cwd)
        try:
            source_state = validate_supervisor_target(target, forge_root=_guard_fr)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)
        current_template = manifest.intent.proxy.template if manifest.intent.proxy else None
        current_proxy_id = None
        if manifest.intent.proxy and hasattr(manifest.intent.proxy, "proxy_id"):
            current_proxy_id = manifest.intent.proxy.proxy_id  # type: ignore[union-attr]
        current_direct = not bool(manifest.intent.proxy)

        sup_config = SupervisorConfig(resume_id=target, forge_root=source_state.forge_root or _guard_fr)
        routing_display = apply_supervisor_routing(
            sup_config,
            source_state,
            supervisor_proxy=supervisor_proxy,
            supervisor_direct=supervisor_direct,
            current_proxy_id=current_proxy_id,
            current_template=current_template,
            current_direct=current_direct,
        )

        store.update(timeout_s=5.0, mutate=lambda m: apply_supervisor_to_intent(m, sup_config))
        console.print(f"Supervisor set to [green]{target}[/green] for session [cyan]{name}[/cyan]")
        if routing_display:
            label = "auto-seeded" if not supervisor_proxy and not supervisor_direct else "explicit"
            console.print(f"  Routing ({label}): {routing_display}")
        return

    # No args: show current supervisor config
    manifest = store.read()
    effective = compute_effective_intent(manifest)

    if not effective.policy or not effective.policy.supervisor or not effective.policy.supervisor.resume_id:
        console.print("No supervisor configured.")
        return

    sup = effective.policy.supervisor
    assert sup.resume_id is not None  # guarded above
    console.print(f"Supervisor: [green]{sup.resume_id}[/green]")
    if sup.suspended:
        console.print("  Status: [yellow]suspended[/yellow]")

    target_state = read_scoped_supervisor_target(sup.resume_id, sup.forge_root, manifest.forge_root)
    if target_state is not None:
        uuid = target_state.confirmed.claude_session_id
        if uuid:
            console.print(f"  Claude UUID: {uuid[:16]}...")
        swp = target_state.confirmed.started_with_proxy
        if swp and swp.template:
            console.print(f"  Source model: {swp.template}")

    if sup.proxy:
        console.print(f"  Routing: proxy: {sup.proxy}")
    elif sup.direct:
        console.print("  Routing: direct (no proxy)")
    console.print(f"  Fork session: {'yes' if sup.fork_session else 'no'}")
    console.print(f"  Timeout: {sup.timeout_seconds}s")
    console.print(f"  Throttle: {sup.throttle_seconds}s")
    if sup.plan_override_path:
        console.print(f"  Plan override: {sup.plan_override_path}")
