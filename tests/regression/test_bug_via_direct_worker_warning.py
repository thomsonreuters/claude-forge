"""Regression: --via must surface routing warnings in JSON output.

Bug: _routing_plan_warnings() existed but was only shown for non-JSON output
(gated by `if not json_output`). JSON consumers had no way to see that direct
workers (e.g., claude-opus) ignore --via routing.

Fix: Plumb routing_warnings through _handle_review_output, _build_check_json,
and format_json_output. Also update --via help text to say "proxy-backed workers".
"""

from __future__ import annotations

import pytest

from forge.core.reactive.routing import RoutingResult

pytestmark = pytest.mark.regression


def test_bug_via_flag_warns_about_direct_workers() -> None:
    """A direct-only worker in a routing plan should produce a warning."""
    from forge.cli.workflow import _routing_plan_warnings
    from forge.review.models import ModelSpec

    specs = [
        ModelSpec(
            name="claude-opus",
            model_id="claude-opus-4-6",
            family="anthropic",
            provider_refs=(),
            description="Direct Claude",
        ),
    ]

    class FakeRoutingPlan:
        routes = (
            RoutingResult(
                base_url=None,
                proxy_id=None,
                template=None,
                source="direct",
                route=None,
                credential=None,
                warning="Direct worker 'claude-opus' is not routed through --via proxy",
            ),
        )

    warnings = _routing_plan_warnings(specs, FakeRoutingPlan())
    assert len(warnings) == 1
    assert "claude-opus" in warnings[0]


def _make_output():
    from forge.review.models import MultiReviewOutput, ReviewResult

    return MultiReviewOutput(
        prompt="test",
        results=[ReviewResult(model_name="test", stdout="ok", stderr="", success=True, duration_seconds=1.0)],
    )


def test_bug_build_check_json_includes_warnings() -> None:
    """_build_check_json should include routing_warnings when provided."""
    from forge.cli.workflow import _build_check_json

    data = _build_check_json(_make_output(), passed=True, reason="all passed", routing_warnings=["warn1"])
    assert data["routing_warnings"] == ["warn1"]


def test_bug_build_check_json_omits_empty_warnings() -> None:
    """_build_check_json should not include routing_warnings key when empty."""
    from forge.cli.workflow import _build_check_json

    data = _build_check_json(_make_output(), passed=True, reason="all passed")
    assert "routing_warnings" not in data


def _make_adversarial_output():
    from forge.review.models import AdversarialOutput, ReviewResult

    return AdversarialOutput(
        resource_path="(generated)",
        stances=["for"],
        results=[ReviewResult(model_name="test", stdout="ok", stderr="", success=True, duration_seconds=1.0)],
        stance_map={"test": "for"},
    )


def _make_consensus_output():
    from forge.review.models import ConsensusOutput, ReviewResult

    r = ReviewResult(model_name="test", stdout="ok", stderr="", success=True, duration_seconds=1.0)
    return ConsensusOutput(subject="test", round1_results=[r], round2_results=[r], role_map={"test": "analyst"})


def test_bug_build_adversarial_json_includes_warnings() -> None:
    from forge.cli.workflow import _build_adversarial_json

    data = _build_adversarial_json(_make_adversarial_output(), routing_warnings=["warn1"])
    assert data["routing_warnings"] == ["warn1"]


def test_bug_build_adversarial_json_omits_empty_warnings() -> None:
    from forge.cli.workflow import _build_adversarial_json

    data = _build_adversarial_json(_make_adversarial_output())
    assert "routing_warnings" not in data


def test_bug_build_consensus_json_includes_warnings() -> None:
    from forge.cli.workflow import _build_consensus_json

    data = _build_consensus_json(_make_consensus_output(), routing_warnings=["warn1"])
    assert data["routing_warnings"] == ["warn1"]


def test_bug_build_consensus_json_omits_empty_warnings() -> None:
    from forge.cli.workflow import _build_consensus_json

    data = _build_consensus_json(_make_consensus_output())
    assert "routing_warnings" not in data
