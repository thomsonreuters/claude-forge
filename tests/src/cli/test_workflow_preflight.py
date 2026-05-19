"""Tests for workflow preflight output."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from forge.cli import workflow as workflow_module
from forge.cli.main import main
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


def _auto_routing_plan(specs, **_kw):
    route = ModelRoute(
        provider="openrouter",
        credential="openrouter",
        family="openai",
        template_id="openrouter-openai",
        template_family="openai",
        model_ref="openai/gpt-5.5",
    )
    return WorkerRoutingPlan(
        routes=tuple(
            RoutingResult(
                base_url="http://localhost:8096",
                proxy_id="openrouter-openai",
                template="openrouter-openai",
                source="preferred_proxy",
                route=route,
                credential="openrouter",
            )
            for _ in specs
        ),
        resolved_at="2026-05-14T12:00:00Z",
        via_override=None,
    )


def test_workflow_json_preflight_reports_missing_claude_cli(monkeypatch):
    monkeypatch.setattr("forge.review.routing.resolve_invocation_routing", _auto_routing_plan)
    monkeypatch.setattr("forge.review.engine.shutil.which", lambda name: None)

    runner = CliRunner()
    with patch("forge.review.engine.run_multi_review") as mock_run:
        result = runner.invoke(
            main,
            [
                "workflow",
                "panel",
                "-p",
                "Review this",
                "--models",
                "deepseek-v4-pro",
                "--json",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert "claude CLI not found in PATH" in data["preflight_errors"][0]
    mock_run.assert_not_called()
