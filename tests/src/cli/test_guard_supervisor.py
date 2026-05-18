"""Tests for forge guard supervisor on-demand command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from pytest import fixture

from forge.cli.main import main
from forge.guard.types import PolicyDecision, Violation
from forge.session import IndexStore, SessionStore, create_session_state
from forge.session.models import PolicyIntent, StartedWithProxy


def _seed_duplicate_supervisor_targets(project: Path) -> tuple[Path, Path]:
    index = IndexStore()

    forge_root_a = project
    forge_root_b = project / "nested-project"
    forge_root_b.mkdir(parents=True, exist_ok=True)

    worktree_a = project
    worktree_b = project / "nested-project-checkout"
    worktree_b.mkdir(parents=True, exist_ok=True)

    target_a = create_session_state(
        "shared",
        proxy_template="template-a",
        proxy_base_url="http://localhost:8101",
        worktree_path=str(worktree_a),
    )
    target_a.forge_root = str(forge_root_a)
    target_a.confirmed.claude_session_id = "uuid-alpha"
    target_a.confirmed.started_with_proxy = StartedWithProxy(base_url="http://localhost:8101", template="template-a")
    SessionStore(str(forge_root_a), "shared").write(target_a)

    target_b = create_session_state(
        "shared",
        proxy_template="template-b",
        proxy_base_url="http://localhost:8102",
        worktree_path=str(worktree_b),
    )
    target_b.forge_root = str(forge_root_b)
    target_b.confirmed.claude_session_id = "uuid-beta"
    target_b.confirmed.started_with_proxy = StartedWithProxy(base_url="http://localhost:8102", template="template-b")
    SessionStore(str(forge_root_b), "shared").write(target_b)

    controller = create_session_state(
        "controller",
        proxy_template="controller-template",
        proxy_base_url="http://localhost:8110",
        worktree_path=str(project),
    )
    controller.forge_root = str(forge_root_a)
    SessionStore(str(forge_root_a), "controller").write(controller)

    index.add_session(
        name="shared",
        worktree_path=str(worktree_a),
        project_root=str(project),
        forge_root=str(forge_root_a),
        checkout_root=str(worktree_a),
        relative_path=".",
    )
    index.add_session(
        name="shared",
        worktree_path=str(worktree_b),
        project_root=str(project),
        forge_root=str(forge_root_b),
        checkout_root=str(worktree_b),
        relative_path="nested-project",
    )
    index.add_session(
        name="controller",
        worktree_path=str(project),
        project_root=str(project),
        forge_root=str(forge_root_a),
        checkout_root=str(project),
        relative_path=".",
    )

    return forge_root_a, forge_root_b


def _read_supervisor_resume_id(forge_root: Path, name: str) -> str | None:
    manifest = SessionStore(str(forge_root), name).read()
    policy = manifest.intent.policy
    if policy and policy.supervisor:
        return policy.supervisor.resume_id
    return None


def _set_supervisor_resume_id(forge_root: Path, name: str, resume_id: str) -> None:
    store = SessionStore(str(forge_root), name)

    def _mutate(state) -> None:
        assert state.intent.policy is not None
        assert state.intent.policy.supervisor is not None
        state.intent.policy.supervisor.resume_id = resume_id

    store.update(timeout_s=5.0, mutate=_mutate)


def _apply_supervisor_to_intent(manifest, supervisor) -> None:
    if manifest.intent.policy is None:
        manifest.intent.policy = PolicyIntent(enabled=True, supervisor=supervisor)
        return
    manifest.intent.policy.enabled = True
    manifest.intent.policy.supervisor = supervisor


def _validate_supervisor_target(target: str, forge_root: str | None = None):
    from unittest.mock import MagicMock

    state = MagicMock()
    state.confirmed.started_with_proxy = None  # direct-mode planner
    state.forge_root = forge_root  # used by SupervisorConfig to scope runtime lookups
    return state


def _auto_seed_supervisor_proxy(*args, **kwargs):
    return None


def _hooks_installed(*args, **kwargs):
    return True


def _project_env(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".forge").mkdir()
    monkeypatch.chdir(project)
    return project


@fixture
def temp_guard_env(tmp_path: Path, monkeypatch):
    return _project_env(tmp_path, monkeypatch)


@fixture
def runner() -> CliRunner:
    return CliRunner()


def _allow_decision(**kwargs) -> PolicyDecision:
    return PolicyDecision(decision="allow", policy_id="semantic.supervisor", **kwargs)


def _deny_decision(violations: list[Violation] | None = None, **kwargs) -> PolicyDecision:
    return PolicyDecision(
        decision="deny",
        policy_id="semantic.supervisor",
        violations=violations or [],
        **kwargs,
    )


def _warn_decision(**kwargs) -> PolicyDecision:
    return PolicyDecision(decision="warn", policy_id="semantic.supervisor", **kwargs)


class TestSupervisorHelp:
    def test_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "--help"])
        assert result.exit_code == 0
        assert "--resume-id" in result.output
        assert "--file" in result.output
        assert "--json" in result.output

    def test_missing_resume_id_exits_error(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("pass")
        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f)])
        assert result.exit_code != 0

    def test_missing_file_exits_error(self):
        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-r", "abc-123"])
        assert result.exit_code != 0


class TestSupervisorAligned:
    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_aligned_exits_0(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision()

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        assert result.exit_code == 0

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_aligned_json(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision()

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["passed"] is True
        assert data["clean"] is True
        assert data["final_decision"] == "allow"
        assert data["violations"] == []

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_warn_exits_0(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _warn_decision(warnings=["Possible divergence: minor (confidence: 50%)"])

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        assert result.exit_code == 0


class TestSupervisorDivergent:
    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_deny_exits_1(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _deny_decision(
            violations=[
                Violation(
                    rule_id="semantic.supervisor.alignment",
                    message="Action diverges from plan",
                    severity="high",
                    evidence="wrote code not in plan",
                    suggested_fix="follow the plan",
                    citations=["plan section 3"],
                )
            ]
        )

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        assert result.exit_code == 1

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_deny_json_includes_violations(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _deny_decision(
            violations=[
                Violation(
                    rule_id="semantic.supervisor.alignment",
                    message="Divergent action",
                    severity="high",
                    evidence="wrong code",
                    suggested_fix="fix it",
                )
            ]
        )

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["passed"] is False
        assert data["final_decision"] == "deny"
        assert len(data["violations"]) == 1
        assert data["violations"][0]["severity"] == "high"


class TestSupervisorInfraFailure:
    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_supervisor_error_exits_2(self, mock_invoke, tmp_path):
        """Fail-open allow with infra-failure markers → exit 2."""
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision(warnings=["Supervisor error: exit 1, failing open"])

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        assert result.exit_code == 2

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_supervisor_skipped_exits_2(self, mock_invoke, tmp_path):
        """Supervisor skipped (depth limit) → exit 2."""
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision(warnings=["Supervisor skipped (FORGE_DEPTH limit reached)"])

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        assert result.exit_code == 2

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_infra_failure_json(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision(warnings=["Supervisor error: timeout, failing open"])

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123", "--json"])
        assert result.exit_code == 2
        data = json.loads(result.output)
        assert data["passed"] is False
        assert data["clean"] is False
        assert data["final_decision"] == "error"

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_exception_exits_2(self, mock_invoke, tmp_path):
        """Exception during invocation → exit 2."""
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.side_effect = RuntimeError("connection failed")

        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        assert result.exit_code == 2


class TestSupervisorOptions:
    @patch("forge.install.hooks.has_forge_hook", side_effect=_hooks_installed)
    @patch("forge.guard.semantic.supervisor.apply_supervisor_to_intent", side_effect=_apply_supervisor_to_intent)
    @patch("forge.guard.semantic.supervisor.auto_seed_supervisor_proxy", side_effect=_auto_seed_supervisor_proxy)
    @patch("forge.guard.semantic.supervisor.validate_supervisor_target", side_effect=_validate_supervisor_target)
    def test_supervise_session_uses_current_project_scope(
        self,
        _mock_validate,
        _mock_seed,
        _mock_apply,
        _mock_hook,
        runner: CliRunner,
        temp_guard_env: Path,
    ):
        forge_root_a, _forge_root_b = _seed_duplicate_supervisor_targets(temp_guard_env)

        result = runner.invoke(main, ["guard", "supervise", "shared", "--session", "controller"])

        assert result.exit_code == 0
        assert _read_supervisor_resume_id(forge_root_a, "controller") == "shared"
        assert "Supervisor set to" in result.output

    @patch("forge.install.hooks.has_forge_hook", side_effect=_hooks_installed)
    @patch("forge.guard.semantic.supervisor.apply_supervisor_to_intent", side_effect=_apply_supervisor_to_intent)
    @patch("forge.guard.semantic.supervisor.auto_seed_supervisor_proxy", side_effect=_auto_seed_supervisor_proxy)
    @patch("forge.guard.semantic.supervisor.validate_supervisor_target", side_effect=_validate_supervisor_target)
    def test_supervise_session_validates_in_selected_session_scope(
        self,
        mock_validate,
        _mock_seed,
        _mock_apply,
        _mock_hook,
        runner: CliRunner,
        temp_guard_env: Path,
    ):
        """Validation should use the selected session's forge_root, not CWD."""
        forge_root_a, _forge_root_b = _seed_duplicate_supervisor_targets(temp_guard_env)

        result = runner.invoke(main, ["guard", "supervise", "shared", "--session", "controller"])
        assert result.exit_code == 0

        # validate_supervisor_target must be called with the selected session's
        # forge_root (forge_root_a), not from _resolve_forge_root(cwd) which
        # could differ in cross-worktree scenarios.
        call_kwargs = mock_validate.call_args
        assert call_kwargs is not None
        actual_fr = (
            call_kwargs[1].get("forge_root") or call_kwargs[0][1]
            if len(call_kwargs[0]) > 1
            else call_kwargs[1].get("forge_root")
        )
        assert actual_fr == str(forge_root_a)

    @patch("forge.install.hooks.has_forge_hook", side_effect=_hooks_installed)
    @patch("forge.guard.semantic.supervisor.apply_supervisor_to_intent", side_effect=_apply_supervisor_to_intent)
    @patch("forge.guard.semantic.supervisor.auto_seed_supervisor_proxy", side_effect=_auto_seed_supervisor_proxy)
    @patch("forge.guard.semantic.supervisor.validate_supervisor_target", side_effect=_validate_supervisor_target)
    def test_supervise_show_uses_same_project_target_metadata(
        self,
        _mock_validate,
        _mock_seed,
        _mock_apply,
        _mock_hook,
        runner: CliRunner,
        temp_guard_env: Path,
    ):
        forge_root_a, _forge_root_b = _seed_duplicate_supervisor_targets(temp_guard_env)
        result = runner.invoke(main, ["guard", "supervise", "shared", "--session", "controller"])
        assert result.exit_code == 0

        show_result = runner.invoke(main, ["guard", "supervise", "--session", "controller"])

        assert show_result.exit_code == 0
        assert "Supervisor: [green]shared[/green]" not in show_result.output
        assert "Supervisor: shared" in show_result.output or "Target" in show_result.output
        assert "Claude UUID: uuid-alpha" in show_result.output or "Claude UUID: uuid-alpha..." in show_result.output
        assert "Source model: template-a" in show_result.output

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_proxy_passed_to_config(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision()

        runner = CliRunner()
        runner.invoke(
            main,
            ["guard", "supervisor", "-f", str(f), "-r", "abc-123", "--proxy", "litellm-openai"],
        )
        config = mock_invoke.call_args[0][0]
        assert config.proxy == "litellm-openai"

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_timeout_passed_to_config(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("x = 1")
        mock_invoke.return_value = _allow_decision()

        runner = CliRunner()
        runner.invoke(
            main,
            ["guard", "supervisor", "-f", str(f), "-r", "abc-123", "-t", "90"],
        )
        config = mock_invoke.call_args[0][0]
        assert config.timeout_seconds == 90

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_file_content_in_context(self, mock_invoke, tmp_path):
        f = tmp_path / "src.py"
        f.write_text("def hello(): pass")
        mock_invoke.return_value = _allow_decision()

        runner = CliRunner()
        runner.invoke(main, ["guard", "supervisor", "-f", str(f), "-r", "abc-123"])
        context = mock_invoke.call_args[0][1]
        assert "def hello(): pass" in (context.new_content or "")


# --- Toggle tests for `forge guard supervise --off/--on/--remove/--reload` ---


def _make_supervised_project(project: Path, monkeypatch, *, suspended: bool = False) -> SessionStore:
    """Create a project with a supervised session for toggle tests."""
    from forge.session.models import SupervisorConfig

    monkeypatch.setenv("FORGE_SESSION", "worker")

    manifest = create_session_state(
        "worker",
        proxy_template="test-template",
        proxy_base_url="http://localhost:8080",
        worktree_path=str(project),
    )
    manifest.forge_root = str(project)
    _apply_supervisor_to_intent(
        manifest,
        SupervisorConfig(resume_id="planner", proxy="litellm-openai", suspended=suspended),
    )
    store = SessionStore(str(project), "worker")
    store.write(manifest)
    return store


class TestSuperviseRoutingDisplay:
    """Tests for supervisor routing display in forge guard supervise (show)."""

    def test_show_displays_proxy_routing(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        from forge.session.models import SupervisorConfig

        monkeypatch.setenv("FORGE_SESSION", "worker")
        manifest = create_session_state("worker", worktree_path=str(temp_guard_env))
        manifest.forge_root = str(temp_guard_env)
        _apply_supervisor_to_intent(
            manifest,
            SupervisorConfig(resume_id="planner", proxy="litellm-gemini"),
        )
        SessionStore(str(temp_guard_env), "worker").write(manifest)

        result = runner.invoke(main, ["guard", "supervise"])
        assert result.exit_code == 0
        assert "Routing: proxy: litellm-gemini" in result.output

    def test_show_displays_direct_routing(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        from forge.session.models import SupervisorConfig

        monkeypatch.setenv("FORGE_SESSION", "worker")
        manifest = create_session_state("worker", worktree_path=str(temp_guard_env))
        manifest.forge_root = str(temp_guard_env)
        _apply_supervisor_to_intent(
            manifest,
            SupervisorConfig(resume_id="planner", direct=True),
        )
        SessionStore(str(temp_guard_env), "worker").write(manifest)

        result = runner.invoke(main, ["guard", "supervise"])
        assert result.exit_code == 0
        assert "Routing: direct (no proxy)" in result.output


class TestSuperviseToggle:
    """Tests for forge guard supervise --off/--on/--remove."""

    def test_off_suspends(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        store = _make_supervised_project(temp_guard_env, monkeypatch)
        result = runner.invoke(main, ["guard", "supervise", "--off"])

        assert result.exit_code == 0
        assert "suspended" in result.output.lower()

        updated = store.read()
        assert updated.intent.policy is not None
        assert updated.intent.policy.supervisor is not None
        assert updated.intent.policy.supervisor.suspended is True
        assert updated.intent.policy.supervisor.resume_id == "planner"

    def test_on_resumes(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        store = _make_supervised_project(temp_guard_env, monkeypatch, suspended=True)
        result = runner.invoke(main, ["guard", "supervise", "--on"])

        assert result.exit_code == 0
        assert "resumed" in result.output.lower()

        updated = store.read()
        assert updated.intent.policy is not None
        assert updated.intent.policy.supervisor is not None
        assert updated.intent.policy.supervisor.suspended is False

    def test_remove_clears(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        store = _make_supervised_project(temp_guard_env, monkeypatch)
        result = runner.invoke(main, ["guard", "supervise", "--remove"])

        assert result.exit_code == 0
        assert "removed" in result.output.lower()

        updated = store.read()
        assert updated.intent.policy is not None
        assert updated.intent.policy.supervisor is None

    def test_off_without_supervisor_reports_not_configured(
        self, runner: CliRunner, temp_guard_env: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FORGE_SESSION", "worker")
        manifest = create_session_state("worker", worktree_path=str(temp_guard_env))
        manifest.forge_root = str(temp_guard_env)
        SessionStore(str(temp_guard_env), "worker").write(manifest)

        result = runner.invoke(main, ["guard", "supervise", "--off"])
        assert result.exit_code == 0
        assert "no supervisor configured" in result.output.lower()

    def test_mutual_exclusivity(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        _make_supervised_project(temp_guard_env, monkeypatch)
        result = runner.invoke(main, ["guard", "supervise", "--off", "--on"])
        assert result.exit_code != 0 or "only one action" in result.output.lower()

    def test_reload_from_path(self, runner: CliRunner, temp_guard_env: Path, monkeypatch) -> None:
        store = _make_supervised_project(temp_guard_env, monkeypatch)
        plan = temp_guard_env / "plan.md"
        plan.write_text("# Updated Plan")

        result = runner.invoke(main, ["guard", "supervise", "--reload-from", str(plan)])

        assert result.exit_code == 0
        assert "plan updated" in result.output.lower()

        updated = store.read()
        assert updated.intent.policy is not None
        assert updated.intent.policy.supervisor is not None
        assert updated.intent.policy.supervisor.plan_override_path is not None
        assert "plan.md" in updated.intent.policy.supervisor.plan_override_path


class TestSupervisorProxyFlags:
    """Tests for --supervisor-proxy / --no-supervisor-proxy on guard supervise."""

    def test_supervisor_proxy_mutual_exclusivity(self, temp_guard_env: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["guard", "supervise", "planner", "--supervisor-proxy", "x", "--no-supervisor-proxy"],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_supervisor_proxy_requires_target(self, temp_guard_env: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervise", "--supervisor-proxy", "x"])
        assert result.exit_code == 1
        assert "require a target" in result.output

    def test_no_supervisor_proxy_requires_target(self, temp_guard_env: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["guard", "supervise", "--no-supervisor-proxy"])
        assert result.exit_code == 1
        assert "require a target" in result.output

    @patch("forge.guard.semantic.supervisor.validate_supervisor_target", side_effect=_validate_supervisor_target)
    @patch("forge.guard.semantic.supervisor.apply_supervisor_routing")
    @patch("forge.guard.semantic.supervisor.preflight_supervisor_proxy", return_value="litellm-gemini")
    def test_supervisor_proxy_passed_to_apply(
        self, mock_preflight, mock_apply, mock_validate, temp_guard_env: Path
    ) -> None:
        project = temp_guard_env
        store = SessionStore(str(project), "test-session")
        state = create_session_state("test-session", worktree_path=str(project))
        state.forge_root = str(project)
        store.write(state)

        runner = CliRunner()
        monkeypatch_env = {"FORGE_SESSION": "test-session"}
        with patch.dict("os.environ", monkeypatch_env):
            result = runner.invoke(
                main,
                ["guard", "supervise", "planner", "--supervisor-proxy", "litellm-gemini"],
            )

        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
        mock_preflight.assert_called_once_with("litellm-gemini")
        mock_apply.assert_called_once()
        assert mock_apply.call_args.kwargs.get("supervisor_proxy") == "litellm-gemini"
