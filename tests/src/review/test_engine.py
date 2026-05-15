"""Tests for forge.review.engine."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge.core.reactive.routing import ModelRoute, RoutingResult, RoutingSource
from forge.review.engine import preflight_check, run_multi_review
from forge.review.models import ModelAvailability, ModelSpec, PromptMode
from forge.review.routing import WorkerRoutingPlan


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Ensure ANTHROPIC_API_KEY is absent so --bare auto-detect is off by default."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _spec(
    name: str = "test-model",
    family: str = "openai",
    preferred_proxy: str | None = "test-proxy",
    provider_refs: tuple[tuple[str, str], ...] | None = None,
    prompt: str | None = None,
    prompt_mode: PromptMode = "override",
) -> ModelSpec:
    if provider_refs is None:
        if preferred_proxy:
            provider_refs = (("openrouter", f"openai/{name}"),)
        else:
            provider_refs = (("direct", name),)
    return ModelSpec(
        name=name,
        model_id=name,
        family=family,
        provider_refs=provider_refs,
        description="Test",
        preferred_proxy=preferred_proxy,
        prompt=prompt,
        prompt_mode=prompt_mode,
    )


def _route(
    provider: str = "openrouter",
    model_ref: str = "openai/gpt-5.5",
    template_id: str = "openrouter-openai",
    credential: str = "openrouter",
    family: str = "openai",
) -> ModelRoute:
    return ModelRoute(
        provider=provider,
        credential=credential,
        family=family,
        template_id=template_id if provider != "direct" else None,
        template_family=family if provider != "direct" else None,
        model_ref=model_ref,
    )


def _routing_result(
    route: ModelRoute | None = None,
    base_url: str | None = "http://localhost:8096",
    source: RoutingSource = "preferred_proxy",
) -> RoutingResult:
    if route is None:
        route = _route()
    return RoutingResult(
        base_url=base_url,
        proxy_id="test-proxy",
        template="openrouter-openai",
        source=source,
        route=route,
        credential=route.credential if route else None,
    )


def _plan(*results: RoutingResult) -> WorkerRoutingPlan:
    return WorkerRoutingPlan(
        routes=tuple(results),
        resolved_at="2026-05-14T12:00:00Z",
        via_override=None,
    )


def _mock_popen(stdout: str = "review output", returncode: int = 0, stderr: str = ""):
    """Create a mock Popen that returns given output."""
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.poll.return_value = returncode
    proc.pid = 12345
    return proc


class TestRunMultiReview:
    @patch("forge.review.engine.subprocess.Popen")
    def test_single_model_success(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("great review")
        plan = _plan(_routing_result())
        output = run_multi_review("review this", models=[_spec()], routing_plan=plan)
        assert output.successful == 1
        assert output.results[0].success
        assert output.results[0].stdout == "great review"

    @patch("forge.review.engine.subprocess.Popen")
    def test_multiple_models_parallel(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        specs = [_spec(f"model-{i}") for i in range(3)]
        plan = _plan(*[_routing_result() for _ in range(3)])
        output = run_multi_review("review", models=specs, routing_plan=plan)
        assert output.successful == 3
        assert len(output.results) == 3

    @patch("forge.review.engine.subprocess.Popen")
    def test_results_in_deterministic_order(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        specs = [_spec("alpha"), _spec("beta"), _spec("gamma")]
        plan = _plan(*[_routing_result() for _ in range(3)])
        output = run_multi_review("review", models=specs, routing_plan=plan)
        names = [r.model_name for r in output.results]
        assert names == ["alpha", "beta", "gamma"]

    @patch("forge.review.engine.subprocess.Popen")
    def test_duplicate_model_specs_return_one_result_per_input_in_order(self, mock_popen_cls):
        mock_popen_cls.side_effect = [_mock_popen("first"), _mock_popen("second")]
        specs = [_spec("same-model"), _spec("same-model")]
        plan = _plan(*[_routing_result() for _ in range(2)])
        output = run_multi_review("review", models=specs, routing_plan=plan)
        assert len(output.results) == 2
        assert [r.model_name for r in output.results] == ["same-model", "same-model"]
        assert {r.stdout for r in output.results} == {"first", "second"}

    @patch("forge.review.engine.subprocess.Popen")
    def test_model_failure_captured(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen(stdout="", returncode=1, stderr="error msg")
        plan = _plan(_routing_result())
        output = run_multi_review("review", models=[_spec()], routing_plan=plan)
        assert output.failed == 1
        assert output.results[0].error == "error msg"

    @patch("forge.review.engine.subprocess.Popen")
    def test_direct_model_no_base_url(self, mock_popen_cls):
        """Direct route means no ANTHROPIC_BASE_URL in env."""
        mock_popen_cls.return_value = _mock_popen("direct output")
        direct_route = _route(provider="direct", model_ref="claude-opus-4-6")
        direct_result = _routing_result(route=direct_route, base_url=None, source="direct")
        plan = _plan(direct_result)
        output = run_multi_review(
            "review",
            models=[_spec(preferred_proxy=None, provider_refs=(("direct", "claude-opus-4-6"),))],
            routing_plan=plan,
        )
        assert output.successful == 1
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert "ANTHROPIC_BASE_URL" not in call_kwargs["env"]

    @patch("forge.review.engine.subprocess.Popen")
    def test_forge_depth_set_in_env(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        with patch.dict("os.environ", {"FORGE_DEPTH": "0"}):
            run_multi_review("review", models=[_spec()], routing_plan=plan)
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert call_kwargs["env"]["FORGE_DEPTH"] == "1"

    @patch("forge.review.engine.subprocess.Popen")
    def test_bare_flag_when_api_key_present(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            run_multi_review("review", models=[_spec()], routing_plan=plan)
        cmd = mock_popen_cls.call_args[0][0]
        assert "--bare" in cmd

    @patch("forge.review.engine.subprocess.Popen")
    def test_bare_flag_skipped_without_api_key(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review("review", models=[_spec()], routing_plan=plan)
        cmd = mock_popen_cls.call_args[0][0]
        assert "--bare" not in cmd

    @patch("forge.review.engine.subprocess.Popen")
    def test_resume_id_in_command(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review("review", models=[_spec()], routing_plan=plan, resume_id="uuid-123")
        cmd = mock_popen_cls.call_args[0][0]
        assert "--resume" in cmd
        assert "uuid-123" in cmd

    @patch("forge.review.engine.subprocess.Popen")
    def test_model_flag_for_proxied_worker(self, mock_popen_cls):
        """Proxied workers get --model from route.model_ref."""
        mock_popen_cls.return_value = _mock_popen("output")
        route = _route(model_ref="openai/gpt-5.5")
        plan = _plan(_routing_result(route=route))
        run_multi_review("review", models=[_spec()], routing_plan=plan)
        cmd = mock_popen_cls.call_args[0][0]
        assert "--model" in cmd
        assert "openai/gpt-5.5" in cmd

    @patch("forge.review.engine.subprocess.Popen")
    def test_direct_worker_uses_env_pin_not_model_flag(self, mock_popen_cls, monkeypatch):
        mock_popen_cls.return_value = _mock_popen("output")
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "openrouter")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://inherited:8080")

        direct_route = _route(provider="direct", model_ref="claude-opus-4-7")
        direct_result = _routing_result(route=direct_route, base_url=None, source="direct")
        plan = _plan(direct_result)

        run_multi_review(
            "review",
            models=[
                _spec(
                    name="claude-opus-4.7",
                    family="anthropic",
                    preferred_proxy=None,
                    provider_refs=(("direct", "claude-opus-4-7"),),
                )
            ],
            routing_plan=plan,
        )

        cmd = mock_popen_cls.call_args[0][0]
        env = mock_popen_cls.call_args.kwargs["env"]
        assert "--model" not in cmd
        assert env["ANTHROPIC_MODEL"] == "opus"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-7"
        assert "ANTHROPIC_BASE_URL" not in env
        assert "FORGE_SUBPROCESS_PROXY" not in env

    @patch("forge.review.engine.subprocess.Popen")
    def test_cwd_passed_through(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review("review", models=[_spec()], routing_plan=plan, cwd="/my/project")
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert call_kwargs["cwd"] == "/my/project"

    @patch("forge.review.engine.subprocess.Popen")
    def test_start_new_session_for_cleanup(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review("review", models=[_spec()], routing_plan=plan)
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert call_kwargs["start_new_session"] is True

    def test_empty_models_returns_empty(self):
        output = run_multi_review("review", models=[])
        assert output.successful == 0
        assert output.results == []

    def test_skips_at_max_forge_depth(self):
        with patch.dict("os.environ", {"FORGE_DEPTH": "2"}):
            output = run_multi_review("review", models=[_spec()])
        assert output.results == []
        assert output.successful == 0

    @patch("forge.review.engine.subprocess.Popen")
    def test_runs_below_max_forge_depth(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        with patch.dict("os.environ", {"FORGE_DEPTH": "1"}):
            output = run_multi_review("review", models=[_spec()], routing_plan=plan)
        assert output.successful == 1
        mock_popen_cls.assert_called_once()

    @patch("forge.review.engine.subprocess.Popen")
    def test_per_worker_prompt_override(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review("global prompt", models=[_spec(prompt="worker-specific")], routing_plan=plan)
        communicate_kwargs = mock_popen_cls.return_value.communicate.call_args[1]
        assert communicate_kwargs["input"] == "worker-specific"

    @patch("forge.review.engine.subprocess.Popen")
    def test_none_prompt_falls_back_to_global(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review("global prompt", models=[_spec(prompt=None)], routing_plan=plan)
        communicate_kwargs = mock_popen_cls.return_value.communicate.call_args[1]
        assert communicate_kwargs["input"] == "global prompt"

    @patch("forge.review.engine.subprocess.Popen")
    def test_prompt_prefix_mode_prepends_hint_to_global_prompt(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        plan = _plan(_routing_result())
        run_multi_review(
            "global prompt",
            models=[_spec(prompt="worker hint", prompt_mode="prefix")],
            routing_plan=plan,
        )
        communicate_kwargs = mock_popen_cls.return_value.communicate.call_args[1]
        assert communicate_kwargs["input"] == "worker hint\n\nglobal prompt"

    @patch("forge.review.engine.subprocess.Popen")
    def test_mixed_prompts(self, mock_popen_cls):
        mock_popen_cls.return_value = _mock_popen("output")
        specs = [_spec("custom", prompt="my custom"), _spec("default", prompt=None)]
        plan = _plan(*[_routing_result() for _ in range(2)])
        run_multi_review("global prompt", models=specs, routing_plan=plan)
        inputs = {call[1]["input"] for call in mock_popen_cls.return_value.communicate.call_args_list}
        assert inputs == {"my custom", "global prompt"}


def _avail(spec: ModelSpec, status: str = "ready", reason: str = "") -> ModelAvailability:
    return ModelAvailability(spec=spec, status=status, reason=reason)


class TestPreflightCheck:
    """Tests for preflight_check() with routing plan."""

    def test_all_routed_returns_empty(self):
        specs = [_spec("a"), _spec("b")]
        plan = _plan(*[_routing_result() for _ in range(2)])
        assert preflight_check(specs, routing_plan=plan) == []

    def test_unresolved_route_returns_error(self):
        spec = _spec("a")
        unresolved = RoutingResult(
            base_url=None,
            proxy_id=None,
            template=None,
            source="unresolved",
            route=None,
            credential=None,
            warning="No compatible proxy found",
        )
        plan = _plan(unresolved)
        errors = preflight_check([spec], routing_plan=plan)
        assert len(errors) == 1
        assert "a" in errors[0]

    def test_direct_route_requires_anthropic_api_key(self):
        spec = _spec(
            name="claude-opus",
            family="anthropic",
            preferred_proxy=None,
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        direct_route = _route(
            provider="direct",
            credential="anthropic-api",
            family="anthropic",
            model_ref="claude-opus-4-6",
        )
        direct_result = _routing_result(route=direct_route, base_url=None, source="direct")
        plan = _plan(direct_result)

        with patch("forge.review.engine.resolve_env_or_credential", return_value=None):
            errors = preflight_check([spec], routing_plan=plan)

        assert len(errors) == 1
        assert "ANTHROPIC_API_KEY" in errors[0]
        assert "Workflow model 'claude-opus'" in errors[0]

    def test_direct_route_allows_resolved_anthropic_api_key(self):
        spec = _spec(
            name="claude-opus",
            family="anthropic",
            preferred_proxy=None,
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        direct_route = _route(
            provider="direct",
            credential="anthropic-api",
            family="anthropic",
            model_ref="claude-opus-4-6",
        )
        direct_result = _routing_result(route=direct_route, base_url=None, source="direct")
        plan = _plan(direct_result)

        with patch("forge.review.engine.resolve_env_or_credential", return_value="sk-test"):
            errors = preflight_check([spec], routing_plan=plan)

        assert errors == []

    @patch("forge.review.models.check_model_availability")
    def test_fallback_without_plan(self, mock_avail):
        spec = _spec("a")
        mock_avail.return_value = [_avail(spec)]
        assert preflight_check([spec]) == []


class TestCredentialInjection:
    """Tests for ANTHROPIC_API_KEY injection from credential file into workflow env."""

    @patch("forge.review.engine.subprocess.Popen")
    def test_credential_file_key_injected_into_env(self, mock_popen_cls, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_popen_cls.return_value = _mock_popen("output")

        plan = _plan(_routing_result())
        with patch(
            "forge.review.engine.resolve_env_or_credential",
            return_value="sk-from-file",
        ):
            run_multi_review("test", models=[_spec()], routing_plan=plan)

        call_kwargs = mock_popen_cls.call_args[1]
        assert call_kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-from-file"

    @patch("forge.review.engine.subprocess.Popen")
    def test_bare_flag_uses_built_env(self, mock_popen_cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """--bare should be added when ANTHROPIC_API_KEY is in the built env."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_popen_cls.return_value = _mock_popen("output")

        plan = _plan(_routing_result())
        with patch(
            "forge.review.engine.resolve_env_or_credential",
            return_value="sk-from-file",
        ):
            run_multi_review("test", models=[_spec()], routing_plan=plan)

        cmd = mock_popen_cls.call_args[0][0]
        assert "--bare" in cmd
