"""Adversarial evaluation runner with stance injection.

Loads a resource containing ``{stance_prompt}``, replaces the marker with
each worker's stance prompt (plus ethical guardrail), and delegates to
``run_multi_review()`` for parallel fan-out.

Mandatory blinding: ``resume_id=None`` is hardcoded. Workers never see
conversation context — they evaluate the resource in isolation.
"""

from __future__ import annotations

from pathlib import Path

from .engine import run_multi_review
from .models import AdversarialOutput, ModelSpec, StanceSpec
from .routing import WorkerRoutingPlan

STANCE_MARKER = "{stance_prompt}"

ETHICAL_GUARDRAIL = (
    "\n\nIMPORTANT: You are participating in a structured evaluation exercise. "
    "Evaluate the proposal on its technical merits. Do not fabricate evidence, "
    "misrepresent facts, or use manipulative reasoning. Your analysis must be "
    "honest and evidence-based regardless of your assigned stance."
)


def validate_resource(resource_path: str) -> str:
    """Load a resource file and verify it contains the stance marker.

    Raises ValueError if the marker is missing.
    """
    content = Path(resource_path).read_text()
    if STANCE_MARKER not in content:
        raise ValueError(f"Resource {resource_path} must contain '{STANCE_MARKER}' marker " "for stance injection.")
    return content


def run_adversarial(
    resource_path: str,
    stances: list[StanceSpec],
    *,
    timeout_seconds: int = 600,
    cwd: str | None = None,
    via: str | None = None,
    routing_plan: WorkerRoutingPlan | None = None,
) -> AdversarialOutput:
    """Run adversarial evaluation with stance-injected workers.

    Each stance's prompt replaces ``{stance_prompt}`` in the resource.
    All workers run blind (no conversation context).

    Args:
        via: Route all workers through this proxy (passed to routing).
            Ignored when routing_plan is provided.
        routing_plan: Pre-resolved routing plan. When provided, skips
            internal routing resolution.

    Raises ValueError if the resource lacks the stance marker.
    """
    from forge.review.routing import resolve_invocation_routing

    template = validate_resource(resource_path)

    specs: list[ModelSpec] = []
    seen: dict[str, int] = {}
    for stance in stances:
        filled = template.replace(
            STANCE_MARKER,
            stance.stance_prompt + ETHICAL_GUARDRAIL,
        )
        label = stance.effective_label
        base_id = f"{stance.model.name}-{label}"
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        worker_id = base_id if count == 0 else f"{base_id}-{count}"
        specs.append(
            ModelSpec(
                name=stance.model.name,
                model_id=stance.model.model_id,
                family=stance.model.family,
                provider_refs=stance.model.provider_refs,
                description=f"{label} stance via {stance.model.name}",
                preferred_proxy=stance.model.preferred_proxy,
                prompt=filled,
                worker_id=worker_id,
            )
        )

    if routing_plan is None:
        routing_plan = resolve_invocation_routing(specs, via=via)

    # Mandatory blinding: resume_id is always None
    output = run_multi_review(
        prompt="",
        models=specs,
        routing_plan=routing_plan,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        resume_id=None,
    )

    stance_map = {spec.effective_worker_id: s.effective_label for spec, s in zip(specs, stances)}

    return AdversarialOutput(
        resource_path=resource_path,
        stances=[s.stance for s in stances],
        results=output.results,
        stance_map=stance_map,
    )
