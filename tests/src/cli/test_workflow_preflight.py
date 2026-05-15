"""Tests for workflow preflight output."""

from __future__ import annotations

from unittest.mock import patch

from forge.cli import workflow as workflow_module
from forge.core.reactive.routing import ModelRoute, RoutingResult
from forge.review.models import ModelSpec
from forge.review.routing import WorkerRoutingPlan


def test_run_preflight_prints_routing_warnings() -> None:
    spec = ModelSpec(
        name="gpt-5.5",
        model_id="gpt-5.5",
        family="openai",
        provider_refs=(("openrouter", "openai/gpt-5.5"),),
        description="test",
    )
    route = ModelRoute(
        provider="openrouter",
        credential="openrouter",
        family="openai",
        template_id="openrouter-anthropic",
        template_family="anthropic",
        model_ref="openai/gpt-5.5",
    )
    plan = WorkerRoutingPlan(
        routes=(
            RoutingResult(
                base_url="http://localhost:8095",
                proxy_id="openrouter-anthropic",
                template="openrouter-anthropic",
                source="route_scan",
                route=route,
                credential="openrouter",
                warning="tier overrides may differ",
            ),
        ),
        resolved_at="2026-05-14T12:00:00Z",
        via_override=None,
    )

    with (
        patch("forge.review.engine.preflight_check", return_value=[]),
        patch.object(workflow_module.console, "print") as mock_print,
    ):
        workflow_module._run_preflight([spec], routing_plan=plan)

    printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
    assert "Routing warning" in printed
    assert "gpt-5.5: tier overrides may differ" in printed
