"""Tests for SemanticSupervisorPolicy.

Tests cover:
- _evaluate() via mocked invoke_supervisor
- Cache behavior (hit/miss/expiry/skip-on-warn)
- State persistence (get_state/set_state with pruning)
- applies_to() filtering
- Engine integration (supervisor + deterministic composition)
- Hook warning output
- Policy state generalization round-trip (M25)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from forge.guard.engine import build_engine
from forge.guard.semantic.supervisor import SemanticSupervisorPolicy
from forge.guard.semantic.verdict import verdict_to_decision
from forge.guard.types import ActionContext, PolicyDecision, Violation
from forge.session.models import SupervisorConfig

# --- Fixtures ---


def _make_context(tool_name: str = "Write", target_path: str = "src/main.py") -> ActionContext:
    """Create a minimal ActionContext for testing."""
    return ActionContext(
        event=f"PreToolUse.{tool_name}",
        tool_name=tool_name,
        tool_args={"file_path": target_path, "content": "print('hello')"},
        repo_root="/workspace",
        session_name="test-session",
        target_path=target_path,
        new_content="print('hello')",
    )


def _make_config(**overrides: object) -> SupervisorConfig:
    """Create a SupervisorConfig with defaults suitable for testing."""
    defaults = {
        "resume_id": "uuid-test-supervisor",
        "timeout_seconds": 10,
        "throttle_seconds": 30,
    }
    defaults.update(overrides)
    return SupervisorConfig(**defaults)  # type: ignore[arg-type]


def _allow_decision(warnings: list[str] | None = None) -> PolicyDecision:
    """Create a clean allow decision."""
    return PolicyDecision(
        decision="allow",
        policy_id="semantic.supervisor",
        warnings=warnings or [],
    )


def _warn_decision(msg: str = "Possible divergence") -> PolicyDecision:
    """Create a warn decision."""
    return PolicyDecision(
        decision="warn",
        policy_id="semantic.supervisor",
        warnings=[msg],
    )


def _deny_decision(msg: str = "Divergent from plan") -> PolicyDecision:
    """Create a deny decision with a violation."""
    return PolicyDecision(
        decision="deny",
        policy_id="semantic.supervisor",
        violations=[
            Violation(
                rule_id="semantic.supervisor.alignment",
                message=msg,
                severity="high",
                citations=["Section 2: API design"],
            )
        ],
    )


# --- applies_to() Tests ---


class TestSupervisorAppliesTo:
    """Tests for SemanticSupervisorPolicy.applies_to()."""

    def test_write_with_resume_id(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config())
        assert policy.applies_to(_make_context("Write")) is True

    def test_edit_with_resume_id(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config())
        assert policy.applies_to(_make_context("Edit")) is True

    def test_read_tool_excluded(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config())
        assert policy.applies_to(_make_context("Read")) is False

    def test_no_resume_id(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config(resume_id=None))
        assert policy.applies_to(_make_context("Write")) is False

    def test_no_config(self) -> None:
        policy = SemanticSupervisorPolicy(config=None)
        assert policy.applies_to(_make_context("Write")) is False


# --- _evaluate() and Caching Tests ---


class TestSupervisorEvaluate:
    """Tests for SemanticSupervisorPolicy._evaluate() with mocked invoke_supervisor."""

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_aligned_verdict_allows(self, mock_invoke: MagicMock) -> None:
        mock_invoke.return_value = _allow_decision()
        policy = SemanticSupervisorPolicy(config=_make_config())
        result = policy.evaluate(_make_context())
        assert result.decision == "allow"
        mock_invoke.assert_called_once()

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_divergent_high_confidence_denies(self, mock_invoke: MagicMock) -> None:
        mock_invoke.return_value = _deny_decision()
        policy = SemanticSupervisorPolicy(config=_make_config())
        result = policy.evaluate(_make_context())
        assert result.decision == "deny"

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_divergent_low_confidence_warns(self, mock_invoke: MagicMock) -> None:
        mock_invoke.return_value = _warn_decision("Possible divergence (confidence: 40%)")
        policy = SemanticSupervisorPolicy(config=_make_config())
        result = policy.evaluate(_make_context())
        assert result.decision == "warn"

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_timeout_allows_with_warning(self, mock_invoke: MagicMock) -> None:
        """Supervisor timeout should fail-open with warning."""
        mock_invoke.return_value = PolicyDecision(
            decision="allow",
            policy_id="semantic.supervisor",
            warnings=["Supervisor timed out after 10s"],
        )
        policy = SemanticSupervisorPolicy(config=_make_config())
        result = policy.evaluate(_make_context())
        assert result.decision == "allow"
        assert len(result.warnings) > 0

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_clean_allow_is_cached(self, mock_invoke: MagicMock) -> None:
        """Clean allows (no warnings) should be cached."""
        mock_invoke.return_value = _allow_decision()
        policy = SemanticSupervisorPolicy(config=_make_config(throttle_seconds=60))

        # First call: cache miss, invokes supervisor
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1

        # Second call: cache hit, no invocation
        result = policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1
        assert result.cached is True

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_warn_outcome_not_cached(self, mock_invoke: MagicMock) -> None:
        """Warn outcomes should NOT be cached (M26 fix)."""
        mock_invoke.return_value = _warn_decision()
        policy = SemanticSupervisorPolicy(config=_make_config(throttle_seconds=60))

        # First call
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1

        # Second call: should re-invoke (not cached)
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 2

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_allow_with_warnings_not_cached(self, mock_invoke: MagicMock) -> None:
        """Allow-with-warnings (e.g., timeout) should NOT be cached (M26 fix)."""
        mock_invoke.return_value = PolicyDecision(
            decision="allow",
            policy_id="semantic.supervisor",
            warnings=["Supervisor timed out after 10s"],
        )
        policy = SemanticSupervisorPolicy(config=_make_config(throttle_seconds=60))

        policy.evaluate(_make_context())
        policy.evaluate(_make_context())
        # Both calls should invoke supervisor (nothing cached)
        assert mock_invoke.call_count == 2

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_deny_not_cached(self, mock_invoke: MagicMock) -> None:
        """Denials should NOT be cached (allows re-evaluation after fix)."""
        mock_invoke.return_value = _deny_decision()
        policy = SemanticSupervisorPolicy(config=_make_config(throttle_seconds=60))

        policy.evaluate(_make_context())
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 2

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_cache_expires_after_throttle(self, mock_invoke: MagicMock) -> None:
        """Cached entries should expire after the throttle window."""
        mock_invoke.return_value = _allow_decision()
        policy = SemanticSupervisorPolicy(config=_make_config(throttle_seconds=0))

        # With throttle=0, cache always expires
        policy.evaluate(_make_context())
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 2


# --- State Persistence Tests ---


class TestSupervisorState:
    """Tests for get_state/set_state and cache pruning."""

    def test_get_state_returns_cache(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config())
        policy._cache.update("key1", verdict="aligned")
        state = policy.get_state()
        assert "cache" in state
        assert "key1" in state["cache"]

    def test_set_state_restores_cache(self) -> None:
        from forge.core.state import now_iso

        policy = SemanticSupervisorPolicy(config=_make_config())
        saved = {"cache": {"key1": {"verdict": "aligned", "checked_at": now_iso()}}}
        policy.set_state(saved)
        assert policy._cache.check("key1") is not None

    def test_set_state_empty_dict(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config())
        policy._cache.update("something", verdict="aligned")
        policy.set_state({})
        assert policy._cache.check("something") is None

    def test_get_state_prunes_to_50(self) -> None:
        """Cache should be pruned to 50 most recent entries on get_state()."""
        policy = SemanticSupervisorPolicy(config=_make_config())
        # Add 60 entries directly to ThrottleCache internals
        for i in range(60):
            policy._cache._cache[f"key{i:03d}"] = {
                "verdict": "aligned",
                "checked_at": f"2025-01-01T{i:02d}:00:00Z",
                "confidence": 1.0,
            }
        state = policy.get_state()
        assert len(state["cache"]) == 50
        # Should keep the most recent (highest checked_at)
        assert "key059" in state["cache"]
        assert "key000" not in state["cache"]

    def test_state_round_trips(self) -> None:
        """State should survive save → restore cycle."""
        policy1 = SemanticSupervisorPolicy(config=_make_config())
        policy1._cache.update("key1", verdict="aligned", confidence=1.0)
        state = policy1.get_state()

        policy2 = SemanticSupervisorPolicy(config=_make_config())
        policy2.set_state(state)
        result = policy2._cache.check("key1")
        assert result is not None
        assert result["verdict"] == "aligned"


# --- FORGE_DEPTH Guard Tests ---


class TestSupervisorDepthGuard:
    """Verify invoke_supervisor skips at FORGE_DEPTH >= MAX_DEPTH."""

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_skips_supervisor_at_max_depth(self, mock_run: MagicMock) -> None:
        """At FORGE_DEPTH=2, supervisor should return allow without spawning."""
        from forge.guard.semantic.supervisor import invoke_supervisor

        with patch.dict("os.environ", {"FORGE_DEPTH": "2"}):
            result = invoke_supervisor(_make_config(), _make_context())

        assert result.decision == "allow"
        assert any("FORGE_DEPTH" in w for w in result.warnings)
        mock_run.assert_not_called()

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_runs_supervisor_below_max_depth(self, mock_run: MagicMock) -> None:
        """At FORGE_DEPTH=1, supervisor should proceed normally."""
        from forge.core.reactive.session_runner import SessionResult
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_run.return_value = SessionResult(
            stdout='```json\n{"verdict": "aligned", "confidence": 0.9, "violations": []}\n```',
            stderr="",
            returncode=0,
        )

        with patch.dict("os.environ", {"FORGE_DEPTH": "1"}):
            result = invoke_supervisor(_make_config(), _make_context())

        assert result.decision == "allow"
        mock_run.assert_called_once()


class TestSupervisorResumeTargetResolution:
    """Tests for resolving supervisor resume targets."""

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_resolves_forge_session_name_to_uuid(self, mock_run: MagicMock) -> None:
        """A Forge session name should resolve to its confirmed Claude UUID."""
        from forge.core.reactive.session_runner import SessionResult
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_run.return_value = SessionResult(
            stdout='```json\n{"verdict": "aligned", "confidence": 0.9, "violations": []}\n```',
            stderr="",
            returncode=0,
        )

        session_state = MagicMock()
        session_state.confirmed.claude_session_id = "resolved-uuid-1234"
        session_state.worktree.path = "/workspace"

        with patch("forge.session.manager.SessionManager.get_session", return_value=session_state):
            result = invoke_supervisor(_make_config(resume_id="planner-session"), _make_context())

        assert result.decision == "allow"
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["resume_id"] == "resolved-uuid-1234"

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_missing_confirmed_uuid_fails_open(self, mock_run: MagicMock) -> None:
        """A Forge session without a confirmed UUID should fail open with a warning."""
        from forge.guard.semantic.supervisor import invoke_supervisor

        session_state = MagicMock()
        session_state.confirmed.claude_session_id = None

        with patch("forge.session.manager.SessionManager.get_session", return_value=session_state):
            result = invoke_supervisor(_make_config(resume_id="planner-session"), _make_context())

        assert result.decision == "allow"
        assert result.warnings == [
            "Supervisor error: Forge session 'planner-session' has no confirmed Claude session ID, failing open"
        ]
        mock_run.assert_not_called()

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_resolved_target_includes_source_cwd(self, mock_run: MagicMock) -> None:
        """Forge session resolution should include the source worktree path as CWD."""
        from forge.core.reactive.session_runner import SessionResult
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_run.return_value = SessionResult(
            stdout='```json\n{"verdict": "aligned", "confidence": 0.9, "violations": []}\n```',
            stderr="",
            returncode=0,
        )

        session_state = MagicMock()
        session_state.confirmed.claude_session_id = "resolved-uuid-1234"
        session_state.worktree.path = "/original/checkout"

        with patch("forge.session.manager.SessionManager.get_session", return_value=session_state):
            invoke_supervisor(_make_config(resume_id="planner-session"), _make_context())

        assert mock_run.call_args.kwargs["cwd"] == "/original/checkout"

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_resolved_target_raw_uuid_no_cwd(self, mock_run: MagicMock) -> None:
        """Raw UUID targets should not set source_cwd (no resolution possible)."""
        from forge.core.reactive.session_runner import SessionResult
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_run.return_value = SessionResult(
            stdout='```json\n{"verdict": "aligned", "confidence": 0.9, "violations": []}\n```',
            stderr="",
            returncode=0,
        )

        raw_uuid = "12345678-1234-1234-1234-123456789abc"
        invoke_supervisor(_make_config(resume_id=raw_uuid), _make_context())

        assert mock_run.call_args.kwargs["cwd"] is None

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_fork_session_passed_to_run_claude(self, mock_run: MagicMock) -> None:
        """invoke_supervisor should pass fork_session from config to run_claude_session."""
        from forge.core.reactive.session_runner import SessionResult
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_run.return_value = SessionResult(
            stdout='```json\n{"verdict": "aligned", "confidence": 0.9, "violations": []}\n```',
            stderr="",
            returncode=0,
        )

        raw_uuid = "12345678-1234-1234-1234-123456789abc"
        invoke_supervisor(_make_config(resume_id=raw_uuid, fork_session=True), _make_context())
        assert mock_run.call_args.kwargs["fork_session"] is True

        invoke_supervisor(_make_config(resume_id=raw_uuid, fork_session=False), _make_context())
        assert mock_run.call_args.kwargs["fork_session"] is False

    @patch("forge.guard.semantic.supervisor.resolve_subprocess_routing")
    @patch("forge.guard.semantic.supervisor.run_claude_session")
    def test_direct_mode_skips_routing_resolver(self, mock_run: MagicMock, mock_resolve: MagicMock) -> None:
        """direct=True should not consult proxy/env routing before invoking Claude."""
        from forge.core.reactive.session_runner import SessionResult
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_run.return_value = SessionResult(
            stdout='```json\n{"verdict": "aligned", "confidence": 0.9, "violations": []}\n```',
            stderr="",
            returncode=0,
        )

        raw_uuid = "12345678-1234-1234-1234-123456789abc"
        with patch.dict("os.environ", {"FORGE_SUBPROCESS_PROXY": "broken-proxy"}):
            result = invoke_supervisor(_make_config(resume_id=raw_uuid, direct=True), _make_context())

        assert result.decision == "allow"
        mock_resolve.assert_not_called()
        assert mock_run.call_args.kwargs["base_url"] is None
        assert mock_run.call_args.kwargs["direct"] is True


# --- Engine Integration Tests ---


class TestSupervisorEngineIntegration:
    """Tests for supervisor integration with PolicyEngine."""

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_supervisor_plus_tdd_both_allow(self, mock_invoke: MagicMock) -> None:
        """When both supervisor and TDD allow, final decision is allow."""
        mock_invoke.return_value = _allow_decision()
        engine = build_engine(["tdd"], fail_mode="open")
        engine.register(SemanticSupervisorPolicy(config=_make_config()))

        # Write to tests/ first (satisfies TDD)
        ctx_test = _make_context("Write", "tests/test_foo.py")
        engine.evaluate(ctx_test)

        # Write to src/ (supervisor allows, TDD allows because tests touched)
        ctx_src = _make_context("Write", "src/foo.py")
        result = engine.evaluate(ctx_src)
        assert result.final_decision == "allow"

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_supervisor_deny_blocks(self, mock_invoke: MagicMock) -> None:
        """Supervisor deny should block even if TDD allows."""
        mock_invoke.return_value = _deny_decision()
        engine = build_engine(["tdd"], fail_mode="open")
        engine.register(SemanticSupervisorPolicy(config=_make_config()))

        # Touch tests first
        ctx_test = _make_context("Write", "tests/test_foo.py")
        engine.evaluate(ctx_test)

        # Supervisor denies the src write
        ctx_src = _make_context("Write", "src/foo.py")
        result = engine.evaluate(ctx_src)
        assert result.final_decision == "deny"

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_supervisor_warns_surfaces_warnings(self, mock_invoke: MagicMock) -> None:
        """Supervisor warn should surface via all_warnings."""
        mock_invoke.return_value = _warn_decision("Possible divergence from plan")
        engine = build_engine([], fail_mode="open")
        engine.register(SemanticSupervisorPolicy(config=_make_config()))

        result = engine.evaluate(_make_context())
        assert result.final_decision == "warn"
        assert any("divergence" in w.lower() for w in result.all_warnings)

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_empty_bundles_supervisor_only(self, mock_invoke: MagicMock) -> None:
        """Supervisor should run even with empty bundles (gating fix verification)."""
        mock_invoke.return_value = _allow_decision()
        engine = build_engine([], fail_mode="open")
        engine.register(SemanticSupervisorPolicy(config=_make_config()))

        result = engine.evaluate(_make_context())
        assert result.final_decision == "allow"
        mock_invoke.assert_called_once()

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_state_persists_through_engine(self, mock_invoke: MagicMock) -> None:
        """Engine should collect and restore supervisor state."""
        mock_invoke.return_value = _allow_decision()
        engine = build_engine([], fail_mode="open")
        engine.register(SemanticSupervisorPolicy(config=_make_config()))

        engine.evaluate(_make_context())
        collected = engine.get_collected_state()
        assert "semantic.supervisor" in collected
        assert "cache" in collected["semantic.supervisor"]


# --- Verdict Integration Tests (L13 fix verification) ---


class TestFailOpenWithWarning:
    """Verify that empty/unparseable responses produce warn, not silent allow (L13 fix)."""

    def test_empty_response_produces_warn(self) -> None:
        """Empty supervisor response should map to warn decision."""
        from forge.guard.semantic.verdict import parse_supervisor_verdict

        verdict = parse_supervisor_verdict("")
        decision = verdict_to_decision(verdict)
        assert decision.decision == "warn"
        assert len(decision.warnings) > 0

    def test_unparseable_response_produces_warn(self) -> None:
        """Unparseable supervisor response should map to warn decision."""
        from forge.guard.semantic.verdict import parse_supervisor_verdict

        verdict = parse_supervisor_verdict("This is not JSON at all.")
        decision = verdict_to_decision(verdict)
        assert decision.decision == "warn"
        assert len(decision.warnings) > 0


# --- Policy State Generalization (M25) ---


class TestPolicyStateGeneralization:
    """Verify generic policy_states round-trip through engine."""

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_policy_states_round_trip(self, mock_invoke: MagicMock) -> None:
        """policy_states should round-trip through engine restore → evaluate → collect."""
        mock_invoke.return_value = _allow_decision()

        # Set up engine with supervisor
        engine = build_engine(["tdd"], fail_mode="open")
        supervisor = SemanticSupervisorPolicy(config=_make_config())
        engine.register(supervisor)

        # Simulate restored state from manifest
        persisted = {
            "tdd.tests-before-impl": {"tests_touched": ["tests/test_old.py"]},
            "semantic.supervisor": {
                "cache": {
                    "old_key": {
                        "verdict": "aligned",
                        "checked_at": "2025-01-01T00:00:00Z",
                        "confidence": 1.0,
                    }
                }
            },
        }
        engine.restore_state(persisted)

        # Evaluate (adds new state)
        ctx = _make_context("Write", "tests/test_new.py")
        engine.evaluate(ctx)

        # Collect state — should contain both old and new data
        collected = engine.get_collected_state()
        assert "tdd.tests-before-impl" in collected
        assert "semantic.supervisor" in collected

        # TDD state should include both old and new test paths
        tdd_state = collected["tdd.tests-before-impl"]
        assert "tests/test_old.py" in tdd_state.get("tests_touched", [])
        assert "tests/test_new.py" in tdd_state.get("tests_touched", [])

    def test_non_applicable_policy_state_preserved(self) -> None:
        """State for policies that didn't apply should be preserved in merged output.

        Regression test: when TDD's applies_to() returns False (e.g., writing to docs/),
        its state should not be lost from the merged policy_states.
        """
        from forge.guard.store import build_policy_state_update
        from forge.guard.types import CompositeDecision

        # Simulate: TDD didn't run (wrote to docs/), only provenance collected
        engine_state: dict[str, dict[str, Any]] = {}  # No stateful policies collected
        existing = {
            "decisions": [],
            "policy_states": {
                "tdd.tests-before-impl": {"tests_touched": ["tests/test_important.py"]},
                "semantic.supervisor": {"cache": {"k1": {"verdict": "aligned"}}},
            },
        }

        result = CompositeDecision(final_decision="allow")
        updated = build_policy_state_update(
            result=result,
            engine_state=engine_state,
            existing_state=existing,
        )

        # Both policy states should be preserved even though neither was collected
        assert "tdd.tests-before-impl" in updated["policy_states"]
        assert "tests/test_important.py" in updated["policy_states"]["tdd.tests-before-impl"]["tests_touched"]
        assert "semantic.supervisor" in updated["policy_states"]
        assert "k1" in updated["policy_states"]["semantic.supervisor"]["cache"]


# --- Setup Helper Tests ---


class TestValidateSupervisorTarget:
    """Tests for validate_supervisor_target()."""

    def test_valid_target_with_uuid_and_confirmation(self) -> None:
        from forge.guard.semantic.supervisor import validate_supervisor_target

        state = MagicMock()
        state.confirmed.claude_session_id = "uuid-1234"
        state.confirmed.confirmed_by = "hook:SessionStart:startup"
        state.confirmed.transcript_path = None

        with patch("forge.session.manager.SessionManager.get_session", return_value=state):
            result = validate_supervisor_target("planner")
        assert result is state

    def test_missing_session_raises(self) -> None:
        from forge.guard.semantic.supervisor import validate_supervisor_target

        with (
            patch(
                "forge.session.manager.SessionManager.get_session",
                side_effect=KeyError("not found"),
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            validate_supervisor_target("nonexistent")

    def test_no_claude_uuid_raises(self) -> None:
        from forge.guard.semantic.supervisor import validate_supervisor_target

        state = MagicMock()
        state.confirmed.claude_session_id = None

        with (
            patch("forge.session.manager.SessionManager.get_session", return_value=state),
            pytest.raises(ValueError, match="no confirmed Claude session ID"),
        ):
            validate_supervisor_target("unlaunched-session")

    def test_pre_seeded_uuid_without_evidence_raises(self) -> None:
        """Pre-seeded UUID alone (no hook confirmation, no transcript) is rejected."""
        from forge.guard.semantic.supervisor import validate_supervisor_target

        state = MagicMock()
        state.confirmed.claude_session_id = "pre-seeded-uuid"
        state.confirmed.confirmed_by = None
        state.confirmed.transcript_path = None
        state.worktree = None

        with (
            patch("forge.session.manager.SessionManager.get_session", return_value=state),
            pytest.raises(ValueError, match="pre-seeded UUID but no confirmed"),
        ):
            validate_supervisor_target("no-launch-session")

    def test_transcript_on_disk_is_valid_evidence(self, tmp_path) -> None:
        """A transcript file on disk counts as conversation evidence."""
        from forge.guard.semantic.supervisor import validate_supervisor_target

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n")

        state = MagicMock()
        state.confirmed.claude_session_id = "uuid-with-transcript"
        state.confirmed.confirmed_by = None
        state.confirmed.transcript_path = str(transcript)

        with patch("forge.session.manager.SessionManager.get_session", return_value=state):
            result = validate_supervisor_target("transcript-session")
        assert result is state


class TestAutoSeedSupervisorProxy:
    """Tests for auto_seed_supervisor_proxy()."""

    def test_different_routing_returns_proxy(self) -> None:
        from forge.guard.semantic.supervisor import auto_seed_supervisor_proxy

        state = MagicMock()
        state.confirmed.started_with_proxy.proxy_id = "proxy-123"
        state.confirmed.started_with_proxy.template = "litellm-openai"

        result = auto_seed_supervisor_proxy(state, current_proxy_id=None, current_template=None, current_direct=True)
        assert result == "proxy-123"

    def test_same_routing_returns_none(self) -> None:
        from forge.guard.semantic.supervisor import auto_seed_supervisor_proxy

        state = MagicMock()
        state.confirmed.started_with_proxy.proxy_id = "proxy-123"
        state.confirmed.started_with_proxy.template = "litellm-openai"

        result = auto_seed_supervisor_proxy(
            state, current_proxy_id="proxy-123", current_template="litellm-openai", current_direct=False
        )
        assert result is None

    def test_no_confirmed_proxy_returns_none(self) -> None:
        from forge.guard.semantic.supervisor import auto_seed_supervisor_proxy

        state = MagicMock()
        state.confirmed.started_with_proxy = None

        result = auto_seed_supervisor_proxy(state, current_proxy_id=None, current_template=None, current_direct=False)
        assert result is None

    def test_falls_back_to_template_when_no_proxy_id(self) -> None:
        from forge.guard.semantic.supervisor import auto_seed_supervisor_proxy

        state = MagicMock()
        state.confirmed.started_with_proxy.proxy_id = None
        state.confirmed.started_with_proxy.template = "litellm-gemini"

        result = auto_seed_supervisor_proxy(
            state, current_proxy_id=None, current_template="litellm-openai", current_direct=False
        )
        assert result == "litellm-gemini"


class TestApplySupervisorRouting:
    """Tests for apply_supervisor_routing()."""

    def test_explicit_proxy_overrides_auto_seed(self) -> None:
        from forge.guard.semantic.supervisor import apply_supervisor_routing
        from forge.session.models import SupervisorConfig

        state = MagicMock()
        state.confirmed.started_with_proxy.proxy_id = "planner-proxy"
        state.confirmed.started_with_proxy.template = "litellm-openai"
        sup_config = SupervisorConfig()

        with patch("forge.guard.semantic.supervisor.auto_seed_supervisor_proxy") as mock_seed:
            result = apply_supervisor_routing(
                sup_config,
                state,
                supervisor_proxy="pre-validated-proxy",
            )
            mock_seed.assert_not_called()
        assert sup_config.proxy == "pre-validated-proxy"
        assert result == "pre-validated-proxy"

    def test_explicit_direct_overrides_auto_seed(self) -> None:
        from forge.guard.semantic.supervisor import apply_supervisor_routing
        from forge.session.models import SupervisorConfig

        state = MagicMock()
        state.confirmed.started_with_proxy.proxy_id = "planner-proxy"
        sup_config = SupervisorConfig()

        with patch("forge.guard.semantic.supervisor.auto_seed_supervisor_proxy") as mock_seed:
            result = apply_supervisor_routing(sup_config, state, supervisor_direct=True)
            mock_seed.assert_not_called()
        assert sup_config.direct is True
        assert result == "direct"

    def test_neither_flag_falls_through_to_auto_seed(self) -> None:
        from forge.guard.semantic.supervisor import apply_supervisor_routing
        from forge.session.models import SupervisorConfig

        state = MagicMock()
        state.confirmed.started_with_proxy.proxy_id = "planner-proxy"
        state.confirmed.started_with_proxy.template = "litellm-openai"
        sup_config = SupervisorConfig()

        result = apply_supervisor_routing(
            sup_config,
            state,
            current_proxy_id=None,
            current_template=None,
            current_direct=True,
        )
        assert sup_config.proxy == "planner-proxy"
        assert result == "planner-proxy"

    def test_auto_seed_direct_returns_direct_string(self) -> None:
        """When source was direct (no proxy), display string should be 'direct'."""
        from forge.guard.semantic.supervisor import apply_supervisor_routing
        from forge.session.models import SupervisorConfig

        state = MagicMock()
        state.confirmed.started_with_proxy = None  # source was direct
        sup_config = SupervisorConfig()

        result = apply_supervisor_routing(
            sup_config,
            state,
            current_proxy_id="some-proxy",
            current_template="litellm-openai",
            current_direct=False,
        )
        assert sup_config.direct is True
        assert result == "direct"

    def test_preflight_proxy_not_found_raises(self) -> None:
        from forge.guard.semantic.supervisor import preflight_supervisor_proxy
        from forge.proxy.proxies import ProxyResolutionError

        with patch("forge.proxy.proxies.ProxyRegistryStore") as mock_store:
            mock_store.return_value.read.return_value = MagicMock()
            with patch("forge.proxy.proxies.resolve_proxy", side_effect=ProxyResolutionError("not found")):
                with pytest.raises(ValueError, match="not found in registry"):
                    preflight_supervisor_proxy("bad-proxy")

    def test_preflight_proxy_returns_resolved_id(self) -> None:
        from forge.guard.semantic.supervisor import preflight_supervisor_proxy

        mock_entry = MagicMock()
        mock_entry.proxy_id = "resolved-id"
        with patch("forge.proxy.proxies.ProxyRegistryStore") as mock_store:
            mock_store.return_value.read.return_value = MagicMock()
            with patch("forge.proxy.proxies.resolve_proxy", return_value=mock_entry):
                result = preflight_supervisor_proxy("my-proxy")
        assert result == "resolved-id"


class TestApplySupervisorToIntent:
    """Tests for apply_supervisor_to_intent()."""

    def test_sets_supervisor_and_enables_policy(self) -> None:
        from forge.guard.semantic.supervisor import apply_supervisor_to_intent

        manifest = MagicMock()
        manifest.intent.policy = None
        sup_config = SupervisorConfig(resume_id="planner")

        apply_supervisor_to_intent(manifest, sup_config)

        assert manifest.intent.policy.enabled is True
        assert manifest.intent.policy.supervisor is sup_config

    def test_preserves_existing_policy_fields(self) -> None:
        from forge.guard.semantic.supervisor import apply_supervisor_to_intent
        from forge.session.models import PolicyIntent

        manifest = MagicMock()
        manifest.intent.policy = PolicyIntent(enabled=False, bundles=["tdd"], fail_mode="closed")
        sup_config = SupervisorConfig(resume_id="planner")

        apply_supervisor_to_intent(manifest, sup_config)

        assert manifest.intent.policy.enabled is True
        assert manifest.intent.policy.bundles == ["tdd"]
        assert manifest.intent.policy.fail_mode == "closed"
        assert manifest.intent.policy.supervisor is sup_config

    def test_clears_policy_enabled_override(self) -> None:
        """Wiring supervisor clears a prior %guard disable override."""
        from forge.guard.semantic.supervisor import apply_supervisor_to_intent
        from forge.session.models import PolicyIntent

        manifest = MagicMock()
        manifest.intent.policy = PolicyIntent(enabled=False)
        manifest.overrides = {"policy": {"enabled": False}}
        sup_config = SupervisorConfig(resume_id="planner")

        apply_supervisor_to_intent(manifest, sup_config)

        assert manifest.intent.policy.enabled is True
        assert manifest.intent.policy.supervisor is sup_config
        # Override should be cleared so it doesn't shadow intent
        assert "enabled" not in manifest.overrides.get("policy", {})

    def test_no_overrides_dict_does_not_crash(self) -> None:
        """Works when overrides is None or empty."""
        from forge.guard.semantic.supervisor import apply_supervisor_to_intent
        from forge.session.models import PolicyIntent

        manifest = MagicMock()
        manifest.intent.policy = PolicyIntent(enabled=False)
        manifest.overrides = None
        sup_config = SupervisorConfig(resume_id="planner")

        apply_supervisor_to_intent(manifest, sup_config)
        assert manifest.intent.policy.enabled is True


class TestShouldSupervisorUseDirect:
    """Tests for should_supervisor_use_direct()."""

    def test_direct_mode_planner_returns_true(self) -> None:
        from forge.guard.semantic.supervisor import should_supervisor_use_direct

        state = MagicMock()
        state.confirmed.started_with_proxy = None
        assert should_supervisor_use_direct(state) is True

    def test_proxied_planner_returns_false(self) -> None:
        from forge.guard.semantic.supervisor import should_supervisor_use_direct

        state = MagicMock()
        state.confirmed.started_with_proxy = MagicMock()
        state.confirmed.started_with_proxy.template = "litellm-openai"
        assert should_supervisor_use_direct(state) is False


# --- Suspended supervisor tests (applies_to + _evaluate guard) ---


class TestSupervisorSuspended:
    """Tests for the suspended toggle on supervision."""

    def test_suspended_config_applies_to_returns_false(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config(suspended=True))
        assert policy.applies_to(_make_context("Write")) is False

    def test_unsuspended_config_applies_to_returns_true(self) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config(suspended=False))
        assert policy.applies_to(_make_context("Write")) is True

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_evaluate_suspended_returns_allow_without_invoke(self, mock_invoke: MagicMock) -> None:
        policy = SemanticSupervisorPolicy(config=_make_config(suspended=True))
        result = policy.evaluate(_make_context())
        assert result.decision == "allow"
        assert not result.warnings
        mock_invoke.assert_not_called()

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_evaluate_not_configured_preserves_warning(self, mock_invoke: MagicMock) -> None:
        """Missing config still produces the 'not configured' warning."""
        policy = SemanticSupervisorPolicy(config=_make_config(resume_id=None))
        result = policy.evaluate(_make_context())
        assert result.decision == "allow"
        assert any("not configured" in w for w in result.warnings)
        mock_invoke.assert_not_called()


# --- Plan override tests ---


class TestLoadPlanOverride:
    """Tests for _load_plan_override()."""

    def test_no_override_returns_none(self) -> None:
        from forge.guard.semantic.supervisor import _load_plan_override

        config = _make_config(plan_override_path=None)
        assert _load_plan_override(config) is None

    def test_reads_file_content(self, tmp_path) -> None:
        from forge.guard.semantic.supervisor import _load_plan_override

        plan = tmp_path / "plan.md"
        plan.write_text("# My Plan\nDo the thing.")
        config = _make_config(plan_override_path=str(plan))
        assert _load_plan_override(config) == "# My Plan\nDo the thing."

    def test_missing_file_returns_none(self, tmp_path) -> None:
        from forge.guard.semantic.supervisor import _load_plan_override

        config = _make_config(plan_override_path=str(tmp_path / "nonexistent.md"))
        assert _load_plan_override(config) is None

    def test_empty_file_returns_none(self, tmp_path) -> None:
        from forge.guard.semantic.supervisor import _load_plan_override

        plan = tmp_path / "empty.md"
        plan.write_text("")
        config = _make_config(plan_override_path=str(plan))
        assert _load_plan_override(config) is None

    def test_relative_path_resolves_from_forge_root(self, tmp_path) -> None:
        from forge.guard.semantic.supervisor import _load_plan_override

        (tmp_path / "plans").mkdir()
        plan = tmp_path / "plans" / "plan.md"
        plan.write_text("Plan content")
        config = _make_config(plan_override_path="plans/plan.md", forge_root=str(tmp_path))
        assert _load_plan_override(config) == "Plan content"


class TestPlanOverridePrompt:
    """Tests for plan override injection into the supervisor prompt."""

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    @patch("forge.guard.semantic.supervisor._resolve_resume_target")
    def test_invoke_with_plan_override_prepends_preamble(
        self, mock_resolve: MagicMock, mock_run: MagicMock, tmp_path
    ) -> None:
        from forge.guard.semantic.supervisor import (
            _PLAN_OVERRIDE_PREAMBLE,
            invoke_supervisor,
        )

        plan = tmp_path / "plan.md"
        plan.write_text("# Updated Plan\nNew requirements.")

        mock_resolve.return_value = MagicMock(resume_id="uuid-123", warning=None, source_cwd=None)
        mock_run.return_value = MagicMock(success=True, stdout='{"verdict":"aligned","confidence":0.9,"violations":[]}')

        config = _make_config(plan_override_path=str(plan))
        invoke_supervisor(config, _make_context())

        prompt_sent = mock_run.call_args[0][0]
        assert "Updated Plan" in _PLAN_OVERRIDE_PREAMBLE
        assert "# Updated Plan\nNew requirements." in prompt_sent
        assert "supersedes" in prompt_sent.lower()

    @patch("forge.guard.semantic.supervisor.run_claude_session")
    @patch("forge.guard.semantic.supervisor._resolve_resume_target")
    def test_invoke_without_override_no_preamble(self, mock_resolve: MagicMock, mock_run: MagicMock) -> None:
        from forge.guard.semantic.supervisor import invoke_supervisor

        mock_resolve.return_value = MagicMock(resume_id="uuid-123", warning=None, source_cwd=None)
        mock_run.return_value = MagicMock(success=True, stdout='{"verdict":"aligned","confidence":0.9,"violations":[]}')

        config = _make_config(plan_override_path=None)
        invoke_supervisor(config, _make_context())

        prompt_sent = mock_run.call_args[0][0]
        assert "supersedes" not in prompt_sent.lower()


class TestPlanOverrideCache:
    """Tests for cache key differentiation with plan_override_path."""

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_different_plan_override_produces_cache_miss(self, mock_invoke: MagicMock, tmp_path) -> None:
        """Changing plan_override_path on the same policy must miss the cache."""
        mock_invoke.return_value = _allow_decision()

        plan_a = tmp_path / "plan_a.md"
        plan_a.write_text("Plan A")
        plan_b = tmp_path / "plan_b.md"
        plan_b.write_text("Plan B")

        config = _make_config(plan_override_path=str(plan_a), throttle_seconds=60)
        policy = SemanticSupervisorPolicy(config=config)

        # First eval with plan_a — cache miss
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1

        # Second eval same plan_a — cache hit
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1

        # Switch to plan_b — must be a cache miss
        config.plan_override_path = str(plan_b)
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 2

    @patch("forge.guard.semantic.supervisor.invoke_supervisor")
    def test_same_plan_path_edited_produces_cache_miss(self, mock_invoke: MagicMock, tmp_path) -> None:
        """In-place edit of the plan file (different mtime/size) must miss cache."""
        import time

        mock_invoke.return_value = _allow_decision()

        plan = tmp_path / "plan.md"
        plan.write_text("Version 1")

        config = _make_config(plan_override_path=str(plan), throttle_seconds=60)
        policy = SemanticSupervisorPolicy(config=config)

        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1

        # Cache hit with same content
        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 1

        # Edit in place — mtime and size change
        time.sleep(0.01)  # Ensure mtime_ns differs
        plan.write_text("Version 2 with more content")

        policy.evaluate(_make_context())
        assert mock_invoke.call_count == 2
