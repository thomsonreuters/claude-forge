"""Two-round consensus workflow with role-assigned workers.

Round 1: Each worker evaluates the subject from their assigned role.
         Blinded (``resume_id=None``). Workers don't see each other.

Round 2: Each worker receives the reconciliation brief (all Round 1
         positions) and produces a reconciled recommendation.
         Still blinded (no conversation context).

Both rounds delegate to ``run_multi_review()`` for parallel fan-out.
"""

from __future__ import annotations

import json
from pathlib import Path

from forge.core.reactive.structured_output import extract_json_from_response

from .engine import run_multi_review
from .models import ConsensusOutput, ModelSpec, RoleSpec
from .routing import WorkerRoutingPlan

ROLE_MARKER = "{role_prompt}"

CONSENSUS_GUARDRAIL = (
    "\n\nIMPORTANT: You are participating in a structured consensus-building exercise. "
    "Provide your honest expert assessment from your assigned perspective. "
    "Support claims with evidence and reasoning. Do not fabricate evidence "
    "or misrepresent trade-offs. When you lack certainty, say so explicitly."
)

_MAX_EXCERPT_LEN = 1500

_ROUND2_OUTPUT_CONTRACT = (
    "\n## Required Output Format\n\n"
    "Respond with your reconciled assessment in JSON wrapped in a ```json code fence:\n\n"
    "```\n"
    "{\n"
    '  "position": "SUPPORT" | "SUPPORT_WITH_CONDITIONS" | "OPPOSE",\n'
    '  "confidence": "LOW" | "MEDIUM" | "HIGH",\n'
    '  "agreements": ["point of agreement 1", ...],\n'
    '  "disagreements": ["unresolved point 1", ...],\n'
    '  "recommendation": "1-2 sentence reconciled recommendation",\n'
    '  "conditions": ["condition 1", ...]\n'
    "}\n"
    "```\n"
)


def validate_resource(resource_path: str) -> str:
    """Load a resource file and verify it contains the role marker.

    Raises ValueError if the marker is missing.
    """
    content = Path(resource_path).read_text()
    if ROLE_MARKER not in content:
        raise ValueError(f"Resource {resource_path} must contain '{ROLE_MARKER}' marker " "for role injection.")
    return content


def _build_reconciliation_brief(
    round1_results: list,
    role_map: dict[str, str],
    original_subject: str = "",
) -> str:
    """Build a structured reconciliation brief from Round 1 positions.

    Each worker's output is labeled by role (not model name) to minimize
    anchoring bias. Parse-resilient: tries JSON extraction with fallback
    to truncated raw text. Includes the original subject and output contract
    so Round 2 workers retain scope and produce parseable output.
    """
    sections: list[str] = []

    if original_subject:
        sections.append(f"# Original Subject\n\n{original_subject}\n")

    sections.append("# Round 1 Positions\n")

    for result in round1_results:
        role = role_map.get(result.model_name, "unknown")
        section = f"## {role} perspective\n\n"

        if not result.success:
            section += f"Status: failed ({result.error})\n"
            sections.append(section)
            continue

        section += "Status: success\n"

        # Try structured extraction; fall back to truncated text
        parsed = extract_json_from_response(result.stdout)
        if parsed is not None:
            section += f"Position: {json.dumps(parsed, indent=2)}\n"
        else:
            excerpt = result.stdout[:_MAX_EXCERPT_LEN]
            if len(result.stdout) > _MAX_EXCERPT_LEN:
                excerpt += "..."
            section += f"Position: {excerpt}\n"

        sections.append(section)

    sections.append(
        "\n---\n\n"
        "# Reconciliation Task\n\n"
        "You have seen all initial positions above. Now:\n\n"
        "1. Identify the specific points of AGREEMENT across perspectives.\n"
        "2. Identify the specific points of DISAGREEMENT.\n"
        "3. For each disagreement, assess which position has stronger evidence.\n"
        "4. Propose a RECONCILED RECOMMENDATION that incorporates the strongest "
        "points from each perspective.\n"
        "5. If genuine consensus is not possible on a point, explicitly state "
        "'NO CONSENSUS' for that point and explain why.\n\n"
        "Maintain your assigned role perspective but be willing to update your "
        "position based on compelling evidence from other perspectives.\n"
    )

    sections.append(_ROUND2_OUTPUT_CONTRACT)

    return "\n".join(sections)


def run_consensus(
    resource_path: str,
    roles: list[RoleSpec],
    *,
    timeout_seconds: int = 600,
    cwd: str | None = None,
    original_subject: str = "",
    via: str | None = None,
    routing_plan: WorkerRoutingPlan | None = None,
) -> ConsensusOutput:
    """Run two-round consensus workflow with role-assigned workers.

    Round 1: Each worker evaluates the subject from their assigned role,
    blinded. Round 2: Each worker receives the reconciliation brief and
    produces a reconciled recommendation, still blinded.

    Args:
        original_subject: The raw subject/target text (before template
            wrapping). Included in the reconciliation brief so Round 2
            workers retain scope context.
        via: Route all workers through this proxy (passed to routing).
            Ignored when routing_plan is provided.
        routing_plan: Pre-resolved routing plan. When provided, skips
            internal routing resolution and reuses the same route decisions
            for both rounds; Round 2 changes prompts but not route-bearing
            model fields or order.

    Raises ValueError if the resource lacks the role marker.
    """
    from forge.review.routing import resolve_invocation_routing

    template = validate_resource(resource_path)

    # --- Build Round 1 specs ---
    specs_r1: list[ModelSpec] = []
    seen: dict[str, int] = {}
    for role_spec in roles:
        filled = template.replace(
            ROLE_MARKER,
            role_spec.role_prompt + CONSENSUS_GUARDRAIL,
        )
        label = role_spec.effective_label
        base_id = f"{role_spec.model.name}-{label}"
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        worker_id = base_id if count == 0 else f"{base_id}-{count}"
        specs_r1.append(
            ModelSpec(
                name=role_spec.model.name,
                model_id=role_spec.model.model_id,
                family=role_spec.model.family,
                provider_refs=role_spec.model.provider_refs,
                description=f"{label} role via {role_spec.model.name}",
                preferred_proxy=role_spec.model.preferred_proxy,
                prompt=filled,
                worker_id=worker_id,
            )
        )

    role_map = {spec.effective_worker_id: r.effective_label for spec, r in zip(specs_r1, roles)}

    plan_r1 = routing_plan if routing_plan is not None else resolve_invocation_routing(specs_r1, via=via)

    # --- Round 1: Independent positions (blinded) ---
    round1_output = run_multi_review(
        prompt="",
        models=specs_r1,
        routing_plan=plan_r1,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        resume_id=None,
    )

    # --- Build reconciliation brief ---
    brief = _build_reconciliation_brief(round1_output.results, role_map, original_subject=original_subject)

    # --- Build Round 2 specs (same worker_ids for correlation) ---
    specs_r2: list[ModelSpec] = []
    for spec_r1, role_spec in zip(specs_r1, roles):
        reconciliation_prompt = f"[ROLE: {role_spec.effective_label}]\n" f"{role_spec.role_prompt}\n\n" f"{brief}"
        specs_r2.append(
            ModelSpec(
                name=spec_r1.name,
                model_id=spec_r1.model_id,
                family=spec_r1.family,
                provider_refs=spec_r1.provider_refs,
                description=f"{role_spec.effective_label} reconciliation via {spec_r1.name}",
                preferred_proxy=spec_r1.preferred_proxy,
                prompt=reconciliation_prompt,
                worker_id=spec_r1.effective_worker_id,
            )
        )

    plan_r2 = routing_plan if routing_plan is not None else resolve_invocation_routing(specs_r2, via=via)

    # --- Round 2: Reconciliation (blinded) ---
    round2_output = run_multi_review(
        prompt="",
        models=specs_r2,
        routing_plan=plan_r2,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        resume_id=None,
    )

    return ConsensusOutput(
        subject=original_subject or resource_path,
        roles=[r.effective_label for r in roles],
        round1_results=round1_output.results,
        round2_results=round2_output.results,
        role_map=role_map,
        reconciliation_brief=brief,
    )
