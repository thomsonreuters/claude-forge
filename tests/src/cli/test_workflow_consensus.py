"""Tests for forge workflow consensus CLI subcommand."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.cli.main import main
from forge.core.reactive.routing import ModelRoute, RoutingResult
from forge.review.models import ConsensusOutput, ReviewResult
from forge.review.routing import WorkerRoutingPlan


def _auto_routing_plan(specs, **_kw):
    """Create a valid routing plan for any spec list (test helper)."""
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


@pytest.fixture(autouse=True)
def _skip_preflight(monkeypatch):
    """Bypass preflight in CLI workflow tests (engine is mocked anyway)."""
    monkeypatch.setattr("forge.cli.workflow._run_preflight", lambda *a, **kw: None)


def _mock_consensus_output(
    round1: list[ReviewResult] | None = None,
    round2: list[ReviewResult] | None = None,
) -> ConsensusOutput:
    if round1 is None:
        round1 = [
            ReviewResult("gpt-5.5-architecture", "R1 arch position", "", True, 1.5),
            ReviewResult("gem-security", "R1 security position", "", True, 2.0),
        ]
    if round2 is None:
        round2 = [
            ReviewResult("gpt-5.5-architecture", "R2 reconciled arch", "", True, 3.0),
            ReviewResult("gem-security", "R2 reconciled security", "", True, 3.5),
        ]
    return ConsensusOutput(
        subject="(generated)",
        roles=["architecture", "security"],
        round1_results=round1,
        round2_results=round2,
        role_map={
            "gpt-5.5-architecture": "architecture",
            "gem-security": "security",
        },
        reconciliation_brief="# Round 1 Positions\n## architecture...",
    )


class TestConsensusHelp:
    def test_consensus_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "consensus", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
        assert "--code" in result.output
        assert "--worker" in result.output
        assert "--json" in result.output
        # No --context flag (blinding is mandatory)
        assert "--context" not in result.output

    def test_consensus_in_workflow_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "--help"])
        assert "consensus" in result.output

    def test_timeout_help_mentions_per_round(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "consensus", "--help"])
        assert "per-round" in result.output.lower() or "Per-round" in result.output


class TestConsensusSubject:
    def test_missing_subject_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "consensus"])
        assert result.exit_code == 2
        assert "No subject" in result.output

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_positional_subject(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("m1", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Should we use microservices?", "--json"],
        )
        assert result.exit_code == 0

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_prompt_flag(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("m1", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "-p", "Evaluate this proposal", "--json"],
        )
        assert result.exit_code == 0

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_stdin_subject(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("m1", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "--json"],
            input="stdin proposal\n",
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "round1" in data


class TestConsensusJson:
    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_json_output_has_round1_and_round2(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("m1-architecture", "position", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "test", "--json", "--models", "claude-opus"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "round1" in data
        assert "round2" in data
        assert "reconciliation_brief" in data
        assert "roles" in data
        # Subject should be the actual input, not "(generated)"
        assert data["subject"] == "test"
        assert "role_map" in data

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_json_includes_role_per_result(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("claude-opus-architecture", "pos", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "test", "--json", "--models", "claude-opus"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Each result in round1/round2 should have a "role" field
        for worker_data in data["round1"].values():
            assert "role" in worker_data
        for worker_data in data["round2"].values():
            assert "role" in worker_data


class TestConsensusCode:
    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_code_flag_uses_code_template(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("m1", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "src/forge/", "--code", "--json"],
        )
        assert result.exit_code == 0
        # Round 1 prompt should contain code-specific content
        r1_prompt = mock_run.call_args_list[0][1]["models"][0].prompt
        assert "Code Under Evaluation" in r1_prompt

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_without_code_uses_proposal_template(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("m1", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Should we migrate?", "--json"],
        )
        assert result.exit_code == 0
        r1_prompt = mock_run.call_args_list[0][1]["models"][0].prompt
        assert "Subject Under Evaluation" in r1_prompt


class TestConsensusCheck:
    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_check_pass_with_support(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult(
                    "m1-architecture",
                    '```json\n{"position": "SUPPORT", "confidence": "HIGH"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Test proposal", "--check", "--models", "claude-opus"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
        assert data["check_mode"] == "position"

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_check_reject_with_oppose(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult(
                    "m1-security",
                    '```json\n{"position": "OPPOSE", "reason": "insecure"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Test proposal", "--check", "--models", "claude-opus"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_support_with_conditions_passes(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult(
                    "m1-architecture",
                    '```json\n{"position": "SUPPORT_WITH_CONDITIONS"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Test", "--check", "--models", "claude-opus"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_check_rejects_legacy_passed_field(self, mock_run, _mock_routing):
        """Consensus check must require 'position', not accept 'passed'."""
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult(
                    "m1-architecture",
                    '```json\n{"passed": true}\n```',
                    "",
                    True,
                    1.0,
                ),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Test", "--check", "--models", "claude-opus"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False
        assert "without position" in data["reason"]

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_check_rejects_legacy_verdict_field(self, mock_run, _mock_routing):
        """Consensus check must require 'position', not accept 'verdict'."""
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult(
                    "m1-architecture",
                    '```json\n{"verdict": "ACCEPT"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "Test", "--check", "--models", "claude-opus"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False


class TestConsensusWorkerCli:
    def test_worker_and_models_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "workflow",
                "consensus",
                "test",
                "--worker",
                "claude-opus:security",
                "--models",
                "claude-opus",
            ],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_worker_named_role(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("claude-opus-security", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "test", "--worker", "claude-opus:security", "--json"],
        )
        assert result.exit_code == 0
        # Worker prompt should contain the security role
        specs = mock_run.call_args_list[0][1]["models"]
        assert any("security" in (s.prompt or "").lower() for s in specs)

    @patch("forge.review.routing.resolve_invocation_routing", side_effect=_auto_routing_plan)
    @patch("forge.review.consensus.run_multi_review")
    def test_worker_custom_prompt(self, mock_run, _mock_routing):
        from forge.review.models import MultiReviewOutput

        mock_run.return_value = MultiReviewOutput(
            prompt="",
            results=[
                ReviewResult("claude-opus-compliance", "output", "", True, 1.0),
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "test", "--worker", 'claude-opus:"Focus on compliance"', "--json"],
        )
        assert result.exit_code == 0
        specs = mock_run.call_args_list[0][1]["models"]
        assert any("Focus on compliance" in (s.prompt or "") for s in specs)

    def test_invalid_worker_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "consensus", "test", "--worker", "nonexistent:security"],
        )
        assert result.exit_code == 2
        assert "Unknown model" in result.output


class TestParseConsensusWorkerSpecs:
    def test_named_role(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        result = _parse_consensus_worker_specs(["claude-opus:security"])
        assert len(result) == 1
        assert result[0].role == "security"
        assert result[0].display_label is None
        assert result[0].model.name == "claude-opus"

    def test_custom_prompt(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        result = _parse_consensus_worker_specs(["claude-opus:Focus on DX"])
        assert result[0].role == "custom"
        assert result[0].role_prompt == "Focus on DX"
        assert result[0].display_label is not None

    def test_custom_prompt_with_surviving_quotes(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        result = _parse_consensus_worker_specs(['claude-opus:"Focus on compliance"'])
        assert result[0].role == "custom"
        assert result[0].role_prompt == "Focus on compliance"

    def test_unknown_model_raises(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        with pytest.raises(ValueError, match="Unknown model"):
            _parse_consensus_worker_specs(["nonexistent:security"])

    def test_missing_colon_raises(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        with pytest.raises(ValueError, match="Expected"):
            _parse_consensus_worker_specs(["claude-opus"])

    def test_empty_role_raises(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        with pytest.raises(ValueError, match="Empty"):
            _parse_consensus_worker_specs(["claude-opus:"])

    def test_truncates_display_label(self):
        from forge.cli.workflow import _parse_consensus_worker_specs

        long_prompt = "A" * 50
        result = _parse_consensus_worker_specs([f'claude-opus:"{long_prompt}"'])
        label = result[0].display_label
        assert label is not None
        assert len(label) < len(long_prompt)
        assert label.endswith("...")


class TestBuildConsensusRoles:
    def test_proposal_mode_uses_proposal_cycle(self):
        from forge.cli.workflow import _build_consensus_roles
        from forge.review.models import DEFAULT_MODELS

        specs = list(DEFAULT_MODELS.values())[:3]
        roles = _build_consensus_roles(specs, code_mode=False)
        role_names = [r.role for r in roles]
        assert role_names == ["architecture", "security", "correctness"]

    def test_code_mode_uses_code_cycle(self):
        from forge.cli.workflow import _build_consensus_roles
        from forge.review.models import DEFAULT_MODELS

        specs = list(DEFAULT_MODELS.values())[:3]
        roles = _build_consensus_roles(specs, code_mode=True)
        role_names = [r.role for r in roles]
        assert role_names == ["architecture", "security", "maintainability"]

    def test_cycle_wraps(self):
        from forge.cli.workflow import _build_consensus_roles
        from forge.review.models import DEFAULT_MODELS

        specs = list(DEFAULT_MODELS.values())[:3] + list(DEFAULT_MODELS.values())[:1]
        roles = _build_consensus_roles(specs, code_mode=False)
        assert roles[3].role == "architecture"
