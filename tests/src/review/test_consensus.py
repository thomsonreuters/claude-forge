"""Tests for forge.review.consensus."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge.core.reactive.routing import ModelRoute, RoutingResult
from forge.review.consensus import (
    CONSENSUS_GUARDRAIL,
    ROLE_MARKER,
    _build_reconciliation_brief,
    run_consensus,
    validate_resource,
)
from forge.review.models import ModelSpec, ReviewResult, RoleSpec
from forge.review.routing import WorkerRoutingPlan


def _auto_plan(specs, **_kw):
    """Create a routing plan that matches any spec list."""
    route = ModelRoute(
        provider="openrouter",
        credential="openrouter",
        family="openai",
        template_id="openrouter-openai",
        template_family="openai",
        model_ref="openai/gpt-5.5",
    )
    results = tuple(
        RoutingResult(
            base_url="http://localhost:8096",
            proxy_id="openrouter-openai",
            template="openrouter-openai",
            source="preferred_proxy",
            route=route,
            credential="openrouter",
        )
        for _ in specs
    )
    return WorkerRoutingPlan(routes=results, resolved_at="2026-05-14T12:00:00Z", via_override=None)


def _spec(name: str = "test-model", proxy: str | None = "test-proxy") -> ModelSpec:
    provider_refs: tuple[tuple[str, str], ...]
    if proxy:
        provider_refs = (("openrouter", f"openai/{name}"),)
    else:
        provider_refs = (("direct", name),)
    return ModelSpec(
        name=name,
        model_id=name,
        family="openai",
        provider_refs=provider_refs,
        description="Test",
        preferred_proxy=proxy,
    )


def _mock_popen(stdout: str = "output", returncode: int = 0):
    proc = MagicMock()
    proc.communicate.return_value = (stdout, "")
    proc.returncode = returncode
    proc.poll.return_value = returncode
    proc.pid = 12345
    return proc


class TestValidateResource:
    def test_accepts_resource_with_marker(self, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate this: {ROLE_MARKER}")
        content = validate_resource(str(resource))
        assert ROLE_MARKER in content

    def test_rejects_resource_without_marker(self, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text("No marker here")
        with pytest.raises(ValueError, match="role_prompt"):
            validate_resource(str(resource))


class TestRoleSpec:
    def test_named_role(self):
        spec = RoleSpec(role="security", role_prompt="Focus on security", model=_spec())
        assert spec.role == "security"
        assert spec.effective_label == "security"

    def test_custom_role(self):
        spec = RoleSpec(
            role="custom",
            role_prompt="Focus on compliance",
            model=_spec(),
            display_label="compliance",
        )
        assert spec.effective_label == "compliance"

    def test_effective_label_falls_back_to_role(self):
        spec = RoleSpec(role="architecture", role_prompt="test", model=_spec())
        assert spec.effective_label == "architecture"


class TestBuildReconciliationBrief:
    def test_includes_all_positions(self):
        results = [
            ReviewResult(model_name="m1-arch", stdout="Position A", stderr="", success=True, duration_seconds=1.0),
            ReviewResult(model_name="m2-sec", stdout="Position B", stderr="", success=True, duration_seconds=1.0),
        ]
        role_map = {"m1-arch": "architecture", "m2-sec": "security"}
        brief = _build_reconciliation_brief(results, role_map)
        assert "architecture perspective" in brief
        assert "security perspective" in brief
        assert "Position A" in brief
        assert "Position B" in brief

    def test_labels_by_role_not_model(self):
        results = [
            ReviewResult(model_name="gpt-5.5-security", stdout="test", stderr="", success=True, duration_seconds=1.0),
        ]
        role_map = {"gpt-5.5-security": "security"}
        brief = _build_reconciliation_brief(results, role_map)
        assert "security perspective" in brief
        assert "gpt-5.5" not in brief

    def test_handles_failed_workers(self):
        results = [
            ReviewResult(
                model_name="m1-arch",
                stdout="",
                stderr="",
                success=False,
                duration_seconds=1.0,
                error="Timeout after 600s",
            ),
        ]
        role_map = {"m1-arch": "architecture"}
        brief = _build_reconciliation_brief(results, role_map)
        assert "failed" in brief
        assert "Timeout after 600s" in brief

    def test_includes_reconciliation_task(self):
        results = [
            ReviewResult(model_name="m1", stdout="test", stderr="", success=True, duration_seconds=1.0),
        ]
        role_map = {"m1": "architecture"}
        brief = _build_reconciliation_brief(results, role_map)
        assert "Reconciliation Task" in brief
        assert "AGREEMENT" in brief
        assert "DISAGREEMENT" in brief
        assert "NO CONSENSUS" in brief

    def test_extracts_json_when_parseable(self):
        json_output = '```json\n{"position": "SUPPORT", "confidence": "HIGH"}\n```'
        results = [
            ReviewResult(model_name="m1", stdout=json_output, stderr="", success=True, duration_seconds=1.0),
        ]
        role_map = {"m1": "architecture"}
        brief = _build_reconciliation_brief(results, role_map)
        assert '"position"' in brief
        assert '"SUPPORT"' in brief

    def test_falls_back_to_truncated_text(self):
        from forge.review.consensus import _MAX_EXCERPT_LEN

        long_text = "A" * (_MAX_EXCERPT_LEN + 100)
        results = [
            ReviewResult(model_name="m1", stdout=long_text, stderr="", success=True, duration_seconds=1.0),
        ]
        role_map = {"m1": "architecture"}
        brief = _build_reconciliation_brief(results, role_map)
        assert "A" * _MAX_EXCERPT_LEN in brief
        # Full text should NOT be present (truncated)
        assert long_text not in brief
        assert "..." in brief


class TestRunConsensus:
    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_replaces_role_marker(self, mock_popen_cls, mock_routing, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}\nEnd.")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="security", role_prompt="Focus on security", model=_spec("m1")),
        ]
        run_consensus(str(resource), roles)

        # Round 1 prompt should have role text, not the marker
        call_args_list = mock_popen_cls.return_value.communicate.call_args_list
        r1_prompt = call_args_list[0][1]["input"]
        assert "Focus on security" in r1_prompt
        assert ROLE_MARKER not in r1_prompt

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_consensus_guardrail_present(self, mock_popen_cls, mock_routing, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="architecture", role_prompt="Focus on arch", model=_spec()),
        ]
        run_consensus(str(resource), roles)

        r1_prompt = mock_popen_cls.return_value.communicate.call_args_list[0][1]["input"]
        assert CONSENSUS_GUARDRAIL in r1_prompt

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_mandatory_blinding_both_rounds(self, mock_popen_cls, mock_routing, tmp_path):
        """resume_id=None for both rounds (mandatory blinding)."""
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="security", role_prompt="test", model=_spec()),
        ]
        run_consensus(str(resource), roles)

        # Two calls to Popen (Round 1 + Round 2), neither should have --resume
        for call in mock_popen_cls.call_args_list:
            cmd = call[0][0]
            assert "--resume" not in cmd

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_worker_names_include_role(self, mock_popen_cls, mock_routing, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="architecture", role_prompt="arch", model=_spec("gpt")),
            RoleSpec(role="security", role_prompt="sec", model=_spec("gem")),
        ]
        output = run_consensus(str(resource), roles)

        r1_names = [r.model_name for r in output.round1_results]
        assert "gpt-architecture" in r1_names
        assert "gem-security" in r1_names

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_round2_receives_reconciliation_brief(self, mock_popen_cls, mock_routing, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen(stdout="Round 1 position")

        roles = [
            RoleSpec(role="architecture", role_prompt="Focus on arch", model=_spec()),
        ]
        output = run_consensus(str(resource), roles)

        # Round 2 prompt (second communicate call) should contain reconciliation content
        r2_prompt = mock_popen_cls.return_value.communicate.call_args_list[1][1]["input"]
        assert "Reconciliation Task" in r2_prompt
        assert "[ROLE: architecture]" in r2_prompt
        assert output.reconciliation_brief != ""

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_output_structure(self, mock_popen_cls, mock_routing, tmp_path):
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="architecture", role_prompt="test", model=_spec("m1")),
            RoleSpec(role="security", role_prompt="test", model=_spec("m2")),
        ]
        output = run_consensus(str(resource), roles, original_subject="Test proposal")

        assert output.roles == ["architecture", "security"]
        assert output.subject == "Test proposal"
        assert len(output.round1_results) == 2
        assert len(output.round2_results) == 2
        assert "m1-architecture" in output.role_map
        assert "m2-security" in output.role_map
        assert output.role_map["m1-architecture"] == "architecture"

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_one_worker_fails_round1(self, mock_popen_cls, mock_routing, tmp_path):
        """One worker fails in Round 1; others succeed. Round 2 still runs."""
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")

        call_count = 0

        def make_popen(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_popen(stdout="", returncode=1)
            return _mock_popen(stdout="Good position")

        mock_popen_cls.side_effect = make_popen

        roles = [
            RoleSpec(role="architecture", role_prompt="test", model=_spec("m1")),
            RoleSpec(role="security", role_prompt="test", model=_spec("m2")),
        ]
        output = run_consensus(str(resource), roles)

        # Round 2 still ran
        assert len(output.round2_results) == 2
        # Brief contains failure note
        assert "failed" in output.reconciliation_brief

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_duplicate_model_role_gets_suffixed_id(self, mock_popen_cls, mock_routing, tmp_path):
        """Same model+role combo gets suffixed worker_id."""
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="security", role_prompt="test", model=_spec("gpt")),
            RoleSpec(role="security", role_prompt="test", model=_spec("gpt")),
        ]
        output = run_consensus(str(resource), roles)

        r1_names = [r.model_name for r in output.round1_results]
        assert "gpt-security" in r1_names
        assert "gpt-security-1" in r1_names

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_custom_role_uses_effective_label_in_roles(self, mock_popen_cls, mock_routing, tmp_path):
        """Custom roles should use display_label, not 'custom', in output.roles."""
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [
            RoleSpec(role="custom", role_prompt="Focus on compliance", model=_spec("m1"), display_label="compliance"),
        ]
        output = run_consensus(str(resource), roles)
        assert output.roles == ["compliance"]
        assert output.role_map["m1-compliance"] == "compliance"

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_plan)
    @patch("forge.review.engine.subprocess.Popen")
    def test_subject_falls_back_to_resource_path(self, mock_popen_cls, mock_routing, tmp_path):
        """When no original_subject, subject should be resource path."""
        resource = tmp_path / "eval.md"
        resource.write_text(f"Evaluate: {ROLE_MARKER}")
        mock_popen_cls.return_value = _mock_popen()

        roles = [RoleSpec(role="security", role_prompt="test", model=_spec())]
        output = run_consensus(str(resource), roles)
        assert output.subject == str(resource)

    def test_incompatible_resource_raises(self, tmp_path):
        resource = tmp_path / "no-marker.md"
        resource.write_text("Just plain text")

        roles = [RoleSpec(role="security", role_prompt="test", model=_spec())]
        with pytest.raises(ValueError, match="role_prompt"):
            run_consensus(str(resource), roles)
