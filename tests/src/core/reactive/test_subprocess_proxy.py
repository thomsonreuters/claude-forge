"""Tests for subprocess proxy routing (FORGE_SUBPROCESS_PROXY)."""

from __future__ import annotations

import pytest

from forge.core.reactive.env import (
    FORGE_SUBPROCESS_BASE_URL_VAR,
    FORGE_SUBPROCESS_PROXY_ID_VAR,
    FORGE_SUBPROCESS_PROXY_VAR,
    FORGE_SUBPROCESS_TEMPLATE_VAR,
    build_claude_env,
)
from forge.core.reactive.session_runner import run_claude_session
from forge.proxy.proxies import ProxyEntry, ProxyRegistry


class TestBuildClaudeEnvSubprocessProxy:
    """build_claude_env() resolves FORGE_SUBPROCESS_PROXY as a fallback."""

    def test_no_subprocess_proxy_no_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Without subprocess proxy or base_url, no ANTHROPIC_BASE_URL is set."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv(FORGE_SUBPROCESS_PROXY_VAR, raising=False)
        env = build_claude_env()
        assert "ANTHROPIC_BASE_URL" not in env

    def test_explicit_base_url_takes_precedence(self, monkeypatch: pytest.MonkeyPatch):
        """Explicit base_url always wins over subprocess proxy."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "some-proxy")
        env = build_claude_env(base_url="http://explicit:8000")
        assert env["ANTHROPIC_BASE_URL"] == "http://explicit:8000"

    def test_explicit_base_url_takes_precedence_over_extra_vars(self, monkeypatch: pytest.MonkeyPatch):
        """extra_vars cannot override the selected proxy route."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "some-proxy")
        env = build_claude_env(
            base_url="http://explicit:8000",
            extra_vars={"ANTHROPIC_BASE_URL": "http://from-extra:9000"},
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://explicit:8000"

    def test_direct_mode_ignores_subprocess_proxy(self, monkeypatch: pytest.MonkeyPatch):
        """direct=True removes ANTHROPIC_BASE_URL even when subprocess proxy is set."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "some-proxy")
        monkeypatch.setenv(FORGE_SUBPROCESS_BASE_URL_VAR, "http://host.docker.internal:8096")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://inherited:8000")
        env = build_claude_env(direct=True)
        assert "ANTHROPIC_BASE_URL" not in env
        assert FORGE_SUBPROCESS_PROXY_VAR not in env
        assert FORGE_SUBPROCESS_BASE_URL_VAR not in env

    def test_injected_subprocess_base_url_used_without_registry(self, monkeypatch: pytest.MonkeyPatch):
        """Host-resolved subprocess metadata works where registry lookup is unavailable."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "openrouter")
        monkeypatch.setenv(FORGE_SUBPROCESS_BASE_URL_VAR, "http://host.docker.internal:8096")

        env = build_claude_env()

        assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:8096"

    def test_direct_mode_removes_extra_vars_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """direct=True cannot be bypassed by extra_vars."""
        monkeypatch.delenv(FORGE_SUBPROCESS_PROXY_VAR, raising=False)
        env = build_claude_env(
            direct=True,
            extra_vars={"ANTHROPIC_BASE_URL": "http://from-extra:9000"},
        )
        assert "ANTHROPIC_BASE_URL" not in env

    def test_subprocess_proxy_resolved_when_no_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """When no base_url and not direct, FORGE_SUBPROCESS_PROXY is resolved."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "openrouter")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: "http://localhost:8095",
        )
        env = build_claude_env()
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8095"

    def test_subprocess_proxy_overrides_extra_vars_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Subprocess proxy fallback cannot be silently bypassed by extra_vars."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "openrouter")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: "http://localhost:8095",
        )
        env = build_claude_env(extra_vars={"ANTHROPIC_BASE_URL": "http://from-extra:9000"})
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8095"

    def test_subprocess_proxy_unresolvable_leaves_no_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """When subprocess proxy can't be resolved, no ANTHROPIC_BASE_URL is set."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "dead-proxy")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: None,
        )
        env = build_claude_env()
        assert "ANTHROPIC_BASE_URL" not in env

    def test_unresolvable_subprocess_proxy_scrubs_inherited_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """A dead subprocess proxy must not fall back to an inherited parent proxy."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://inherited:8080")
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "dead-proxy")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: None,
        )
        env = build_claude_env()
        assert "ANTHROPIC_BASE_URL" not in env

    def test_subprocess_proxy_inherited_by_child(self, monkeypatch: pytest.MonkeyPatch):
        """FORGE_SUBPROCESS_PROXY propagates to child environments."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "openrouter")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: "http://localhost:8095",
        )
        env = build_claude_env()
        assert env[FORGE_SUBPROCESS_PROXY_VAR] == "openrouter"


class TestRunClaudeSessionGuard:
    """run_claude_session() fails when subprocess proxy is configured but unavailable."""

    def test_guard_returns_error_when_proxy_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        """Subprocess proxy set but unresolvable -> error result (not silent fallback)."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "dead-proxy")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: None,
        )

        result = run_claude_session("test prompt")
        assert not result.success
        assert "dead-proxy" in (result.error or "")
        assert "not available" in (result.error or "")

    def test_guard_skipped_when_explicit_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Explicit base_url bypasses the subprocess proxy guard."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "dead-proxy")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: None,
        )
        # This should NOT hit the guard (explicit base_url provided)
        # It will fail on the actual claude binary, but that's a different error
        result = run_claude_session("test", base_url="http://localhost:9999", timeout_seconds=1)
        assert (
            result.error != "Subprocess proxy 'dead-proxy' not available. Start it with: forge proxy start dead-proxy"
        )

    def test_guard_skipped_when_direct(self, monkeypatch: pytest.MonkeyPatch):
        """direct=True bypasses the subprocess proxy guard."""
        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "dead-proxy")
        monkeypatch.setattr(
            "forge.core.reactive.env._resolve_subprocess_proxy",
            lambda proxy_id: None,
        )
        result = run_claude_session("test", direct=True, timeout_seconds=1)
        # Should fail with "claude not found" or timeout, NOT the proxy guard
        assert "not available" not in (result.error or "")


class TestBuildSessionEnv:
    """_build_session_env() threads subprocess_proxy into env vars."""

    def test_subprocess_proxy_set_in_env(self):
        from forge.cli.session import _build_session_env

        env_vars, _ = _build_session_env(
            session_name="test",
            context_limit=200000,
            template=None,
            base_url=None,
            subprocess_proxy="openrouter",
        )
        assert env_vars[FORGE_SUBPROCESS_PROXY_VAR] == "openrouter"

    def test_subprocess_proxy_metadata_set_in_env_for_sidecar(self, monkeypatch: pytest.MonkeyPatch):
        from forge.cli.session import _build_session_env

        entry = ProxyEntry(
            proxy_id="openrouter",
            template="openrouter-openai",
            base_url="http://localhost:8096",
            port=8096,
            status="healthy",
        )
        registry = ProxyRegistry(proxies={"openrouter": entry})

        class Store:
            def read(self) -> ProxyRegistry:
                return registry

        monkeypatch.setattr("forge.proxy.proxies.ProxyRegistryStore", Store)

        env_vars, _ = _build_session_env(
            session_name="test",
            context_limit=200000,
            template=None,
            base_url=None,
            subprocess_proxy="openrouter",
            sidecar=True,
        )

        assert env_vars[FORGE_SUBPROCESS_PROXY_VAR] == "openrouter"
        assert env_vars[FORGE_SUBPROCESS_BASE_URL_VAR] == "http://host.docker.internal:8096"
        assert env_vars[FORGE_SUBPROCESS_PROXY_ID_VAR] == "openrouter"
        assert env_vars[FORGE_SUBPROCESS_TEMPLATE_VAR] == "openrouter-openai"

    def test_no_subprocess_proxy_omits_env_var(self):
        from forge.cli.session import _build_session_env

        env_vars, _ = _build_session_env(
            session_name="test",
            context_limit=200000,
            template=None,
            base_url=None,
        )
        assert FORGE_SUBPROCESS_PROXY_VAR not in env_vars


class TestReviewSubprocessProxy:
    """Review workers honor subprocess proxy fallback for direct model specs."""

    def test_preflight_uses_subprocess_proxy_for_direct_specs(self, monkeypatch: pytest.MonkeyPatch):
        from forge.review.engine import preflight_check
        from forge.review.models import ModelSpec

        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "openrouter")
        monkeypatch.setattr(
            "forge.core.reactive.proxy.check_proxy_reachable",
            lambda proxy, timeout_s=1.0: (proxy == "openrouter", "", "http://localhost:8095"),
        )

        errors = preflight_check(
            [
                ModelSpec(
                    name="claude-opus",
                    model_id="claude-opus",
                    family="anthropic",
                    provider_refs=(("direct", "claude-opus-4-6"),),
                    description="direct worker",
                )
            ]
        )

        assert errors == []

    def test_direct_spec_ignores_dead_subprocess_proxy(self, monkeypatch: pytest.MonkeyPatch):
        """Direct-only specs bypass subprocess proxy and resolve via direct route."""
        from forge.review.engine import preflight_check
        from forge.review.models import ModelSpec

        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "dead-proxy")

        errors = preflight_check(
            [
                ModelSpec(
                    name="claude-opus",
                    model_id="claude-opus",
                    family="anthropic",
                    provider_refs=(("direct", "claude-opus-4-6"),),
                    description="direct worker",
                )
            ]
        )

        assert errors == []

    def test_cost_tracking_resolves_subprocess_proxy_for_direct_specs(self, monkeypatch: pytest.MonkeyPatch):
        from forge.core.reactive.cost_tracking import resolve_proxy_urls
        from forge.review.models import ModelSpec

        monkeypatch.setenv(FORGE_SUBPROCESS_PROXY_VAR, "openrouter")
        monkeypatch.setattr(
            "forge.core.reactive.proxy.lookup_proxy_base_url",
            lambda proxy: "http://localhost:8095" if proxy == "openrouter" else None,
        )

        urls = resolve_proxy_urls(
            [
                ModelSpec(
                    name="claude-opus",
                    model_id="claude-opus",
                    family="anthropic",
                    provider_refs=(("direct", "claude-opus-4-6"),),
                    description="direct worker",
                )
            ]
        )

        assert urls == ["http://localhost:8095"]
