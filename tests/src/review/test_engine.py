"""Tests for forge.review.engine."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge.proxy.proxies import ProxyNotFoundError
from forge.review.engine import preflight_check, run_multi_review
from forge.review.models import DEFAULT_MODELS, ModelAvailability, ModelSpec, PromptMode


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Ensure ANTHROPIC_API_KEY is absent so --bare auto-detect is off by default."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _spec(
    name: str = "test-model",
    proxy: str | None = "test-proxy",
    model_flag: str | None = None,
    prompt: str | None = None,
    prompt_mode: PromptMode = "override",
    direct: bool = False,
    direct_model: str | None = None,
) -> ModelSpec:
    return ModelSpec(
        name=name,
        proxy=proxy,
        model_flag=model_flag,
        description="Test",
        direct=direct,
        direct_model=direct_model,
        prompt=prompt,
        prompt_mode=prompt_mode,
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
    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_single_model_success(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("great review")
        output = run_multi_review("review this", models=[_spec()])
        assert output.successful == 1
        assert output.results[0].success
        assert output.results[0].stdout == "great review"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_multiple_models_parallel(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("output")
        specs = [_spec(f"model-{i}") for i in range(3)]
        output = run_multi_review("review", models=specs)
        assert output.successful == 3
        assert len(output.results) == 3

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_results_in_deterministic_order(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("output")
        specs = [_spec("alpha"), _spec("beta"), _spec("gamma")]
        output = run_multi_review("review", models=specs)
        names = [r.model_name for r in output.results]
        assert names == ["alpha", "beta", "gamma"]

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_duplicate_model_specs_return_one_result_per_input_in_order(self, mock_popen_cls, mock_lookup):
        """Duplicate worker IDs must not overwrite one another in result correlation."""
        mock_popen_cls.side_effect = [_mock_popen("first"), _mock_popen("second")]

        specs = [_spec("same-model"), _spec("same-model")]
        output = run_multi_review("review", models=specs)

        assert len(output.results) == 2
        assert [r.model_name for r in output.results] == ["same-model", "same-model"]
        # Both results must be present (thread scheduling determines which gets which mock)
        assert {r.stdout for r in output.results} == {"first", "second"}

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_model_failure_captured(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen(stdout="", returncode=1, stderr="error msg")
        output = run_multi_review("review", models=[_spec()])
        assert output.failed == 1
        assert output.results[0].error == "error msg"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        side_effect=ProxyNotFoundError("missing-proxy"),
    )
    def test_missing_proxy_skips_model(self, mock_lookup):
        output = run_multi_review("review", models=[_spec(proxy="missing-proxy")])
        assert output.failed == 1
        assert "not found" in output.results[0].error.lower()  # type: ignore[union-attr]

    @patch("forge.review.engine.lookup_proxy_base_url", return_value=None)
    @patch("forge.review.engine.subprocess.Popen")
    def test_direct_model_no_base_url(self, mock_popen_cls, mock_lookup):
        """proxy=None means direct Anthropic — no ANTHROPIC_BASE_URL in env."""
        mock_popen_cls.return_value = _mock_popen("direct output")
        output = run_multi_review("review", models=[_spec(proxy=None)])
        assert output.successful == 1
        # Verify ANTHROPIC_BASE_URL is not in the env
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert "ANTHROPIC_BASE_URL" not in call_kwargs["env"]

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_forge_depth_set_in_env(self, mock_popen_cls, mock_lookup):
        """Child env should have incremented FORGE_DEPTH."""
        mock_popen_cls.return_value = _mock_popen("output")
        with patch.dict("os.environ", {"FORGE_DEPTH": "0"}):
            run_multi_review("review", models=[_spec()])
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert call_kwargs["env"]["FORGE_DEPTH"] == "1"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_bare_flag_when_api_key_present(self, mock_popen_cls, mock_lookup):
        """Review workers include --bare when ANTHROPIC_API_KEY is available."""
        mock_popen_cls.return_value = _mock_popen("output")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            run_multi_review("review", models=[_spec()])
        cmd = mock_popen_cls.call_args[0][0]
        assert "--bare" in cmd

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_bare_flag_skipped_without_api_key(self, mock_popen_cls, mock_lookup):
        """Review workers omit --bare when ANTHROPIC_API_KEY is absent."""
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("review", models=[_spec()])
        cmd = mock_popen_cls.call_args[0][0]
        assert "--bare" not in cmd

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_resume_id_in_command(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("review", models=[_spec()], resume_id="uuid-123")
        cmd = mock_popen_cls.call_args[0][0]
        assert "--resume" in cmd
        assert "uuid-123" in cmd

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_model_flag_in_command(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("review", models=[_spec(model_flag="opus-4-5")])
        cmd = mock_popen_cls.call_args[0][0]
        assert "--model" in cmd
        assert "opus-4-5" in cmd

    @patch("forge.review.engine.lookup_proxy_base_url")
    @patch("forge.review.engine.subprocess.Popen")
    def test_direct_worker_uses_env_pin_not_model_flag(self, mock_popen_cls, mock_lookup, monkeypatch):
        mock_popen_cls.return_value = _mock_popen("output")
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "openrouter")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://inherited:8080")

        run_multi_review(
            "review",
            models=[
                _spec(
                    name="claude-opus-4.7",
                    proxy=None,
                    model_flag="claude-opus-4-7",
                    direct=True,
                    direct_model="claude-opus-4-7",
                )
            ],
        )

        mock_lookup.assert_not_called()
        cmd = mock_popen_cls.call_args[0][0]
        env = mock_popen_cls.call_args.kwargs["env"]
        assert "--model" not in cmd
        assert env["ANTHROPIC_MODEL"] == "opus"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-7"
        assert "ANTHROPIC_BASE_URL" not in env
        assert "FORGE_SUBPROCESS_PROXY" not in env

    def test_direct_worker_requires_direct_model(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        output = run_multi_review(
            "review",
            models=[
                _spec(
                    name="bad-direct",
                    proxy=None,
                    model_flag="opus-4-5",
                    direct=True,
                    direct_model=None,
                )
            ],
        )

        assert output.failed == 1
        assert output.results[0].error == "Direct model spec 'bad-direct' is missing direct_model"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_cwd_passed_through(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("review", models=[_spec()], cwd="/my/project")
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert call_kwargs["cwd"] == "/my/project"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_start_new_session_for_cleanup(self, mock_popen_cls, mock_lookup):
        """Each child should be in its own process group."""
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("review", models=[_spec()])
        call_kwargs = mock_popen_cls.call_args.kwargs
        assert call_kwargs["start_new_session"] is True

    def test_empty_models_returns_empty(self):
        output = run_multi_review("review", models=[])
        assert output.successful == 0
        assert output.results == []

    @patch("forge.review.engine.lookup_proxy_base_url", return_value=None)
    @patch("forge.review.engine.subprocess.Popen")
    def test_defaults_to_all_models(self, mock_popen_cls, mock_lookup):
        """When models=None, uses DEFAULT_MODELS."""
        mock_popen_cls.return_value = _mock_popen("output")
        output = run_multi_review("review")
        assert len(output.results) == len(DEFAULT_MODELS)

    def test_skips_at_max_forge_depth(self):
        """At FORGE_DEPTH >= MAX_DEPTH, returns empty results without spawning."""
        with patch.dict("os.environ", {"FORGE_DEPTH": "2"}):
            output = run_multi_review("review", models=[_spec()])
        assert output.results == []
        assert output.successful == 0

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_runs_below_max_forge_depth(self, mock_popen_cls, mock_lookup):
        """At FORGE_DEPTH < MAX_DEPTH, proceeds normally."""
        mock_popen_cls.return_value = _mock_popen("output")
        with patch.dict("os.environ", {"FORGE_DEPTH": "1"}):
            output = run_multi_review("review", models=[_spec()])
        assert output.successful == 1
        mock_popen_cls.assert_called_once()

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_per_worker_prompt_override(self, mock_popen_cls, mock_lookup):
        """spec.prompt overrides the global prompt."""
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("global prompt", models=[_spec(prompt="worker-specific")])
        communicate_kwargs = mock_popen_cls.return_value.communicate.call_args[1]
        assert communicate_kwargs["input"] == "worker-specific"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_none_prompt_falls_back_to_global(self, mock_popen_cls, mock_lookup):
        """spec.prompt=None uses the global prompt."""
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("global prompt", models=[_spec(prompt=None)])
        communicate_kwargs = mock_popen_cls.return_value.communicate.call_args[1]
        assert communicate_kwargs["input"] == "global prompt"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_prompt_prefix_mode_prepends_hint_to_global_prompt(self, mock_popen_cls, mock_lookup):
        mock_popen_cls.return_value = _mock_popen("output")
        run_multi_review("global prompt", models=[_spec(prompt="worker hint", prompt_mode="prefix")])
        communicate_kwargs = mock_popen_cls.return_value.communicate.call_args[1]
        assert communicate_kwargs["input"] == "worker hint\n\nglobal prompt"

    @patch(
        "forge.review.engine.lookup_proxy_base_url",
        return_value="http://localhost:8085",
    )
    @patch("forge.review.engine.subprocess.Popen")
    def test_mixed_prompts(self, mock_popen_cls, mock_lookup):
        """Some workers use custom prompts, others use global."""
        mock_popen_cls.return_value = _mock_popen("output")
        specs = [_spec("custom", prompt="my custom"), _spec("default", prompt=None)]
        run_multi_review("global prompt", models=specs)
        inputs = {call[1]["input"] for call in mock_popen_cls.return_value.communicate.call_args_list}
        assert inputs == {"my custom", "global prompt"}


def _avail(spec: ModelSpec, status: str = "ready", reason: str = "") -> ModelAvailability:
    return ModelAvailability(spec=spec, status=status, reason=reason)


class TestPreflightCheck:
    """Tests for preflight_check() — delegates to check_model_availability."""

    @patch("forge.review.engine.check_model_availability")
    def test_all_ready_returns_empty(self, mock_avail) -> None:
        specs = [_spec("a", proxy="litellm-openai"), _spec("b", proxy="litellm-gemini")]
        mock_avail.return_value = [_avail(s) for s in specs]
        assert preflight_check(specs) == []

    @patch("forge.review.engine.check_model_availability")
    def test_unavailable_proxy_returns_error(self, mock_avail) -> None:
        spec = _spec("a", proxy="litellm-openai")
        mock_avail.return_value = [
            _avail(spec, status="unavailable", reason="Proxy 'litellm-openai' not responding"),
        ]
        errors = preflight_check([spec])
        assert len(errors) == 1
        assert "litellm-openai" in errors[0]
        assert "not responding" in errors[0]

    @patch("forge.review.engine.check_model_availability")
    def test_unavailable_proxy_includes_create_hint(self, mock_avail) -> None:
        spec = _spec("a", proxy="litellm-openai")
        mock_avail.return_value = [
            _avail(spec, status="unavailable", reason="not found"),
        ]
        errors = preflight_check([spec])
        assert "forge proxy create litellm-openai" in errors[0]

    @patch("forge.review.engine.check_model_availability")
    def test_direct_unavailable_includes_auth_hint(self, mock_avail) -> None:
        spec = _spec("opus", proxy=None)
        mock_avail.return_value = [
            _avail(spec, status="unavailable", reason="ANTHROPIC_API_KEY not configured"),
        ]
        errors = preflight_check([spec])
        assert len(errors) == 1
        assert "ANTHROPIC_API_KEY" in errors[0]
        assert "forge auth login" in errors[0]
        assert "forge proxy create" not in errors[0]

    @patch("forge.review.engine.check_model_availability")
    def test_mixed_ready_and_unavailable(self, mock_avail) -> None:
        spec_ok = _spec("a", proxy="litellm-openai")
        spec_bad = _spec("b", proxy="litellm-gemini")
        mock_avail.return_value = [
            _avail(spec_ok),
            _avail(spec_bad, status="unavailable", reason="not found"),
        ]
        errors = preflight_check([spec_ok, spec_bad])
        assert len(errors) == 1
        assert "b" in errors[0]

    @patch("forge.review.engine.check_model_availability")
    def test_error_status_also_reported(self, mock_avail) -> None:
        spec = _spec("a", proxy="broken")
        mock_avail.return_value = [
            _avail(spec, status="error", reason="Registry corrupted"),
        ]
        errors = preflight_check([spec])
        assert len(errors) == 1
        assert "Registry corrupted" in errors[0]


class TestCredentialInjection:
    """Tests for ANTHROPIC_API_KEY injection from credential file into workflow env."""

    @patch("forge.review.engine.lookup_proxy_base_url", return_value=None)
    @patch("forge.review.engine.subprocess.Popen")
    def test_credential_file_key_injected_into_env(
        self, mock_popen_cls, _mock_lookup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_popen_cls.return_value = _mock_popen("output")

        with patch(
            "forge.review.engine.resolve_env_or_credential",
            return_value="sk-from-file",
        ):
            run_multi_review("test", models=[_spec("opus", proxy=None)])

        call_kwargs = mock_popen_cls.call_args[1]
        assert call_kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-from-file"

    @patch("forge.review.engine.lookup_proxy_base_url", return_value=None)
    @patch("forge.review.engine.subprocess.Popen")
    def test_bare_flag_uses_built_env(self, mock_popen_cls, _mock_lookup, monkeypatch: pytest.MonkeyPatch) -> None:
        """--bare should be added when ANTHROPIC_API_KEY is in the built env (not just os.environ)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_popen_cls.return_value = _mock_popen("output")

        with patch(
            "forge.review.engine.resolve_env_or_credential",
            return_value="sk-from-file",
        ):
            run_multi_review("test", models=[_spec("opus", proxy=None)])

        cmd = mock_popen_cls.call_args[0][0]
        assert "--bare" in cmd
