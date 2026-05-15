"""Tests for workflow-specific routing types and functions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from forge.core.reactive.routing import ModelRoute, RoutingResult, RoutingSource
from forge.review.routing import (
    WorkerRoutingPlan,
    _TemplateMeta,
    clear_template_cache,
    derive_model_routes,
    resolve_invocation_routing,
    resolve_model_flag,
)

# ── resolve_model_flag ───────────────────────────────────────────


class TestResolveModelFlag:

    def test_proxied_returns_model_ref(self):
        route = ModelRoute(
            provider="openrouter",
            credential="openrouter",
            family="openai",
            template_id="openrouter-openai",
            template_family="openai",
            model_ref="openai/gpt-5.5",
        )
        assert resolve_model_flag(route) == "openai/gpt-5.5"

    def test_direct_returns_none(self):
        route = ModelRoute(
            provider="direct",
            credential="anthropic-api",
            family="anthropic",
            template_id=None,
            template_family=None,
            model_ref="claude-opus-4-6",
        )
        assert resolve_model_flag(route) is None

    def test_litellm_returns_model_ref(self):
        route = ModelRoute(
            provider="litellm",
            credential="litellm-remote",
            family="openai",
            template_id="litellm-openai",
            template_family="openai",
            model_ref="openai/gpt-5.5",
        )
        assert resolve_model_flag(route) == "openai/gpt-5.5"


# ── WorkerRoutingPlan ────────────────────────────────────────────


class TestWorkerRoutingPlan:

    def _make_result(self, source: RoutingSource = "preferred_proxy") -> RoutingResult:
        return RoutingResult(
            base_url="http://localhost:8096",
            proxy_id="openrouter-openai",
            template="openrouter-openai",
            source=source,
            route=ModelRoute(
                provider="openrouter",
                credential="openrouter",
                family="openai",
                template_id="openrouter-openai",
                template_family="openai",
                model_ref="openai/gpt-5.5",
            ),
            credential="openrouter",
        )

    def test_construction(self):
        r1 = self._make_result("preferred_proxy")
        r2 = self._make_result("route_scan")
        plan = WorkerRoutingPlan(
            routes=(r1, r2),
            resolved_at="2026-05-14T12:00:00Z",
            via_override=None,
        )
        assert len(plan.routes) == 2
        assert plan.routes[0].source == "preferred_proxy"
        assert plan.routes[1].source == "route_scan"
        assert plan.via_override is None

    def test_with_via_override(self):
        plan = WorkerRoutingPlan(
            routes=(self._make_result("explicit"),),
            resolved_at="2026-05-14T12:00:00Z",
            via_override="openrouter-anthropic",
        )
        assert plan.via_override == "openrouter-anthropic"

    def test_frozen(self):
        plan = WorkerRoutingPlan(
            routes=(self._make_result(),),
            resolved_at="2026-05-14T12:00:00Z",
            via_override=None,
        )
        with pytest.raises(AttributeError):
            plan.via_override = "something"  # type: ignore[misc]

    def test_routes_indexed_by_position(self):
        r1 = self._make_result("explicit")
        r2 = self._make_result("route_scan")
        r3 = self._make_result("direct")
        plan = WorkerRoutingPlan(
            routes=(r1, r2, r3),
            resolved_at="2026-05-14T12:00:00Z",
            via_override=None,
        )
        assert plan.routes[0].source == "explicit"
        assert plan.routes[1].source == "route_scan"
        assert plan.routes[2].source == "direct"


# ── derive_model_routes ──────────────────────────────────────────

# Stub ModelSpec with the Phase 3+4 fields (real ModelSpec doesn't have them yet)


@dataclass(frozen=True)
class _StubModelSpec:
    name: str
    model_id: str
    family: str
    provider_refs: tuple[tuple[str, str], ...]
    description: str = ""
    preferred_proxy: str | None = None
    prompt: str | None = None
    prompt_mode: str = "override"
    worker_id: str | None = None


_OPENROUTER_OPENAI_META = _TemplateMeta(
    name="openrouter-openai",
    family="openai",
    preferred_provider="openrouter",
    credentials=("openrouter",),
)
_OPENROUTER_ANTHROPIC_META = _TemplateMeta(
    name="openrouter-anthropic",
    family="anthropic",
    preferred_provider="openrouter",
    credentials=("openrouter",),
)
_OPENROUTER_GEMINI_META = _TemplateMeta(
    name="openrouter-gemini",
    family="gemini",
    preferred_provider="openrouter",
    credentials=("openrouter",),
)
_LITELLM_OPENAI_META = _TemplateMeta(
    name="litellm-openai",
    family="openai",
    preferred_provider="litellm",
    credentials=("litellm-remote",),
)
_LITELLM_OPENAI_LOCAL_META = _TemplateMeta(
    name="litellm-openai-local",
    family="openai",
    preferred_provider="litellm",
    credentials=("openai-api",),
)

_ALL_METAS = [
    _OPENROUTER_ANTHROPIC_META,
    _OPENROUTER_GEMINI_META,
    _OPENROUTER_OPENAI_META,
    _LITELLM_OPENAI_META,
    _LITELLM_OPENAI_LOCAL_META,
]


class TestDeriveModelRoutes:

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        clear_template_cache()
        yield
        clear_template_cache()

    def _patch_metas(self, metas=None):
        return patch(
            "forge.review.routing._all_template_metas",
            return_value=metas if metas is not None else _ALL_METAS,
        )

    def test_openrouter_model_produces_native_and_cross_family_routes(self):
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
            preferred_proxy="openrouter-openai",
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        assert len(routes) >= 2
        assert routes[0].template_id == "openrouter-openai"
        assert routes[0].template_family == "openai"

        cross = [r for r in routes if r.template_family != "openai"]
        assert len(cross) >= 1
        assert all(r.provider == "openrouter" for r in cross)

    def test_preferred_proxy_ranked_first(self):
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
            preferred_proxy="openrouter-openai",
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        assert routes[0].template_id == "openrouter-openai"

    def test_native_family_before_cross_family(self):
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        native_indices = [i for i, r in enumerate(routes) if r.template_family == "openai"]
        cross_indices = [i for i, r in enumerate(routes) if r.template_family != "openai"]
        if native_indices and cross_indices:
            assert max(native_indices) < min(cross_indices)

    def test_direct_model_produces_single_route(self):
        spec = _StubModelSpec(
            name="claude-opus",
            model_id="claude-opus",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        assert len(routes) == 1
        assert routes[0].provider == "direct"
        assert routes[0].credential == "anthropic-api"
        assert routes[0].model_ref == "claude-opus-4-6"
        assert routes[0].template_id is None

    def test_multi_provider_refs(self):
        """Model with both openrouter and litellm refs produces routes for both."""
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(
                ("openrouter", "openai/gpt-5.5"),
                ("litellm", "openai/gpt-5.5"),
            ),
            preferred_proxy="openrouter-openai",
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        providers = {r.provider for r in routes}
        assert "openrouter" in providers
        assert "litellm" in providers

    def test_litellm_routes_include_local_and_remote(self):
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("litellm", "openai/gpt-5.5"),),
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        template_ids = {r.template_id for r in routes}
        assert "litellm-openai" in template_ids
        assert "litellm-openai-local" in template_ids

    def test_litellm_routes_have_correct_credentials(self):
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("litellm", "openai/gpt-5.5"),),
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        for r in routes:
            if r.template_id == "litellm-openai":
                assert r.credential == "litellm-remote"
            elif r.template_id == "litellm-openai-local":
                assert r.credential == "openai-api"

    def test_no_matching_templates_returns_empty(self):
        spec = _StubModelSpec(
            name="custom",
            model_id="custom",
            family="custom",
            provider_refs=(("nonexistent-provider", "custom/model"),),
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        assert len(routes) == 0

    def test_oss_model_provider_ref(self):
        """OSS models use provider-specific refs from their templates."""
        kimi_meta = _TemplateMeta(
            name="openrouter-kimi",
            family="kimi",
            preferred_provider="openrouter",
            credentials=("openrouter",),
        )
        spec = _StubModelSpec(
            name="kimi-k2.6",
            model_id="kimi-k2.6",
            family="kimi",
            provider_refs=(("openrouter", "moonshotai/kimi-k2.6"),),
            preferred_proxy="openrouter-kimi",
        )
        with self._patch_metas(_ALL_METAS + [kimi_meta]):
            routes = derive_model_routes(spec)

        assert routes[0].template_id == "openrouter-kimi"
        assert routes[0].model_ref == "moonshotai/kimi-k2.6"
        assert routes[0].credential == "openrouter"

    def test_deterministic_ordering(self):
        """Same input produces same output across calls."""
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
            preferred_proxy="openrouter-openai",
        )
        with self._patch_metas():
            routes1 = derive_model_routes(spec)
        with self._patch_metas():
            routes2 = derive_model_routes(spec)

        assert routes1 == routes2

    def test_all_model_refs_preserved(self):
        """model_ref on each route matches what was in provider_refs."""
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
        )
        with self._patch_metas():
            routes = derive_model_routes(spec)

        for r in routes:
            assert r.model_ref == "openai/gpt-5.5"

    def test_real_cross_family_openrouter_route_preserves_requested_ref(self):
        """Real template tier defaults must not overwrite the requested provider ref."""
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
        )

        routes = derive_model_routes(spec)
        cross_route = next(r for r in routes if r.template_id == "openrouter-anthropic")

        assert cross_route.template_family == "anthropic"
        assert cross_route.model_ref == "openai/gpt-5.5"


# ── resolve_invocation_routing ───────────────────────────────────


class TestResolveInvocationRouting:

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        clear_template_cache()
        yield
        clear_template_cache()

    def _patch_metas(self, metas=None):
        return patch(
            "forge.review.routing._all_template_metas",
            return_value=metas if metas is not None else _ALL_METAS,
        )

    def _patch_resolver(self, result):
        return patch(
            "forge.core.reactive.routing.resolve_subprocess_routing",
            return_value=result,
        )

    def test_direct_only_spec_bypasses_resolver(self):
        """Direct-only specs short-circuit; resolver is not called."""
        spec = _StubModelSpec(
            name="claude-opus",
            model_id="claude-opus",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        with self._patch_metas(), self._patch_resolver(None) as mock_resolver:
            plan = resolve_invocation_routing([spec])

        mock_resolver.assert_not_called()
        assert len(plan.routes) == 1
        assert plan.routes[0].source == "direct"
        assert plan.routes[0].route is not None
        assert plan.routes[0].route.provider == "direct"
        assert plan.routes[0].credential == "anthropic-api"

    def test_direct_only_with_via_emits_warning(self):
        spec = _StubModelSpec(
            name="claude-opus",
            model_id="claude-opus",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        with self._patch_metas():
            plan = resolve_invocation_routing([spec], via="openrouter-openai")

        assert plan.routes[0].warning is not None
        assert "--via ignored" in plan.routes[0].warning
        assert plan.via_override == "openrouter-openai"

    def test_proxy_spec_calls_resolver(self):
        """Proxy-capable specs go through the full resolver."""
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
            preferred_proxy="openrouter-openai",
        )
        mock_result = RoutingResult(
            base_url="http://localhost:8096",
            proxy_id="openrouter-openai",
            template="openrouter-openai",
            source="preferred_proxy",
            route=ModelRoute(
                provider="openrouter",
                credential="openrouter",
                family="openai",
                template_id="openrouter-openai",
                template_family="openai",
                model_ref="openai/gpt-5.5",
            ),
            credential="openrouter",
        )
        with self._patch_metas(), self._patch_resolver(mock_result):
            plan = resolve_invocation_routing([spec])

        assert len(plan.routes) == 1
        assert plan.routes[0].source == "preferred_proxy"

    def test_proxy_spec_logs_routing_decision(self, caplog):
        """Plan resolution emits a consolidated routing decision line."""
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
            preferred_proxy="openrouter-openai",
        )
        mock_result = RoutingResult(
            base_url="http://localhost:8096",
            proxy_id="openrouter-openai",
            template="openrouter-openai",
            source="preferred_proxy",
            route=ModelRoute(
                provider="openrouter",
                credential="openrouter",
                family="openai",
                template_id="openrouter-openai",
                template_family="openai",
                model_ref="openai/gpt-5.5",
            ),
            credential="openrouter",
        )

        with (
            self._patch_metas(),
            self._patch_resolver(mock_result),
            caplog.at_level(
                logging.INFO,
                logger="forge.review.routing",
            ),
        ):
            resolve_invocation_routing([spec])

        assert "Routing decision: model=gpt-5.5 source=preferred_proxy" in caplog.text
        assert "proxy=openrouter-openai" in caplog.text
        assert "template=openrouter-openai" in caplog.text
        assert "model_ref=openai/gpt-5.5" in caplog.text

    def test_fail_closed_on_unresolved(self, monkeypatch):
        """Workflow raises when a spec has no route."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
        )
        unresolved = RoutingResult(
            base_url=None,
            proxy_id=None,
            template=None,
            source="unresolved",
            route=None,
            credential=None,
        )
        with self._patch_metas(), self._patch_resolver(unresolved):
            with pytest.raises(RuntimeError, match="No running proxy"):
                resolve_invocation_routing([spec])

    def test_fail_closed_error_mentions_credential_when_missing(self, monkeypatch):
        """Error mentions credential when the key is not configured."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
        )
        unresolved = RoutingResult(
            base_url=None,
            proxy_id=None,
            template=None,
            source="unresolved",
            route=None,
            credential=None,
        )

        with self._patch_metas(), self._patch_resolver(unresolved):
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                resolve_invocation_routing([spec])

    def test_mixed_direct_and_proxy_specs(self):
        """Batch with both direct and proxy specs resolves both."""
        direct_spec = _StubModelSpec(
            name="claude-opus",
            model_id="claude-opus",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        proxy_spec = _StubModelSpec(
            name="gpt-5.5",
            model_id="gpt-5.5",
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"),),
            preferred_proxy="openrouter-openai",
        )
        mock_result = RoutingResult(
            base_url="http://localhost:8096",
            proxy_id="openrouter-openai",
            template="openrouter-openai",
            source="preferred_proxy",
            route=ModelRoute(
                provider="openrouter",
                credential="openrouter",
                family="openai",
                template_id="openrouter-openai",
                template_family="openai",
                model_ref="openai/gpt-5.5",
            ),
            credential="openrouter",
        )
        with self._patch_metas(), self._patch_resolver(mock_result):
            plan = resolve_invocation_routing([direct_spec, proxy_spec])

        assert len(plan.routes) == 2
        assert plan.routes[0].source == "direct"
        assert plan.routes[1].source == "preferred_proxy"

    def test_plan_has_resolved_at_timestamp(self):
        spec = _StubModelSpec(
            name="claude-opus",
            model_id="claude-opus",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6"),),
        )
        with self._patch_metas():
            plan = resolve_invocation_routing([spec])

        assert plan.resolved_at
        assert "T" in plan.resolved_at
