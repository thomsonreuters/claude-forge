"""Workflow runner CLI commands.

Provides:
- forge workflow panel: Fan out review with check gating
- forge workflow analyze: Deep single-model analysis
- forge workflow debate: Adversarial evaluation with stance injection
- forge workflow consensus: Two-round multi-model consensus building
- forge workflow list-models: Show available model backends
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from forge.proxy.proxies import ProxyResolutionError
from forge.review.models import (
    NAMED_ROLES,
    AdversarialOutput,
    ConsensusOutput,
    ModelSpec,
    MultiReviewOutput,
    ReviewResult,
    RoleSpec,
    StanceSpec,
    resolve_model_specs,
)

# Verdict strings treated as "pass" by --check gating.
# ACCEPT/ACCEPT_WITH_CONDITIONS from debate resources;
# PASS/PASSED/TRUE as general-purpose aliases for other resources.
_ACCEPTING_VERDICTS = frozenset(
    {
        "ACCEPT",
        "ACCEPT_WITH_CONDITIONS",
        "PASS",
        "PASSED",
        "TRUE",
        "SUPPORT",
        "SUPPORT_WITH_CONDITIONS",
    }
)


def _coerce_passed(val: Any) -> bool:
    """Coerce a 'passed' field to bool, handling string 'false' correctly.

    Without this, ``bool("false")`` is ``True`` in Python -- a real CI bug
    when models emit ``{"passed": "false"}`` as a string.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


console = Console()


def _run_preflight(
    specs: list[ModelSpec],
    *,
    json_output: bool = False,
    routing_plan: Any | None = None,
) -> None:
    """Check resolved routing/auth before spawning workers. Exit 1 on failure."""
    from forge.review.engine import preflight_check

    errors = preflight_check(specs, routing_plan=routing_plan)
    warnings = _routing_plan_warnings(specs, routing_plan)
    if not errors:
        if not json_output:
            for warning in warnings:
                console.print(f"[yellow]Routing warning:[/yellow] {warning}")
        return
    if json_output:
        data: dict[str, Any] = {"preflight_errors": errors}
        if warnings:
            data["routing_warnings"] = warnings
        click.echo(json.dumps(data))
    else:
        console.print("[red]Error:[/red] Workflow preflight failed:")
        for err in errors:
            console.print(f"  - {err}")
        console.print(
            "\n[dim]Tip: Check model availability with 'forge workflow list-models'.\n"
            "Check proxy status: 'forge proxy list'\n"
            "Check auth status: 'forge auth status'\n"
            "Create a proxy: 'forge proxy create <template>'[/dim]"
        )
    sys.exit(1)


def _routing_plan_warnings(specs: list[ModelSpec], routing_plan: Any | None) -> list[str]:
    """Return deduped route warnings for human-facing workflow output."""
    if routing_plan is None:
        return []

    warnings: list[str] = []
    seen: set[str] = set()
    for spec, result in zip(specs, routing_plan.routes):
        if not result.warning:
            continue
        message = f"{spec.name}: {result.warning}"
        if message in seen:
            continue
        seen.add(message)
        warnings.append(message)
    return warnings


def _handle_routing_error(error: Exception, *, json_output: bool = False) -> None:
    """Handle routing resolution errors with clean CLI output. Calls sys.exit(1)."""
    msg = str(error)
    if json_output:
        click.echo(json.dumps({"routing_error": msg}))
    else:
        console.print(f"[red]Error:[/red] Routing failed: {msg}")
    sys.exit(1)


_ROUTING_ERRORS = (RuntimeError, ValueError, ProxyResolutionError)


def _load_workflow_resource(name: str) -> str:
    """Load a bundled workflow resource by name via importlib.resources."""
    from importlib import resources

    ref = resources.files("forge.review.resources").joinpath(name)
    return ref.read_text(encoding="utf-8")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def workflow_cmd() -> None:
    """Run multi-model workflows.

    \b
    Examples:
        forge workflow panel docs/design.md          # Multi-model doc review
        forge workflow analyze "Should we use X?"    # Deep single-model analysis
        forge workflow debate "Proposal" --code      # Adversarial code eval
    """


@workflow_cmd.command(name="list-models")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--available", "available_only", is_flag=True, help="Show only ready models")
def list_models(json_output: bool, available_only: bool) -> None:
    """Show available model backends for workflow runners."""
    from forge.review.models import available_model_specs, check_model_availability

    availabilities = check_model_availability(available_model_specs())

    if available_only:
        availabilities = [a for a in availabilities if a.status == "ready"]

    if json_output:
        items = [
            {
                "name": a.spec.name,
                "model_id": a.spec.model_id,
                "family": a.spec.family,
                "provider_refs": list(a.spec.provider_refs),
                "preferred_proxy": a.spec.preferred_proxy,
                "description": a.spec.description,
                "status": a.status,
                "reason": a.reason,
            }
            for a in availabilities
        ]
        click.echo(json.dumps(items, indent=2))
        return

    if not availabilities:
        console.print(
            "[yellow]No models are currently ready.[/yellow]\n"
            "[dim]Tip: Check 'forge proxy list' and 'forge auth status'.[/dim]"
        )
        return

    _print_grouped_models(availabilities)


def _primary_credential(spec: ModelSpec) -> str:
    """Determine the primary credential for a model spec.

    Uses derive_model_routes() to get the first route's credential,
    which is stable and deterministic (no registry read).
    """
    from forge.review.routing import derive_model_routes

    routes = derive_model_routes(spec)
    if routes:
        return routes[0].credential
    return "unknown"


def _credential_env_var(credential_name: str) -> str:
    """Map a credential name to its primary env var for display."""
    from forge.core.auth.capabilities import CREDENTIALS

    cred = CREDENTIALS.get(credential_name)
    if cred:
        for ev in cred.env_vars:
            if ev.required and ev.secret:
                return ev.name
    return ""


def _credential_configured(credential_name: str) -> bool:
    """Check whether a credential's primary secret is available."""
    env_var = _credential_env_var(credential_name)
    if not env_var:
        return False
    from forge.core.auth.template_secrets import resolve_env_or_credential

    return resolve_env_or_credential(env_var) is not None


def _print_grouped_models(availabilities: list) -> None:
    """Print models grouped by primary credential."""
    from collections import OrderedDict

    groups: OrderedDict[str, list] = OrderedDict()
    for a in availabilities:
        cred = _primary_credential(a.spec)
        groups.setdefault(cred, []).append(a)

    _STATUS_STYLES = {"ready": "green", "unavailable": "yellow", "error": "red"}

    console.print("\n[bold]Available Models[/bold]\n")

    for cred_name, items in groups.items():
        env_var = _credential_env_var(cred_name)
        configured = _credential_configured(cred_name)
        config_tag = "[green]configured[/green]" if configured else "[yellow]not configured[/yellow]"
        env_display = f" ({env_var})" if env_var else ""
        console.print(f"  [bold]{cred_name}[/bold]{env_display}  [{config_tag}]")

        for a in items:
            style = _STATUS_STYLES.get(a.status, "")
            desc = a.spec.description
            if a.reason:
                desc += f" [dim]({a.reason})[/dim]"
            console.print(f"    [cyan]{a.spec.name:<24}[/cyan] {desc:<50} [{style}]{a.status}[/{style}]")
        console.print()


@workflow_cmd.command(name="panel")
@click.argument("target", nargs=-1)
@click.option("-p", "--prompt", type=str, default=None, help="Review prompt")
@click.option(
    "--code",
    "code_mode",
    is_flag=True,
    help="Use code review framework (default: document review)",
)
@click.option(
    "--context",
    "context_mode",
    type=str,
    default="blind",
    help='Context mode: "blind" (default) or "resume:<uuid>"',
)
@click.option(
    "--models",
    "-m",
    type=str,
    default=None,
    help="Comma-separated model names (default: all)",
)
@click.option("--timeout", "-t", type=int, default=600, help="Per-model timeout in seconds")
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON")
@click.option(
    "--check",
    "check_mode",
    is_flag=True,
    help="Gate on results: exit 0 if passed, exit 1 if failed",
)
@click.option(
    "--roles",
    type=str,
    default=None,
    help=f"Comma-separated reviewer roles ({','.join(sorted(NAMED_ROLES))})",
)
@click.option(
    "--review-type",
    type=click.Choice(["full", "security", "performance", "quick"]),
    default="full",
    help="Review focus area (security/performance require --code)",
)
@click.option(
    "--severity",
    type=click.Choice(["high", "critical"]),
    default=None,
    help="Minimum severity to report",
)
@click.option("--via", type=str, default=None, help="Route proxy-backed workers through this proxy")
@click.option("--cwd", type=click.Path(exists=True), default=None, help="Working directory")
@click.pass_context
def panel(
    ctx: click.Context,
    target: tuple[str, ...],
    prompt: str | None,
    code_mode: bool,
    context_mode: str,
    models: str | None,
    timeout: int,
    json_output: bool,
    check_mode: bool,
    roles: str | None,
    review_type: str,
    severity: str | None,
    via: str | None,
    cwd: str | None,
) -> None:
    """Fan out a review to multiple models.

    \b
    Examples:
      forge workflow panel docs/design.md                  # docs review (default)
      forge workflow panel src/forge/cli/ --code           # code review
      forge workflow panel -p "Review the error handling"  # custom prompt
      forge workflow panel src/ --code --roles security,architecture
      forge workflow panel src/ --code --review-type security --severity high
    """
    resume_id: str | None = None
    if context_mode == "blind":
        pass
    elif context_mode.startswith("resume:"):
        resume_id = context_mode[len("resume:") :]
        if not resume_id:
            console.print("[red]Error:[/red] --context resume:<uuid> requires a UUID.")
            ctx.exit(2)
            return
    else:
        console.print(f'[red]Error:[/red] Invalid --context "{context_mode}".' ' Use "blind" or "resume:<uuid>".')
        ctx.exit(2)
        return

    # Prompt composition: (1) resolve base prompt/resource
    resolved_prompt = _resolve_panel_prompt(target, prompt, code_mode, review_type)
    if resolved_prompt is None:
        console.print("[red]Error:[/red] No prompt provided. Use target argument, -p, or stdin.")
        ctx.exit(2)
        return

    # Validate review-type/code-mode interaction.
    # Only applies when a review resource is loaded (target-based prompt).
    # Skip when -p or stdin provided a custom prompt (review_type is ignored).
    uses_resource = not prompt and bool(target)
    if uses_resource and review_type in ("security", "performance") and not code_mode:
        console.print(f"[red]Error:[/red] --review-type {review_type} requires --code.")
        ctx.exit(2)
        return

    # Prompt composition: (2) append severity suffix
    if severity:
        resolved_prompt += (
            f"\n\nIMPORTANT: Report only {severity}-severity findings or above. "
            f"Skip lower-severity issues. If no findings meet the {severity} threshold, "
            f"explicitly state: 'No findings at or above {severity} severity.'"
        )

    try:
        specs = resolve_model_specs(models)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        ctx.exit(2)
        return

    # Prompt composition: (3) prepend per-worker role prefix
    if roles:
        try:
            role_list = _parse_roles(roles)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            ctx.exit(2)
            return
        specs = _apply_panel_roles(specs, role_list, resolved_prompt)

    from forge.core.reactive.cost_tracking import (
        resolve_proxy_urls_from_plan,
        track_verb_cost,
    )
    from forge.review.engine import run_multi_review
    from forge.review.routing import resolve_invocation_routing

    try:
        routing_plan = resolve_invocation_routing(specs, via=via)
    except _ROUTING_ERRORS as e:
        _handle_routing_error(e, json_output=json_output)
        return

    _run_preflight(specs, json_output=json_output, routing_plan=routing_plan)

    with track_verb_cost("panel", resolve_proxy_urls_from_plan(routing_plan)):
        output = run_multi_review(
            resolved_prompt,
            models=specs,
            routing_plan=routing_plan,
            timeout_seconds=timeout,
            cwd=cwd or str(Path.cwd()),
            resume_id=resume_id,
        )

    _handle_review_output(
        ctx,
        output,
        check_mode=check_mode,
        json_output=json_output,
        routing_warnings=_routing_plan_warnings(specs, routing_plan),
    )


def _resolve_panel_prompt(
    target: tuple[str, ...],
    prompt: str | None,
    code_mode: bool,
    review_type: str = "full",
) -> str | None:
    """Resolve prompt for panel command. Priority: -p > target+framework > stdin.

    When -p is provided, review_type is ignored (custom prompt overrides).
    """
    if prompt:
        return prompt

    resolved_target = " ".join(target) if target else None
    if resolved_target:
        resource_name = _load_review_resource_name(code_mode, review_type)
        framework = _load_workflow_resource(resource_name)
        return f"{framework}\n\n---\n\n## Review Target\n\n{resolved_target}\n"

    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        return text if text else None
    return None


# Review-type to resource file mapping
_CODE_REVIEW_RESOURCES = {
    "full": "codereview.md",
    "security": "codereview-security.md",
    "performance": "codereview-performance.md",
    "quick": "codereview-quick.md",
}

_DOC_REVIEW_RESOURCES = {
    "full": "docreview.md",
    "quick": "docreview-quick.md",
}


def _load_review_resource_name(code_mode: bool, review_type: str) -> str:
    """Map code_mode + review_type to a resource file name.

    Falls back to the full resource if the variant doesn't exist.
    """
    resources = _CODE_REVIEW_RESOURCES if code_mode else _DOC_REVIEW_RESOURCES
    return resources.get(review_type, resources["full"])


def _parse_roles(roles_str: str) -> list[str]:
    """Parse and validate comma-separated role names.

    Raises ValueError for unknown or empty roles.
    """
    roles = [r.strip() for r in roles_str.split(",") if r.strip()]
    if not roles:
        raise ValueError("No roles specified. Provide comma-separated role names.")
    invalid = [r for r in roles if r not in NAMED_ROLES]
    if invalid:
        available = sorted(NAMED_ROLES.keys())
        raise ValueError(f"Unknown roles: {invalid}. Available: {available}")
    return roles


def _apply_panel_roles(
    specs: list[ModelSpec],
    roles: list[str],
    base_prompt: str,
) -> list[ModelSpec]:
    """Create per-worker specs with role-prefixed prompts.

    Roles cycle across models when fewer roles than models.
    Uses dataclasses.replace() on frozen ModelSpec.
    """
    import dataclasses

    result: list[ModelSpec] = []
    seen: dict[str, int] = {}
    for i, spec in enumerate(specs):
        role_name = roles[i % len(roles)]
        role_prompt = NAMED_ROLES[role_name]
        worker_prompt = f"[ROLE: {role_name}]\n{role_prompt}\n\n{base_prompt}"
        base_id = f"{spec.name}-{role_name}"
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        wid = base_id if count == 0 else f"{base_id}-{count}"
        result.append(
            dataclasses.replace(
                spec,
                prompt=worker_prompt,
                worker_id=wid,
            )
        )
    return result


def _evaluate_verdicts(results: list[ReviewResult]) -> tuple[bool, str]:
    """Evaluate --check gate with fail-closed semantics.

    Every worker must succeed AND emit a parseable verdict. Missing verdicts
    from successful workers count as failures. This is the unified check logic
    shared by both panel and debate --check.

    Returns:
        (passed, reason) where reason is a diagnostic string for the check JSON.
    """
    from forge.core.reactive.structured_output import extract_json_from_response

    if not results:
        return False, "no results"

    verdicts: list[tuple[bool, str]] = []
    for result in results:
        if not result.success:
            verdicts.append((False, f"worker {result.model_name} failed"))
            continue

        parsed = extract_json_from_response(result.stdout)
        if parsed is None or not isinstance(parsed, dict):
            verdicts.append((False, f"worker {result.model_name} emitted no verdict"))
            continue

        if "passed" in parsed:
            v = _coerce_passed(parsed["passed"])
            label = "accepted" if v else "rejected"
            verdicts.append((v, f"worker {result.model_name} {label}"))
        elif "verdict" in parsed:
            v_str = str(parsed["verdict"]).upper()
            v = v_str in _ACCEPTING_VERDICTS
            label = "accepted" if v else "rejected"
            verdicts.append((v, f"worker {result.model_name} {label}"))
        elif "position" in parsed:
            v_str = str(parsed["position"]).upper()
            v = v_str in _ACCEPTING_VERDICTS
            label = "accepted" if v else "rejected"
            verdicts.append((v, f"worker {result.model_name} {label}"))
        else:
            verdicts.append(
                (
                    False,
                    f"worker {result.model_name} emitted JSON without verdict fields",
                )
            )

    if all(v for v, _ in verdicts):
        return True, f"all {len(verdicts)} verdicts accepting"

    # all() was False, so at least one entry has v=False
    for v, reason in verdicts:
        if not v:
            return False, reason

    # Unreachable: the loop above always finds a match when all() is False.
    # Explicit raise instead of a silent fallback string.
    raise AssertionError("unreachable: all() was False but no failing verdict found")


_CONSENSUS_ACCEPTING = frozenset({"SUPPORT", "SUPPORT_WITH_CONDITIONS"})


def _evaluate_consensus_positions(results: list[ReviewResult]) -> tuple[bool, str]:
    """Evaluate consensus --check gate with schema-strict semantics.

    Unlike ``_evaluate_verdicts``, this requires the ``position`` field
    specifically (rejects ``passed``/``verdict`` fallbacks) and only
    accepts SUPPORT / SUPPORT_WITH_CONDITIONS.

    Returns:
        (passed, reason) where reason is a diagnostic string for the check JSON.
    """
    from forge.core.reactive.structured_output import extract_json_from_response

    if not results:
        return False, "no results"

    verdicts: list[tuple[bool, str]] = []
    for result in results:
        if not result.success:
            verdicts.append((False, f"worker {result.model_name} failed"))
            continue

        parsed = extract_json_from_response(result.stdout)
        if parsed is None or not isinstance(parsed, dict):
            verdicts.append((False, f"worker {result.model_name} emitted no position"))
            continue

        if "position" not in parsed:
            verdicts.append((False, f"worker {result.model_name} emitted JSON without position field"))
            continue

        v_str = str(parsed["position"]).upper()
        v = v_str in _CONSENSUS_ACCEPTING
        label = "supporting" if v else "opposing"
        verdicts.append((v, f"worker {result.model_name} {label}"))

    if all(v for v, _ in verdicts):
        return True, f"all {len(verdicts)} positions supporting"

    for v, reason in verdicts:
        if not v:
            return False, reason

    raise AssertionError("unreachable: all() was False but no failing position found")


def _build_check_json(
    output: MultiReviewOutput,
    passed: bool,
    reason: str,
    routing_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build JSON output for --check mode with gating fields."""
    from forge.review.synthesis import build_json_dict

    data = build_json_dict(output)
    data["passed"] = passed
    data["check_mode"] = "verdict"
    data["reason"] = reason
    if routing_warnings:
        data["routing_warnings"] = routing_warnings
    return data


def _handle_review_output(
    ctx: click.Context,
    output: MultiReviewOutput,
    *,
    check_mode: bool,
    json_output: bool,
    routing_warnings: list[str] | None = None,
) -> None:
    """Shared output handler for panel-based commands."""
    from forge.review.synthesis import build_json_dict, format_synthesis_prompt

    if check_mode:
        passed, reason = _evaluate_verdicts(output.results)
        data = _build_check_json(output, passed, reason, routing_warnings)
        click.echo(json.dumps(data, indent=2))
        ctx.exit(0 if passed else 1)
        return

    if json_output:
        data = build_json_dict(output)
        if routing_warnings:
            data["routing_warnings"] = routing_warnings
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(format_synthesis_prompt(output))


# --- Analyze subcommand ---


@workflow_cmd.command(name="analyze")
@click.argument("topic", nargs=-1)
@click.option(
    "-p",
    "--prompt",
    "prompt_text",
    type=str,
    default=None,
    help="Topic to analyze (alternative to positional)",
)
@click.option(
    "--models",
    "-m",
    type=str,
    default="claude-opus",
    help="Comma-separated model names (default: claude-opus)",
)
@click.option("--timeout", "-t", type=int, default=600, help="Per-model timeout in seconds")
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON")
@click.option(
    "--check",
    "check_mode",
    is_flag=True,
    help="Gate on verdict: exit 0 if passed, exit 1 if failed",
)
@click.option("--via", type=str, default=None, help="Route proxy-backed workers through this proxy")
@click.option("--cwd", type=click.Path(exists=True), default=None, help="Working directory")
@click.pass_context
def analyze(
    ctx: click.Context,
    topic: tuple[str, ...],
    prompt_text: str | None,
    models: str,
    timeout: int,
    json_output: bool,
    check_mode: bool,
    via: str | None,
    cwd: str | None,
) -> None:
    """Deep structured analysis on a topic (single-model).

    \b
    Examples:
      forge workflow analyze "Should we use event sourcing?"
      forge workflow analyze -p "Evaluate migration strategy" --json
      forge workflow analyze "Architecture review" --check
    """
    resolved_topic = " ".join(topic) if topic else prompt_text
    if not resolved_topic:
        console.print("[red]Error:[/red] No topic provided. Pass as argument or use -p.")
        ctx.exit(2)
        return

    try:
        specs = resolve_model_specs(models)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        ctx.exit(2)
        return

    framework = _load_workflow_resource("thinkdeep.md")
    combined_prompt = f"{framework}\n\n---\n\n## Topic to Analyze\n\n{resolved_topic}\n"

    from forge.core.reactive.cost_tracking import (
        resolve_proxy_urls_from_plan,
        track_verb_cost,
    )
    from forge.review.engine import run_multi_review
    from forge.review.routing import resolve_invocation_routing

    try:
        routing_plan = resolve_invocation_routing(specs, via=via)
    except _ROUTING_ERRORS as e:
        _handle_routing_error(e, json_output=json_output)
        return

    _run_preflight(specs, json_output=json_output, routing_plan=routing_plan)

    with track_verb_cost("analyze", resolve_proxy_urls_from_plan(routing_plan)):
        output = run_multi_review(
            combined_prompt,
            models=specs,
            routing_plan=routing_plan,
            timeout_seconds=timeout,
            cwd=cwd or str(Path.cwd()),
        )

    _handle_review_output(
        ctx,
        output,
        check_mode=check_mode,
        json_output=json_output,
        routing_warnings=_routing_plan_warnings(specs, routing_plan),
    )


# --- Debate subcommand ---

_DEFAULT_PROPOSAL_STANCE_PROMPTS = {
    "for": (
        "You are evaluating this proposal as a SUPPORTER. "
        "Identify strengths, viable implementation paths, and reasons to proceed. "
        "Acknowledge genuine weaknesses but focus on how they can be addressed."
    ),
    "against": (
        "You are evaluating this proposal as a CRITIC. "
        "Attack on these specific vectors: "
        "(1) correctness -- are there logical gaps, incorrect assumptions, or unstated prerequisites? "
        "(2) feasibility -- can this actually be done with the stated constraints and resources? "
        "(3) internal contradictions -- does the proposal contradict itself across sections? "
        "(4) unstated assumptions -- what is being taken for granted without evidence? "
        "(5) alternatives -- are there simpler or better-established approaches being ignored? "
        "Acknowledge genuine strengths but focus relentlessly on potential problems."
    ),
    "neutral": (
        "You are evaluating this proposal as a NEUTRAL ANALYST. "
        "Weigh strengths against weaknesses objectively. "
        "Provide a balanced assessment without advocating for or against."
    ),
}

_DEFAULT_CODE_STANCE_PROMPTS = {
    "for": (
        "You are evaluating this code as a SUPPORTER. "
        "Identify good design, correct implementations, and production readiness. "
        "Acknowledge genuine issues but focus on what works well and why."
    ),
    "against": (
        "You are evaluating this code as a CRITIC. "
        "Attack on these specific vectors: "
        "(1) correctness -- logic errors, edge cases, off-by-one, null handling? "
        "(2) security -- injection, validation gaps, secrets, auth boundaries? "
        "(3) performance -- unnecessary allocations, N+1 patterns, blocking in async? "
        "(4) architecture -- coupling violations, wrong abstraction level, unstable contracts? "
        "(5) test coverage -- are critical paths tested? are failure modes covered? "
        "Acknowledge genuine strengths but focus relentlessly on potential problems."
    ),
    "neutral": (
        "You are evaluating this code as a NEUTRAL ANALYST. "
        "Weigh quality, security, performance, and architecture objectively. "
        "Provide a balanced assessment with specific file:line evidence."
    ),
}

_STANCE_CYCLE = ["for", "against", "neutral"]

# Debate evaluation template (canonical copy in src/skills/debate/resources/debate_evaluation.md).
# Embedded here so the CLI doesn't depend on skill installation.
_DEBATE_EVALUATION_TEMPLATE = """\
# Structured Evaluation

```xml
<role>
You are a technical evaluator performing a structured assessment.
{stance_prompt}
</role>

<behavior>
- Evaluate strictly on technical merits
- Support every claim with evidence or reasoning
- Be specific: cite exact trade-offs, not vague concerns
- Provide a clear verdict with confidence level
</behavior>
```

---

## Proposal Under Evaluation

{proposal}

---

## Evaluation Framework

### 1. Feasibility

- Can this be implemented with the available technology and resources?
- What are the key technical dependencies?
- Are there proven precedents or is this novel?

### 2. Correctness

- Does the proposal solve the stated problem?
- Are there logical gaps or incorrect assumptions?
- Does it handle edge cases and failure modes?

### 3. Trade-offs

- What does this approach gain vs alternatives?
- What does it cost (complexity, performance, maintenance)?
- Are the trade-offs appropriate for the context?

### 4. Risks

- What could go wrong in implementation?
- What could go wrong in production?
- What is the blast radius of failure?

### 5. Completeness

- Are all requirements addressed?
- Are there missing considerations?
- What would need to be added before this is production-ready?

### 6. Alternatives

- What other approaches could solve this problem?
- Why might they be better or worse?

### 7. Recommendation

- Overall verdict: ACCEPT, ACCEPT_WITH_CONDITIONS, or REJECT
- Confidence level: LOW, MEDIUM, HIGH
- Key conditions (if ACCEPT_WITH_CONDITIONS)

---

## Output Format

````xml
<output_format>
Respond with a structured evaluation in JSON:

{
  "verdict": "ACCEPT" | "ACCEPT_WITH_CONDITIONS" | "REJECT",
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "key_findings": [
    {"category": "feasibility|correctness|trade-offs|risks|completeness",
     "finding": "specific finding",
     "severity": "critical|high|medium|low"}
  ],
  "recommendation": "1-2 sentence summary of your recommendation",
  "conditions": ["condition 1", "condition 2"]
}

Wrap the JSON in a ```json code fence.
</output_format>
````
"""

# Code debate evaluation template (canonical copy in src/skills/debate/resources/code_debate_evaluation.md).
# Embedded here so the CLI doesn't depend on skill installation.
_CODE_DEBATE_EVALUATION_TEMPLATE = """\
# Adversarial Code Evaluation

```xml
<role>
You are a senior code evaluator performing a structured adversarial assessment.
{stance_prompt}
You identify bugs, design issues, security concerns, and performance problems.
You provide actionable feedback with specific code references.
</role>

<behavior>
- Read all code in scope before forming opinions
- Cite specific file:line references for every finding
- Evaluate strictly on technical merits
- Support every claim with evidence or reasoning
- Cover ALL files in ONE pass -- do not present partial results
- Be specific: "potential null dereference at auth.py:45" not "might have issues"
- Provide a clear verdict with confidence level
</behavior>

<scope_constraints>
- Review only what's in scope
- Do not expand to adjacent code unless directly affected
- If tests exist for reviewed code, check them for coverage gaps
</scope_constraints>
```

---

## Code Under Evaluation

{target}

---

## Evaluation Framework

### 1. Quality

- Logic errors and edge cases
- Error handling: are errors caught, propagated, and surfaced correctly?
- Type safety: do type annotations match runtime behavior?
- Test coverage: are critical paths tested?

### 2. Security

- Input validation at trust boundaries
- Injection vectors (command, SQL, path traversal)
- Secrets in code or logs
- Authentication and authorization gaps

### 3. Performance

- Unnecessary allocations or copies in hot paths
- N+1 query patterns
- Missing caching where data is reused
- Blocking calls in async contexts

### 4. Architecture

- Component boundaries: is coupling appropriate?
- Dependency direction: do imports flow the right way?
- Abstraction level: is complexity in the right place?
- Interface contracts: are public APIs stable and well-defined?

### 5. Risks

- What could go wrong in production?
- What is the blast radius of failure?
- Missing error recovery or graceful degradation?
- Deployment or migration risks?

### 6. Recommendation

- Overall verdict: ACCEPT, ACCEPT_WITH_CONDITIONS, or REJECT
- Confidence level: LOW, MEDIUM, HIGH
- Key conditions (if ACCEPT_WITH_CONDITIONS)

---

## Output Format

````xml
<output_format>
Respond with a structured evaluation in JSON:

{
  "verdict": "ACCEPT" | "ACCEPT_WITH_CONDITIONS" | "REJECT",
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "key_findings": [
    {"category": "quality|security|performance|architecture|risks",
     "finding": "specific finding with file:line reference",
     "severity": "critical|high|medium|low"}
  ],
  "recommendation": "1-2 sentence summary of your recommendation",
  "conditions": ["condition 1", "condition 2"]
}

Wrap the JSON in a ```json code fence.
</output_format>
````
"""


def _resolve_debate_prompt(
    subject: tuple[str, ...],
    prompt: str | None,
    code_mode: bool,
) -> str | None:
    """Resolve prompt for debate command. Priority: -p > subject+framework > stdin.

    Unlike panel, all inputs are wrapped in a template because the adversarial
    runner requires ``{stance_prompt}`` in the resource file.
    """
    resolved = prompt or (" ".join(subject) if subject else None)
    if not resolved and not sys.stdin.isatty():
        resolved = sys.stdin.read().strip() or None

    if not resolved:
        return None

    if code_mode:
        return _CODE_DEBATE_EVALUATION_TEMPLATE.replace("{target}", resolved)
    return _DEBATE_EVALUATION_TEMPLATE.replace("{proposal}", resolved)


@workflow_cmd.command(name="debate")
@click.argument("subject", nargs=-1)
@click.option(
    "-p",
    "--prompt",
    "prompt_text",
    type=str,
    default=None,
    help="Subject to evaluate (alternative to positional)",
)
@click.option(
    "--code",
    "code_mode",
    is_flag=True,
    help="Use code evaluation framework (default: proposal evaluation)",
)
@click.option(
    "--models",
    "-m",
    type=str,
    default=None,
    help="Comma-separated model names (default: all)",
)
@click.option("--timeout", "-t", type=int, default=600, help="Per-model timeout in seconds")
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON")
@click.option("--check", "check_mode", is_flag=True, help="Gate on verdicts: any REJECT exits 1")
@click.option(
    "--worker",
    "workers",
    multiple=True,
    type=str,
    help='Worker spec: model:stance or model:"custom prompt" (repeatable)',
)
@click.option("--via", type=str, default=None, help="Route proxy-backed workers through this proxy")
@click.option("--cwd", type=click.Path(exists=True), default=None, help="Working directory")
@click.pass_context
def debate(
    ctx: click.Context,
    subject: tuple[str, ...],
    prompt_text: str | None,
    code_mode: bool,
    models: str | None,
    timeout: int,
    json_output: bool,
    check_mode: bool,
    workers: tuple[str, ...],
    via: str | None,
    cwd: str | None,
) -> None:
    """Adversarial evaluation with stance-injected workers.

    Each model receives the evaluation template with its assigned stance prompt
    injected via {stance_prompt} replacement. Models are assigned stances
    cyclically: for, against, neutral.

    Use --worker for explicit model:stance mapping or custom prompts.

    Blinding is mandatory -- workers never see conversation context.

    \b
    Examples:
      forge workflow debate "Should we use event sourcing?" --json
      forge workflow debate src/forge/cli/ --code --check
      forge workflow debate --worker gpt-5.5:for --worker "claude-opus:Focus on security" "proposal"
    """
    from forge.review.adversarial import run_adversarial, validate_resource

    if workers and models:
        console.print("[red]Error:[/red] --worker and --models are mutually exclusive.")
        ctx.exit(2)
        return

    resolved = _resolve_debate_prompt(subject, prompt_text, code_mode)
    if not resolved:
        label = "target" if code_mode else "subject"
        console.print(f"[red]Error:[/red] No {label} provided. Pass as argument or use -p.")
        ctx.exit(2)
        return

    # Write filled evaluation resource to a temp file for the adversarial runner
    tmp_file = None
    try:
        tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp_file.write(resolved)
        tmp_file.close()
        resource_path = tmp_file.name

        try:
            validate_resource(resource_path)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            ctx.exit(2)
            return

        if workers:
            try:
                stances = _parse_worker_specs(workers, code_mode=code_mode)
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                ctx.exit(2)
                return
        else:
            try:
                specs = resolve_model_specs(models)
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                ctx.exit(2)
                return
            stances = _build_stances(specs, code_mode=code_mode)

        from forge.core.reactive.cost_tracking import (
            resolve_proxy_urls_from_plan,
            track_verb_cost,
        )
        from forge.review.routing import resolve_invocation_routing

        stance_models = [s.model for s in stances]
        try:
            routing_plan = resolve_invocation_routing(stance_models, via=via)
        except _ROUTING_ERRORS as e:
            _handle_routing_error(e, json_output=json_output)
            return

        _run_preflight(stance_models, json_output=json_output, routing_plan=routing_plan)

        with track_verb_cost("debate", resolve_proxy_urls_from_plan(routing_plan)):
            output = run_adversarial(
                resource_path,
                stances,
                timeout_seconds=timeout,
                cwd=cwd or str(Path.cwd()),
                routing_plan=routing_plan,
            )
    finally:
        if tmp_file is not None:
            Path(tmp_file.name).unlink(missing_ok=True)

    debate_warnings = _routing_plan_warnings(stance_models, routing_plan)

    if check_mode:
        passed, reason = _evaluate_verdicts(output.results)
        data = _build_adversarial_json(
            output, passed=passed, check_mode_str="verdict", reason=reason, routing_warnings=debate_warnings
        )
        click.echo(json.dumps(data, indent=2))
        ctx.exit(0 if passed else 1)
        return

    if json_output:
        data = _build_adversarial_json(output, routing_warnings=debate_warnings)
        click.echo(json.dumps(data, indent=2))
    else:
        _print_debate_text(output)


def _build_stances(specs: list[ModelSpec], *, code_mode: bool = False) -> list[StanceSpec]:
    """Assign stances cyclically to model specs."""
    prompts = _DEFAULT_CODE_STANCE_PROMPTS if code_mode else _DEFAULT_PROPOSAL_STANCE_PROMPTS
    stances: list[StanceSpec] = []
    for i, spec in enumerate(specs):
        stance = _STANCE_CYCLE[i % len(_STANCE_CYCLE)]
        stances.append(
            StanceSpec(
                stance=stance,
                stance_prompt=prompts[stance],
                model=spec,
            )
        )
    return stances


def _parse_worker_specs(worker_args: tuple[str, ...] | list[str], *, code_mode: bool = False) -> list[StanceSpec]:
    """Parse --worker arguments into StanceSpec list.

    Formats:
        model:stance           — stock stance (for/against/neutral)
        model:custom text      — custom prompt (anything not a known stance)

    Shells strip quotes before Click sees them, so ``model:"Focus on X"``
    arrives as ``model:Focus on X``. The parser treats any RHS that is not
    a known stance name as a custom prompt — no quote detection needed.

    Raises ValueError for unknown models or missing colon.
    """
    from forge.review.models import AVAILABLE_MODELS

    prompts = _DEFAULT_CODE_STANCE_PROMPTS if code_mode else _DEFAULT_PROPOSAL_STANCE_PROMPTS
    stances: list[StanceSpec] = []
    for arg in worker_args:
        if ":" not in arg:
            raise ValueError(f"Invalid --worker '{arg}'. Expected model:stance or model:custom prompt.")

        model_name, rest = arg.split(":", 1)
        model_name = model_name.strip()

        if model_name not in AVAILABLE_MODELS:
            available = list(AVAILABLE_MODELS.keys())
            raise ValueError(f"Unknown model '{model_name}'. Available: {available}")

        spec = AVAILABLE_MODELS[model_name]
        rest = rest.strip()

        # Strip optional surrounding quotes (may survive in some shell contexts)
        if len(rest) >= 2 and rest[0] in ('"', "'") and rest[-1] == rest[0]:
            rest = rest[1:-1]

        if not rest:
            raise ValueError(f"Empty stance/prompt for model '{model_name}'.")

        if rest in prompts:
            stances.append(
                StanceSpec(
                    stance=rest,
                    stance_prompt=prompts[rest],
                    model=spec,
                )
            )
        else:
            # Anything not a known stance is a custom prompt
            label = rest[:30] + ("..." if len(rest) > 30 else "")
            stances.append(
                StanceSpec(
                    stance="custom",
                    stance_prompt=rest,
                    model=spec,
                    display_label=label,
                )
            )

    return stances


def _build_adversarial_json(
    output: AdversarialOutput,
    *,
    passed: bool | None = None,
    check_mode_str: str | None = None,
    reason: str | None = None,
    routing_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build JSON output for adversarial evaluation."""
    data: dict[str, Any] = {
        "resource_path": "(generated)",
        "stances": output.stances,
        "results": {
            r.model_name: {
                "stance": output.stance_map.get(r.model_name, "unknown"),
                "response": r.stdout if r.success else None,
                "error": r.error,
                "duration_seconds": round(r.duration_seconds, 2),
                "success": r.success,
            }
            for r in output.results
        },
        "successful": output.successful,
        "failed": output.failed,
    }
    if passed is not None:
        data["passed"] = passed
    if check_mode_str is not None:
        data["check_mode"] = check_mode_str
    if reason is not None:
        data["reason"] = reason
    if routing_warnings:
        data["routing_warnings"] = routing_warnings
    return data


def _print_debate_text(output: AdversarialOutput) -> None:
    """Print adversarial results as human-readable text."""
    console.print(f"\n[bold]Adversarial Evaluation[/bold] ({len(output.results)} workers)\n")

    for i, result in enumerate(output.results):
        stance = output.stances[i] if i < len(output.stances) else "unknown"
        header = f"[cyan]{result.model_name}[/cyan] ([dim]{stance}[/dim])"
        if result.success:
            console.print(f"--- {header} ---")
            console.print(result.stdout)
            console.print()
        else:
            console.print(f"--- {header} [red]FAILED[/red] ---")
            console.print(f"[red]{result.error}[/red]\n")


# --- Consensus subcommand ---

_PROPOSAL_ROLE_CYCLE = ["architecture", "security", "correctness"]
_CODE_ROLE_CYCLE = ["architecture", "security", "maintainability"]

_CONSENSUS_EVALUATION_TEMPLATE = """\
# Consensus Evaluation

```xml
<role>
You are a technical expert participating in a multi-perspective consensus process.
{role_prompt}
</role>

<behavior>
- Evaluate from your assigned perspective
- Support every claim with evidence or reasoning
- Be specific about trade-offs and constraints
- Identify both strengths and weaknesses from your viewpoint
- Provide a clear position with confidence level
</behavior>
```

---

## Subject Under Evaluation

{subject}

---

## Evaluation Framework

### 1. Assessment from Your Perspective

- What are the key considerations from your assigned viewpoint?
- What risks or opportunities do you see that others might miss?

### 2. Strengths

- What aspects of this proposal align well with your area of focus?

### 3. Concerns

- What issues or risks do you identify from your perspective?
- How severe are they? What is the mitigation path?

### 4. Recommendation

- Your position: SUPPORT, SUPPORT_WITH_CONDITIONS, or OPPOSE
- Confidence level: LOW, MEDIUM, HIGH
- Key conditions (if SUPPORT_WITH_CONDITIONS)

---

## Output Format

````xml
<output_format>
Respond with your assessment in JSON:

{
  "position": "SUPPORT" | "SUPPORT_WITH_CONDITIONS" | "OPPOSE",
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "key_points": [
    {"category": "strength|concern|risk|opportunity",
     "point": "specific finding from your perspective",
     "severity": "critical|high|medium|low"}
  ],
  "recommendation": "1-2 sentence summary from your perspective",
  "conditions": ["condition 1", "condition 2"]
}

Wrap the JSON in a ```json code fence.
</output_format>
````
"""

_CODE_CONSENSUS_EVALUATION_TEMPLATE = """\
# Code Consensus Evaluation

```xml
<role>
You are a senior code evaluator participating in a multi-perspective consensus process.
{role_prompt}
You identify issues and opportunities from your assigned perspective.
You provide actionable feedback with specific code references.
</role>

<behavior>
- Read all code in scope before forming opinions
- Cite specific file:line references for every finding
- Evaluate from your assigned perspective
- Support every claim with evidence or reasoning
- Cover ALL files in ONE pass -- do not present partial results
- Be specific: "potential null dereference at auth.py:45" not "might have issues"
- Provide a clear position with confidence level
</behavior>

<scope_constraints>
- Review only what's in scope
- Do not expand to adjacent code unless directly affected
- If tests exist for reviewed code, check them for coverage gaps
</scope_constraints>
```

---

## Code Under Evaluation

{target}

---

## Evaluation Framework

### 1. Quality

- Logic errors and edge cases
- Error handling: are errors caught, propagated, and surfaced correctly?
- Type safety: do type annotations match runtime behavior?
- Test coverage: are critical paths tested?

### 2. Security

- Input validation at trust boundaries
- Injection vectors (command, SQL, path traversal)
- Secrets in code or logs
- Authentication and authorization gaps

### 3. Performance

- Unnecessary allocations or copies in hot paths
- N+1 query patterns
- Missing caching where data is reused
- Blocking calls in async contexts

### 4. Architecture

- Component boundaries: is coupling appropriate?
- Dependency direction: do imports flow the right way?
- Abstraction level: is complexity in the right place?
- Interface contracts: are public APIs stable and well-defined?

### 5. Recommendation

- Your position: SUPPORT, SUPPORT_WITH_CONDITIONS, or OPPOSE
- Confidence level: LOW, MEDIUM, HIGH
- Key conditions (if SUPPORT_WITH_CONDITIONS)

---

## Output Format

````xml
<output_format>
Respond with your assessment in JSON:

{
  "position": "SUPPORT" | "SUPPORT_WITH_CONDITIONS" | "OPPOSE",
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "key_points": [
    {"category": "quality|security|performance|architecture|maintainability",
     "point": "specific finding with file:line reference",
     "severity": "critical|high|medium|low"}
  ],
  "recommendation": "1-2 sentence summary from your perspective",
  "conditions": ["condition 1", "condition 2"]
}

Wrap the JSON in a ```json code fence.
</output_format>
````
"""


def _resolve_consensus_prompt(
    subject: tuple[str, ...],
    prompt: str | None,
    code_mode: bool,
) -> str | None:
    """Resolve prompt for consensus. Wraps subject in template with {role_prompt} marker."""
    resolved = prompt or (" ".join(subject) if subject else None)
    if not resolved and not sys.stdin.isatty():
        resolved = sys.stdin.read().strip() or None

    if not resolved:
        return None

    if code_mode:
        return _CODE_CONSENSUS_EVALUATION_TEMPLATE.replace("{target}", resolved)
    return _CONSENSUS_EVALUATION_TEMPLATE.replace("{subject}", resolved)


def _build_consensus_roles(
    specs: list[ModelSpec],
    code_mode: bool,
) -> list[RoleSpec]:
    """Assign roles cyclically to model specs. Cycle depends on mode."""
    cycle = _CODE_ROLE_CYCLE if code_mode else _PROPOSAL_ROLE_CYCLE
    role_specs: list[RoleSpec] = []
    for i, spec in enumerate(specs):
        role_name = cycle[i % len(cycle)]
        role_specs.append(
            RoleSpec(
                role=role_name,
                role_prompt=NAMED_ROLES[role_name],
                model=spec,
            )
        )
    return role_specs


def _parse_consensus_worker_specs(
    worker_args: tuple[str, ...] | list[str],
) -> list[RoleSpec]:
    """Parse --worker arguments into RoleSpec list.

    Formats:
        model:role           -- named role (architecture, security, etc.)
        model:custom text    -- custom role prompt

    Raises ValueError for unknown models or missing colon.
    """
    from forge.review.models import AVAILABLE_MODELS

    role_specs: list[RoleSpec] = []
    for arg in worker_args:
        if ":" not in arg:
            raise ValueError(f"Invalid --worker '{arg}'. Expected model:role or model:custom prompt.")

        model_name, rest = arg.split(":", 1)
        model_name = model_name.strip()

        if model_name not in AVAILABLE_MODELS:
            available = list(AVAILABLE_MODELS.keys())
            raise ValueError(f"Unknown model '{model_name}'. Available: {available}")

        spec = AVAILABLE_MODELS[model_name]
        rest = rest.strip()

        # Strip optional surrounding quotes (may survive in some shell contexts)
        if len(rest) >= 2 and rest[0] in ('"', "'") and rest[-1] == rest[0]:
            rest = rest[1:-1]

        if not rest:
            raise ValueError(f"Empty role/prompt for model '{model_name}'.")

        if rest in NAMED_ROLES:
            role_specs.append(RoleSpec(role=rest, role_prompt=NAMED_ROLES[rest], model=spec))
        else:
            label = rest[:30] + ("..." if len(rest) > 30 else "")
            role_specs.append(
                RoleSpec(
                    role="custom",
                    role_prompt=rest,
                    model=spec,
                    display_label=label,
                )
            )

    return role_specs


def _build_consensus_json(
    output: ConsensusOutput,
    *,
    passed: bool | None = None,
    check_mode_str: str | None = None,
    reason: str | None = None,
    routing_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build JSON output for consensus workflow."""
    data: dict[str, Any] = {
        "subject": output.subject,
        "roles": output.roles,
        "role_map": output.role_map,
        "round1": {
            r.model_name: {
                "role": output.role_map.get(r.model_name, "unknown"),
                "response": r.stdout if r.success else None,
                "error": r.error,
                "duration_seconds": round(r.duration_seconds, 2),
                "success": r.success,
            }
            for r in output.round1_results
        },
        "round2": {
            r.model_name: {
                "role": output.role_map.get(r.model_name, "unknown"),
                "response": r.stdout if r.success else None,
                "error": r.error,
                "duration_seconds": round(r.duration_seconds, 2),
                "success": r.success,
            }
            for r in output.round2_results
        },
        "reconciliation_brief": output.reconciliation_brief,
        "successful": output.successful,
        "failed": output.failed,
    }
    if passed is not None:
        data["passed"] = passed
    if check_mode_str is not None:
        data["check_mode"] = check_mode_str
    if reason is not None:
        data["reason"] = reason
    if routing_warnings:
        data["routing_warnings"] = routing_warnings
    return data


def _print_consensus_text(output: ConsensusOutput) -> None:
    """Print consensus results as structured human-readable text."""
    console.print(f"\n[bold]Consensus Workflow[/bold] " f"({len(output.round2_results)} workers, 2 rounds)\n")

    # Round 1 positions (truncated)
    console.print("[dim]Round 1: Initial Positions[/dim]\n")
    for result in output.round1_results:
        role = output.role_map.get(result.model_name, "unknown")
        header = f"[cyan]{result.model_name}[/cyan] ([dim]{role}[/dim])"
        if result.success:
            console.print(f"--- {header} ---")
            excerpt = result.stdout[:500]
            if len(result.stdout) > 500:
                excerpt += "..."
            console.print(excerpt)
            console.print()
        else:
            console.print(f"--- {header} [red]FAILED[/red] ---")
            console.print(f"[red]{result.error}[/red]\n")

    # Reconciliation brief (dimmed)
    console.print("[dim]--- Reconciliation Brief ---[/dim]")
    console.print(f"[dim]{output.reconciliation_brief[:300]}...[/dim]\n")

    # Round 2 recommendations (full)
    console.print("[dim]Round 2: Reconciliation[/dim]\n")
    for result in output.round2_results:
        role = output.role_map.get(result.model_name, "unknown")
        header = f"[cyan]{result.model_name}[/cyan] ([dim]{role}[/dim])"
        if result.success:
            console.print(f"--- {header} ---")
            console.print(result.stdout)
            console.print()
        else:
            console.print(f"--- {header} [red]FAILED[/red] ---")
            console.print(f"[red]{result.error}[/red]\n")

    # Status line (execution status only; actual convergence is in the synthesis)
    completed = sum(1 for r in output.round2_results if r.success)
    total = len(output.round2_results)
    console.print(f"[bold]Completed: {completed}/{total} workers finished reconciliation[/bold]")


@workflow_cmd.command(name="consensus")
@click.argument("subject", nargs=-1)
@click.option(
    "-p",
    "--prompt",
    "prompt_text",
    type=str,
    default=None,
    help="Subject to build consensus on (alternative to positional)",
)
@click.option(
    "--code",
    "code_mode",
    is_flag=True,
    help="Use code evaluation framework (default: proposal evaluation)",
)
@click.option(
    "--models",
    "-m",
    type=str,
    default=None,
    help="Comma-separated model names (default: all)",
)
@click.option(
    "--timeout",
    "-t",
    type=int,
    default=600,
    help="Per-round timeout in seconds (total wall time ~2x for two rounds)",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON")
@click.option(
    "--check",
    "check_mode",
    is_flag=True,
    help="Gate on positions: exit 0 if all supporting, exit 1 otherwise",
)
@click.option(
    "--worker",
    "workers",
    multiple=True,
    type=str,
    help='Worker spec: model:role or model:"custom prompt" (repeatable)',
)
@click.option("--via", type=str, default=None, help="Route proxy-backed workers through this proxy")
@click.option("--cwd", type=click.Path(exists=True), default=None, help="Working directory")
@click.pass_context
def consensus(
    ctx: click.Context,
    subject: tuple[str, ...],
    prompt_text: str | None,
    code_mode: bool,
    models: str | None,
    timeout: int,
    json_output: bool,
    check_mode: bool,
    workers: tuple[str, ...],
    via: str | None,
    cwd: str | None,
) -> None:
    """Two-round consensus building with role-assigned workers.

    Round 1: Each model evaluates the subject from an assigned role
    (architecture, security, etc.) independently.
    Round 2: Each model receives all Round 1 positions and produces
    a reconciled recommendation.

    Default roles: architecture, security, correctness (proposals)
    or architecture, security, maintainability (code).

    \b
    Examples:
      forge workflow consensus "Should we use event sourcing?" --json
      forge workflow consensus src/forge/cli/ --code --check
      forge workflow consensus --worker gpt-5.5:security --worker "claude-opus:Focus on DX" "proposal"
    """
    from forge.review.consensus import run_consensus, validate_resource

    if workers and models:
        console.print("[red]Error:[/red] --worker and --models are mutually exclusive.")
        ctx.exit(2)
        return

    # Resolve raw subject once (positional > -p > stdin) to avoid double-read
    raw_subject = prompt_text or (" ".join(subject) if subject else None)
    if not raw_subject and not sys.stdin.isatty():
        raw_subject = sys.stdin.read().strip() or None

    resolved = _resolve_consensus_prompt((), raw_subject, code_mode)
    if not resolved:
        label = "target" if code_mode else "subject"
        console.print(f"[red]Error:[/red] No {label} provided. Pass as argument or use -p.")
        ctx.exit(2)
        return

    tmp_file = None
    try:
        tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp_file.write(resolved)
        tmp_file.close()
        resource_path = tmp_file.name

        try:
            validate_resource(resource_path)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            ctx.exit(2)
            return

        if workers:
            try:
                role_specs = _parse_consensus_worker_specs(workers)
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                ctx.exit(2)
                return
        else:
            try:
                specs = resolve_model_specs(models)
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                ctx.exit(2)
                return
            role_specs = _build_consensus_roles(specs, code_mode)

        from forge.core.reactive.cost_tracking import (
            resolve_proxy_urls_from_plan,
            track_verb_cost,
        )
        from forge.review.routing import resolve_invocation_routing

        role_models = [r.model for r in role_specs]
        try:
            routing_plan = resolve_invocation_routing(role_models, via=via)
        except _ROUTING_ERRORS as e:
            _handle_routing_error(e, json_output=json_output)
            return

        _run_preflight(role_models, json_output=json_output, routing_plan=routing_plan)

        with track_verb_cost("consensus", resolve_proxy_urls_from_plan(routing_plan)):
            output = run_consensus(
                resource_path,
                role_specs,
                timeout_seconds=timeout,
                cwd=cwd or str(Path.cwd()),
                original_subject=raw_subject or "",
                routing_plan=routing_plan,
            )
    finally:
        if tmp_file is not None:
            Path(tmp_file.name).unlink(missing_ok=True)

    consensus_warnings = _routing_plan_warnings(role_models, routing_plan)

    if check_mode:
        passed, reason = _evaluate_consensus_positions(output.round2_results)
        data = _build_consensus_json(
            output, passed=passed, check_mode_str="position", reason=reason, routing_warnings=consensus_warnings
        )
        click.echo(json.dumps(data, indent=2))
        ctx.exit(0 if passed else 1)
        return

    if json_output:
        data = _build_consensus_json(output, routing_warnings=consensus_warnings)
        click.echo(json.dumps(data, indent=2))
    else:
        _print_consensus_text(output)
