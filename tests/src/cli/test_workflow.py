"""Tests for forge.cli.workflow (forge workflow panel/analyze/debate)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.cli.main import main
from forge.review.models import (
    ModelAvailability,
    ModelSpec,
    MultiReviewOutput,
    ReviewResult,
)


@pytest.fixture(autouse=True)
def _skip_preflight(monkeypatch):
    """Bypass preflight in CLI workflow tests (engine is mocked anyway)."""
    monkeypatch.setattr("forge.cli.workflow._run_preflight", lambda *a, **kw: None)


def _mock_output(
    results: list[ReviewResult] | None = None,
) -> MultiReviewOutput:
    if results is None:
        results = [
            ReviewResult("gpt-5.5", "Good code", "", True, 1.5),
            ReviewResult("gemini-3.1-pro-preview", "Needs work", "", True, 2.0),
        ]
    return MultiReviewOutput(prompt="test prompt", results=results)


class TestRunHelp:
    def test_run_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "--help"])
        assert result.exit_code == 0
        assert "panel" in result.output

    def test_panel_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
        assert "--context" in result.output

    def test_unknown_workflow_exits_error(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "nonexistent-workflow"])
        assert result.exit_code != 0


def _avail_ready(name: str = "test", proxy: str | None = "p") -> ModelAvailability:
    spec = ModelSpec(name=name, proxy=proxy, model_flag=None, description="Test model")
    return ModelAvailability(spec=spec, status="ready", reason="")


def _avail_unavailable(name: str = "test", proxy: str | None = "p", reason: str = "not found") -> ModelAvailability:
    spec = ModelSpec(name=name, proxy=proxy, model_flag=None, description="Test model")
    return ModelAvailability(spec=spec, status="unavailable", reason=reason)


class TestListModels:
    @patch("forge.review.models.check_model_availability")
    def test_list_models_exits_zero(self, mock_avail):
        mock_avail.return_value = [_avail_ready("model-a"), _avail_ready("model-b")]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models"])
        assert result.exit_code == 0
        assert "model-a" in result.output

    @patch("forge.review.models.check_model_availability")
    def test_table_shows_status_column(self, mock_avail):
        mock_avail.return_value = [_avail_ready("model-a")]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models"])
        assert result.exit_code == 0
        assert "Status" in result.output
        assert "ready" in result.output

    @patch("forge.review.models.check_model_availability")
    def test_json_output(self, mock_avail):
        mock_avail.return_value = [_avail_ready("model-a", proxy="litellm-openai")]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "model-a"
        assert data[0]["status"] == "ready"
        assert "proxy" in data[0]

    @patch("forge.review.models.check_model_availability")
    def test_json_mixed_status(self, mock_avail):
        mock_avail.return_value = [
            _avail_ready("model-a"),
            _avail_unavailable("model-b", reason="not responding"),
        ]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--json"])
        data = json.loads(result.output)
        statuses = {d["name"]: d["status"] for d in data}
        assert statuses == {"model-a": "ready", "model-b": "unavailable"}

    @patch("forge.review.models.check_model_availability")
    def test_available_filter_table(self, mock_avail):
        mock_avail.return_value = [
            _avail_ready("model-a"),
            _avail_unavailable("model-b"),
        ]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--available"])
        assert "model-a" in result.output
        assert "model-b" not in result.output

    @patch("forge.review.models.check_model_availability")
    def test_available_filter_json(self, mock_avail):
        mock_avail.return_value = [
            _avail_ready("model-a"),
            _avail_unavailable("model-b"),
        ]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--json", "--available"])
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "model-a"

    @patch("forge.review.models.check_model_availability")
    def test_unavailable_shows_reason_in_json(self, mock_avail):
        mock_avail.return_value = [
            _avail_unavailable("model-b", reason="Proxy 'litellm-gemini' not responding"),
        ]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--json"])
        data = json.loads(result.output)
        assert data[0]["status"] == "unavailable"
        assert "not responding" in data[0]["reason"]

    @patch("forge.review.models.check_model_availability")
    def test_unavailable_shows_in_table(self, mock_avail):
        mock_avail.return_value = [
            _avail_unavailable("model-b", reason="gone"),
        ]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models"])
        assert "unavailable" in result.output

    @patch("forge.review.models.check_model_availability")
    def test_available_no_ready_models_message(self, mock_avail):
        mock_avail.return_value = [_avail_unavailable("model-a")]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--available"])
        assert "No models are currently ready" in result.output

    @patch("forge.review.models.check_model_availability")
    def test_available_no_ready_json_empty(self, mock_avail):
        mock_avail.return_value = [_avail_unavailable("model-a")]
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "list-models", "--json", "--available"])
        data = json.loads(result.output)
        assert data == []


class TestRunPanel:
    @patch("forge.review.engine.run_multi_review")
    def test_prompt_option(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review this"])
        assert result.exit_code == 0

    @patch("forge.review.engine.run_multi_review")
    def test_json_flag(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["successful"] == 2

    @patch("forge.review.engine.run_multi_review")
    def test_target_loads_docreview_framework(self, mock_run):
        """Positional target without --code loads docreview.md framework."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "docs/design.md"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "Document Review" in prompt_arg
        assert "Review Target" in prompt_arg
        assert "docs/design.md" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_target_with_code_flag_loads_codereview_framework(self, mock_run):
        """Positional target with --code loads codereview.md framework."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/main.py", "--code"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "Code Review" in prompt_arg
        assert "Review Target" in prompt_arg
        assert "src/main.py" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_default_models_all(self, mock_run):
        """Default for panel is all models (N=all fan-out)."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        runner.invoke(main, ["workflow", "panel", "src/foo.py", "--code"])
        specs = mock_run.call_args[1]["models"]
        assert len(specs) >= 3

    @patch("forge.review.engine.run_multi_review")
    def test_context_blind_is_default(self, mock_run):
        """Default --context is 'blind' (resume_id=None)."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        runner.invoke(main, ["workflow", "panel", "-p", "Review"])
        assert mock_run.call_args[1]["resume_id"] is None

    @patch("forge.review.engine.run_multi_review")
    def test_context_resume_uuid(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        runner.invoke(
            main,
            ["workflow", "panel", "-p", "Review", "--context", "resume:abc-123"],
        )
        assert mock_run.call_args[1]["resume_id"] == "abc-123"

    def test_no_prompt_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel"])
        assert result.exit_code == 2
        assert "No prompt" in result.output

    def test_invalid_context_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "panel", "-p", "Review", "--context", "invalid"],
        )
        assert result.exit_code == 2
        assert "Invalid --context" in result.output


class TestParseRoles:
    def test_valid_single_role(self):
        from forge.cli.workflow import _parse_roles

        assert _parse_roles("security") == ["security"]

    def test_valid_multiple_roles(self):
        from forge.cli.workflow import _parse_roles

        assert _parse_roles("security,architecture") == ["security", "architecture"]

    def test_strips_whitespace(self):
        from forge.cli.workflow import _parse_roles

        assert _parse_roles(" security , performance ") == ["security", "performance"]

    def test_invalid_role_raises(self):
        import pytest

        from forge.cli.workflow import _parse_roles

        with pytest.raises(ValueError, match="Unknown roles"):
            _parse_roles("invalid_role")

    def test_mixed_valid_invalid_raises(self):
        import pytest

        from forge.cli.workflow import _parse_roles

        with pytest.raises(ValueError, match="Unknown roles"):
            _parse_roles("security,bogus")

    def test_empty_roles_raises(self):
        import pytest

        from forge.cli.workflow import _parse_roles

        with pytest.raises(ValueError, match="No roles"):
            _parse_roles(",")

    def test_whitespace_only_roles_raises(self):
        import pytest

        from forge.cli.workflow import _parse_roles

        with pytest.raises(ValueError, match="No roles"):
            _parse_roles(" ")


class TestApplyPanelRoles:
    def test_assigns_role_prefix_to_prompt(self):
        from forge.cli.workflow import _apply_panel_roles
        from forge.review.models import ModelSpec

        spec = ModelSpec(name="test-model", proxy=None, model_flag=None, description="test")
        result = _apply_panel_roles([spec], ["security"], "base prompt")
        assert len(result) == 1
        assert result[0].prompt is not None
        assert "[ROLE: security]" in result[0].prompt
        assert "base prompt" in result[0].prompt

    def test_sets_worker_id(self):
        from forge.cli.workflow import _apply_panel_roles
        from forge.review.models import ModelSpec

        spec = ModelSpec(name="gpt-5.5", proxy=None, model_flag=None, description="test")
        result = _apply_panel_roles([spec], ["architecture"], "prompt")
        assert result[0].worker_id == "gpt-5.5-architecture"
        assert result[0].effective_worker_id == "gpt-5.5-architecture"

    def test_cycles_roles_across_models(self):
        from forge.cli.workflow import _apply_panel_roles
        from forge.review.models import ModelSpec

        specs = [ModelSpec(name=f"model-{i}", proxy=None, model_flag=None, description="test") for i in range(3)]
        result = _apply_panel_roles(specs, ["security", "architecture"], "prompt")
        assert result[0].worker_id == "model-0-security"
        assert result[1].worker_id == "model-1-architecture"
        assert result[2].worker_id == "model-2-security"

    def test_preserves_original_spec_fields(self):
        from forge.cli.workflow import _apply_panel_roles
        from forge.review.models import ModelSpec

        spec = ModelSpec(name="gpt-5.5", proxy="litellm-openai", model_flag="gpt-5.5", description="test")
        result = _apply_panel_roles([spec], ["correctness"], "prompt")
        assert result[0].name == "gpt-5.5"
        assert result[0].proxy == "litellm-openai"
        assert result[0].model_flag == "gpt-5.5"

    def test_no_collision_same_model_different_roles(self):
        """Same model with different roles gets distinct worker_ids."""
        from forge.cli.workflow import _apply_panel_roles
        from forge.review.models import ModelSpec

        spec = ModelSpec(name="gpt-5.5", proxy=None, model_flag=None, description="test")
        result = _apply_panel_roles([spec, spec], ["security", "architecture"], "prompt")
        ids = [s.effective_worker_id for s in result]
        assert ids[0] != ids[1]
        assert ids == ["gpt-5.5-security", "gpt-5.5-architecture"]

    def test_collision_same_model_same_role_gets_suffix(self):
        """Same model + same role gets index suffix to prevent collision."""
        from forge.cli.workflow import _apply_panel_roles
        from forge.review.models import ModelSpec

        spec = ModelSpec(name="gpt-5.5", proxy=None, model_flag=None, description="test")
        result = _apply_panel_roles([spec, spec], ["security"], "prompt")
        ids = [s.effective_worker_id for s in result]
        assert ids[0] != ids[1]
        assert ids[0] == "gpt-5.5-security"
        assert ids[1] == "gpt-5.5-security-1"


class TestPanelRolesCli:
    @patch("forge.review.engine.run_multi_review")
    def test_roles_flag_applies_role_prompts(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--code", "--roles", "security"])
        assert result.exit_code == 0
        specs = mock_run.call_args[1]["models"]
        assert all(s.prompt is not None for s in specs)
        assert all("[ROLE: security]" in s.prompt for s in specs)

    @patch("forge.review.engine.run_multi_review")
    def test_roles_sets_worker_ids(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--code", "--roles", "security,architecture"])
        assert result.exit_code == 0
        specs = mock_run.call_args[1]["models"]
        ids = [s.effective_worker_id for s in specs]
        assert "security" in ids[0]
        assert "architecture" in ids[1]

    def test_invalid_role_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--code", "--roles", "bogus"])
        assert result.exit_code == 2
        assert "Unknown roles" in result.output

    @patch("forge.review.engine.run_multi_review")
    def test_roles_with_custom_prompt(self, mock_run):
        """--roles + -p: role prefix prepended to custom prompt."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "My custom prompt", "--roles", "security"])
        assert result.exit_code == 0
        specs = mock_run.call_args[1]["models"]
        assert "[ROLE: security]" in specs[0].prompt
        assert "My custom prompt" in specs[0].prompt


class TestLoadReviewResourceName:
    def test_full_code_returns_codereview(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=True, review_type="full") == "codereview.md"

    def test_full_doc_returns_docreview(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=False, review_type="full") == "docreview.md"

    def test_security_code_returns_security_resource(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=True, review_type="security") == "codereview-security.md"

    def test_performance_code_returns_performance_resource(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=True, review_type="performance") == "codereview-performance.md"

    def test_quick_code_returns_quick_code_resource(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=True, review_type="quick") == "codereview-quick.md"

    def test_quick_doc_returns_quick_doc_resource(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=False, review_type="quick") == "docreview-quick.md"

    def test_unknown_type_falls_back_to_full(self):
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=True, review_type="unknown") == "codereview.md"

    def test_security_doc_falls_back_to_full_doc(self):
        """security is code-only; doc mode falls back to full."""
        from forge.cli.workflow import _load_review_resource_name

        assert _load_review_resource_name(code_mode=False, review_type="security") == "docreview.md"


class TestReviewTypeCli:
    def test_security_without_code_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--review-type", "security"])
        assert result.exit_code == 2
        assert "requires --code" in result.output

    def test_performance_without_code_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--review-type", "performance"])
        assert result.exit_code == 2
        assert "requires --code" in result.output

    @patch("forge.review.engine.run_multi_review")
    def test_security_with_code_loads_security_resource(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--code", "--review-type", "security"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "Security" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_quick_doc_mode_loads_quick_resource(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "docs/", "--review-type", "quick"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "Quick" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_custom_prompt_overrides_review_type(self, mock_run):
        """-p overrides --review-type; security without --code is allowed when -p is set."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "My custom review", "--review-type", "security"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "My custom review" in prompt_arg
        # Should NOT contain security resource content
        assert "Security-Focused" not in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_stdin_overrides_review_type(self, mock_run):
        """stdin prompt overrides --review-type; security without --code is allowed."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "panel", "--review-type", "security"],
            input="custom review from stdin",
        )
        assert result.exit_code == 0


class TestSeverityCli:
    @patch("forge.review.engine.run_multi_review")
    def test_severity_appends_filter_suffix(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--code", "--severity", "high"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "high-severity" in prompt_arg
        assert "No findings" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_severity_critical(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "src/", "--code", "--severity", "critical"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "critical-severity" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_severity_with_roles_composition_order(self, mock_run):
        """Severity suffix applied before role prefix (composition order)."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "panel", "src/", "--code", "--severity", "high", "--roles", "security"],
        )
        assert result.exit_code == 0
        specs = mock_run.call_args[1]["models"]
        worker_prompt = specs[0].prompt
        assert worker_prompt is not None
        # Role prefix is at the start, severity suffix is embedded in the base prompt
        assert worker_prompt.startswith("[ROLE: security]")
        assert "high-severity" in worker_prompt


class TestRunCheck:
    @patch("forge.review.engine.run_multi_review")
    def test_check_fail_closed_on_no_verdict(self, mock_run):
        """Free-form text without JSON verdict -> fail under fail-closed semantics."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False
        assert data["check_mode"] == "verdict"
        assert "no verdict" in data["reason"]

    @patch("forge.review.engine.run_multi_review")
    def test_check_exit_0_on_verdict_pass(self, mock_run):
        """Workers with structured JSON verdicts -> pass."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "model-a",
                    '```json\n{"passed": true, "findings": []}\n```',
                    "",
                    True,
                    1.0,
                ),
                ReviewResult(
                    "model-b",
                    '```json\n{"verdict": "ACCEPT"}\n```',
                    "",
                    True,
                    1.5,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
        assert data["check_mode"] == "verdict"
        assert "reason" in data

    @patch("forge.review.engine.run_multi_review")
    def test_check_exit_1_on_failure(self, mock_run):
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult("model-a", "", "error", False, 1.0, error="failed"),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False

    @patch("forge.review.engine.run_multi_review")
    def test_check_exit_1_on_zero_results(self, mock_run):
        """No results -> not passed (no evidence of success)."""
        mock_run.return_value = _mock_output(results=[])
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False

    @patch("forge.review.engine.run_multi_review")
    def test_check_verdict_from_structured_output(self, mock_run):
        """When workers return structured JSON with 'passed' field, use verdict mode."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "model-a",
                    '```json\n{"passed": true, "findings": []}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
        assert data["check_mode"] == "verdict"

    @patch("forge.review.engine.run_multi_review")
    def test_check_verdict_reject(self, mock_run):
        """When any worker verdict rejects, overall check fails."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "model-a",
                    '```json\n{"verdict": "REJECT", "reason": "bugs"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False
        assert data["check_mode"] == "verdict"

    @patch("forge.review.engine.run_multi_review")
    def test_check_json_includes_required_fields(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        data = json.loads(result.output)
        assert "passed" in data
        assert "check_mode" in data
        assert "results" in data
        assert "successful" in data

    @patch("forge.review.engine.run_multi_review")
    def test_check_string_false_is_not_truthy(self, mock_run):
        """Regression: bool('false') is True in Python; _coerce_passed handles this."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "model-a",
                    '```json\n{"passed": "false"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False

    @patch("forge.review.engine.run_multi_review")
    def test_check_accept_with_conditions_passes(self, mock_run):
        """ACCEPT_WITH_CONDITIONS is treated as a pass."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "model-a",
                    '```json\n{"verdict": "ACCEPT_WITH_CONDITIONS", "conditions": ["add tests"]}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "panel", "-p", "Review", "--check"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
        assert data["check_mode"] == "verdict"


class TestRunDebate:
    def test_debate_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "debate", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
        assert "--code" in result.output
        # No --context flag for debate (blinding is mandatory)
        assert "--context" not in result.output

    def test_missing_subject_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "debate"])
        assert result.exit_code == 2
        assert "No subject" in result.output

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_invokes_adversarial(self, mock_run):
        """Debate subcommand delegates to adversarial runner."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Should we use event sourcing?", "--json"],
        )
        assert result.exit_code == 0
        mock_run.assert_called_once()
        # Verify blinding: resume_id must be None
        assert mock_run.call_args[1]["resume_id"] is None

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_check_pass(self, mock_run):
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "m1-for",
                    '```json\n{"verdict": "ACCEPT", "confidence": "HIGH"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Test proposal", "--check"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
        assert data["check_mode"] == "verdict"

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_check_reject(self, mock_run):
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "m1-against",
                    '```json\n{"verdict": "REJECT", "reason": "flawed"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Test proposal", "--check"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_json_includes_stance_per_result(self, mock_run):
        """Each result record should include its stance for JSON consumers."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult("gpt-5.5-for", "analysis", "", True, 1.0),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Test proposal", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["results"]["gpt-5.5-for"]["stance"] == "for"

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_json_resource_path_is_generated(self, mock_run):
        """Debate JSON should emit '(generated)' not a dangling temp path."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult("m1-for", "analysis", "", True, 1.0),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Test proposal", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["resource_path"] == "(generated)"

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_fail_closed_on_missing_verdict(self, mock_run):
        """Debate fails when a successful worker doesn't emit a verdict."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult("m1-for", "Just some free text, no JSON verdict", "", True, 1.0),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Test proposal", "--check"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False

    @patch("forge.review.adversarial.run_multi_review")
    def test_debate_accept_with_conditions(self, mock_run):
        """ACCEPT_WITH_CONDITIONS is treated as a pass in debate."""
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "m1-neutral",
                    '```json\n{"verdict": "ACCEPT_WITH_CONDITIONS", "conditions": ["more tests"]}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Test proposal", "--check"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True


class TestRunDebateCode:
    """Tests for debate --code mode, mirroring TestRunPanel code-mode coverage."""

    @patch("forge.review.adversarial.run_multi_review")
    def test_subject_loads_proposal_framework(self, mock_run):
        """Positional subject without --code loads generic evaluation template."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Should we use event sourcing?", "--json"],
        )
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[1]["models"][0].prompt
        assert "Feasibility" in prompt_arg
        assert "event sourcing" in prompt_arg

    @patch("forge.review.adversarial.run_multi_review")
    def test_subject_with_code_flag_loads_code_framework(self, mock_run):
        """Positional subject with --code loads code evaluation template."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "src/forge/cli/", "--code", "--json"],
        )
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[1]["models"][0].prompt
        assert "Code Under Evaluation" in prompt_arg
        assert "Quality" in prompt_arg
        assert "Security" in prompt_arg
        assert "src/forge/cli/" in prompt_arg

    def test_code_mode_missing_subject_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "debate", "--code"])
        assert result.exit_code == 2
        assert "No target" in result.output

    @patch("forge.review.adversarial.run_multi_review")
    def test_code_mode_check_pass(self, mock_run):
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "m1-for",
                    '```json\n{"verdict": "ACCEPT", "confidence": "HIGH"}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "src/main.py", "--code", "--check"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True

    @patch("forge.review.adversarial.run_multi_review")
    def test_code_mode_json_output(self, mock_run):
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult("gpt-5.5-for", "analysis", "", True, 1.0),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "src/auth/", "--code", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["results"]["gpt-5.5-for"]["stance"] == "for"
        assert data["resource_path"] == "(generated)"

    @patch("forge.review.adversarial.run_multi_review")
    def test_code_mode_default_models_all(self, mock_run):
        """Default for debate --code is all models (N=all adversarial)."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        runner.invoke(main, ["workflow", "debate", "src/foo.py", "--code", "--json"])
        specs = mock_run.call_args[1]["models"]
        assert len(specs) >= 3

    @patch("forge.review.adversarial.run_multi_review")
    def test_without_code_flag_unchanged(self, mock_run):
        """Proposal mode still uses generic evaluation template (regression guard)."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "Should we refactor?", "--json"],
        )
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[1]["models"][0].prompt
        assert "Proposal Under Evaluation" in prompt_arg
        assert "Feasibility" in prompt_arg
        # Code-specific headers should NOT be present
        assert "Code Under Evaluation" not in prompt_arg


class TestParseWorkerSpecs:
    def test_stock_stance(self):
        from forge.cli.workflow import _parse_worker_specs

        result = _parse_worker_specs(["claude-opus:for"])
        assert len(result) == 1
        assert result[0].stance == "for"
        assert result[0].display_label is None
        assert result[0].model.name == "claude-opus"

    def test_custom_prompt_unquoted(self):
        """Shell strips quotes; parser treats non-stance RHS as custom prompt."""
        from forge.cli.workflow import _parse_worker_specs

        result = _parse_worker_specs(["claude-opus:Focus on security"])
        assert len(result) == 1
        assert result[0].stance == "custom"
        assert result[0].stance_prompt == "Focus on security"
        assert result[0].display_label is not None
        assert "security" in result[0].display_label.lower()

    def test_custom_prompt_with_surviving_quotes(self):
        """Quotes that survive shell (e.g., CliRunner) are stripped."""
        from forge.cli.workflow import _parse_worker_specs

        result = _parse_worker_specs(['claude-opus:"Focus on security"'])
        assert result[0].stance == "custom"
        assert result[0].stance_prompt == "Focus on security"

    def test_multiple_workers(self):
        from forge.cli.workflow import _parse_worker_specs

        result = _parse_worker_specs(["claude-opus:for", "claude-opus:against"])
        assert len(result) == 2
        assert result[0].stance == "for"
        assert result[1].stance == "against"

    def test_unknown_model_raises(self):
        import pytest

        from forge.cli.workflow import _parse_worker_specs

        with pytest.raises(ValueError, match="Unknown model"):
            _parse_worker_specs(["nonexistent:for"])

    def test_non_stance_is_custom_prompt(self):
        """Any non-stance RHS is treated as a custom prompt, not an error."""
        from forge.cli.workflow import _parse_worker_specs

        result = _parse_worker_specs(["claude-opus:bogus"])
        assert result[0].stance == "custom"
        assert result[0].stance_prompt == "bogus"

    def test_missing_colon_raises(self):
        import pytest

        from forge.cli.workflow import _parse_worker_specs

        with pytest.raises(ValueError, match="Expected"):
            _parse_worker_specs(["claude-opus"])

    def test_custom_prompt_truncates_display_label(self):
        from forge.cli.workflow import _parse_worker_specs

        long_prompt = "A" * 50
        result = _parse_worker_specs([f'claude-opus:"{long_prompt}"'])
        label = result[0].display_label
        assert label is not None
        assert len(label) < len(long_prompt)
        assert label.endswith("...")


class TestDebateCodeModeStances:
    """Verify mode-specific stance prompt selection."""

    def test_build_stances_proposal_mode_uses_proposal_prompts(self):
        from forge.cli.workflow import _DEFAULT_PROPOSAL_STANCE_PROMPTS, _build_stances
        from forge.review.models import DEFAULT_MODELS

        specs = list(DEFAULT_MODELS.values())[:1]
        result = _build_stances(specs, code_mode=False)
        assert result[0].stance_prompt == _DEFAULT_PROPOSAL_STANCE_PROMPTS["for"]

    def test_build_stances_code_mode_uses_code_prompts(self):
        from forge.cli.workflow import _DEFAULT_CODE_STANCE_PROMPTS, _build_stances
        from forge.review.models import DEFAULT_MODELS

        specs = list(DEFAULT_MODELS.values())[:1]
        result = _build_stances(specs, code_mode=True)
        assert result[0].stance_prompt == _DEFAULT_CODE_STANCE_PROMPTS["for"]

    def test_build_stances_code_mode_critic_differs_from_proposal(self):
        from forge.cli.workflow import _build_stances
        from forge.review.models import DEFAULT_MODELS

        specs = list(DEFAULT_MODELS.values())[:2]
        proposal = _build_stances(specs, code_mode=False)
        code = _build_stances(specs, code_mode=True)
        # Second stance is "against" — prompts should differ
        assert proposal[1].stance_prompt != code[1].stance_prompt

    def test_parse_worker_specs_code_mode_uses_code_prompts(self):
        from forge.cli.workflow import _DEFAULT_CODE_STANCE_PROMPTS, _parse_worker_specs

        result = _parse_worker_specs(["claude-opus:against"], code_mode=True)
        assert result[0].stance_prompt == _DEFAULT_CODE_STANCE_PROMPTS["against"]

    def test_parse_worker_specs_code_mode_custom_prompt_unchanged(self):
        from forge.cli.workflow import _parse_worker_specs

        result = _parse_worker_specs(["claude-opus:Focus on security"], code_mode=True)
        assert result[0].stance == "custom"
        assert result[0].stance_prompt == "Focus on security"


class TestDebateWorkerCli:
    def test_worker_and_models_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "test", "--worker", "claude-opus:for", "--models", "claude-opus"],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    @patch("forge.review.adversarial.run_multi_review")
    def test_worker_flag_routes_to_parse(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "test proposal", "--worker", "claude-opus:for", "--json"],
        )
        assert result.exit_code == 0
        specs = mock_run.call_args[1]["models"]
        # Worker should have "for" stance prompt injected
        assert any("SUPPORTER" in (s.prompt or "") for s in specs)

    @patch("forge.review.adversarial.run_multi_review")
    def test_custom_worker_prompt_injected(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "test proposal", "--worker", 'claude-opus:"Focus on security"', "--json"],
        )
        assert result.exit_code == 0
        specs = mock_run.call_args[1]["models"]
        assert any("Focus on security" in (s.prompt or "") for s in specs)

    def test_invalid_worker_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["workflow", "debate", "test", "--worker", "nonexistent:for"],
        )
        assert result.exit_code == 2
        assert "Unknown model" in result.output


class TestRunAnalyze:
    def test_analyze_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "analyze", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
        assert "--json" in result.output
        assert "--models" in result.output

    def test_missing_topic_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "analyze"])
        assert result.exit_code == 2
        assert "No topic" in result.output

    @patch("forge.review.engine.run_multi_review")
    def test_positional_topic(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "analyze", "Should", "we", "refactor?"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "Should we refactor?" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_prompt_flag(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "analyze", "-p", "Evaluate event sourcing"])
        assert result.exit_code == 0
        prompt_arg = mock_run.call_args[0][0]
        assert "Evaluate event sourcing" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_prompt_includes_framework(self, mock_run):
        """Combined prompt includes the thinkdeep.md framework content."""
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        runner.invoke(main, ["workflow", "analyze", "test topic"])
        prompt_arg = mock_run.call_args[0][0]
        assert "Deep Analysis Framework" in prompt_arg
        assert "Topic to Analyze" in prompt_arg

    @patch("forge.review.engine.run_multi_review")
    def test_default_model_is_claude_opus(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        runner.invoke(main, ["workflow", "analyze", "topic"])
        specs = mock_run.call_args[1]["models"]
        assert len(specs) == 1
        assert specs[0].name == "claude-opus"

    @patch("forge.review.engine.run_multi_review")
    def test_json_output(self, mock_run):
        mock_run.return_value = _mock_output()
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "analyze", "topic", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "results" in data

    @patch("forge.review.engine.run_multi_review")
    def test_check_mode(self, mock_run):
        mock_run.return_value = _mock_output(
            results=[
                ReviewResult(
                    "claude-opus",
                    '```json\n{"passed": true}\n```',
                    "",
                    True,
                    1.0,
                ),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["workflow", "analyze", "topic", "--check"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
