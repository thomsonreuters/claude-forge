"""Tests for shared subprocess routing primitives."""

from __future__ import annotations

from typing import get_args
from unittest.mock import MagicMock, patch

import pytest

from forge.core.reactive.routing import (
    ModelRoute,
    ProxyRoutingError,
    RoutingResult,
    RoutingSource,
    resolve_subprocess_routing,
)
from forge.proxy.proxies import ProxyRegistry

# ── ModelRoute ───────────────────────────────────────────────────


class TestModelRoute:

    def test_construction(self):
        route = ModelRoute(
            provider="openrouter",
            credential="openrouter",
            family="openai",
            template_id="openrouter-openai",
            template_family="openai",
            model_ref="openai/gpt-5.5",
        )
        assert route.provider == "openrouter"
        assert route.credential == "openrouter"
        assert route.family == "openai"
        assert route.template_id == "openrouter-openai"
        assert route.template_family == "openai"
        assert route.model_ref == "openai/gpt-5.5"

    def test_direct_route(self):
        route = ModelRoute(
            provider="direct",
            credential="anthropic-api",
            family="anthropic",
            template_id=None,
            template_family=None,
            model_ref="claude-opus-4-6",
        )
        assert route.provider == "direct"
        assert route.template_id is None
        assert route.template_family is None

    def test_frozen(self):
        route = ModelRoute(
            provider="openrouter",
            credential="openrouter",
            family="openai",
            template_id="openrouter-openai",
            template_family="openai",
            model_ref="openai/gpt-5.5",
        )
        with pytest.raises(AttributeError):
            route.provider = "litellm"  # type: ignore[misc]


# ── RoutingResult ────────────────────────────────────────────────


class TestRoutingResult:

    def test_proxied_result(self):
        route = ModelRoute(
            provider="openrouter",
            credential="openrouter",
            family="openai",
            template_id="openrouter-openai",
            template_family="openai",
            model_ref="openai/gpt-5.5",
        )
        result = RoutingResult(
            base_url="http://localhost:8096",
            proxy_id="openrouter-openai",
            template="openrouter-openai",
            source="preferred_proxy",
            route=route,
            credential="openrouter",
        )
        assert result.base_url == "http://localhost:8096"
        assert result.source == "preferred_proxy"
        assert result.route is not None
        assert result.warning is None

    def test_direct_result(self):
        route = ModelRoute(
            provider="direct",
            credential="anthropic-api",
            family="anthropic",
            template_id=None,
            template_family=None,
            model_ref="claude-opus-4-6",
        )
        result = RoutingResult(
            base_url=None,
            proxy_id=None,
            template=None,
            source="direct",
            route=route,
            credential="anthropic-api",
        )
        assert result.base_url is None
        assert result.source == "direct"
        assert result.route is not None
        assert result.route.provider == "direct"

    def test_unresolved_result(self):
        result = RoutingResult(
            base_url=None,
            proxy_id=None,
            template=None,
            source="unresolved",
            route=None,
            credential=None,
        )
        assert result.route is None
        assert result.source == "unresolved"

    def test_with_warning(self):
        result = RoutingResult(
            base_url="http://localhost:8095",
            proxy_id="openrouter-anthropic",
            template="openrouter-anthropic",
            source="route_scan",
            route=ModelRoute(
                provider="openrouter",
                credential="openrouter",
                family="openai",
                template_id="openrouter-anthropic",
                template_family="anthropic",
                model_ref="openai/gpt-5.5",
            ),
            credential="openrouter",
            warning="Routed through cross-family template; tier overrides may differ",
        )
        assert result.warning is not None
        assert "cross-family" in result.warning

    def test_all_source_values_covered(self):
        valid_sources = get_args(RoutingSource)
        assert len(valid_sources) == 7
        assert "explicit" in valid_sources
        assert "unresolved" in valid_sources
        assert "direct" in valid_sources


# ── resolve_subprocess_routing ───────────────────────────────────

_GPT_ROUTE = ModelRoute(
    provider="openrouter",
    credential="openrouter",
    family="openai",
    template_id="openrouter-openai",
    template_family="openai",
    model_ref="openai/gpt-5.5",
)

_GPT_CROSS_ROUTE = ModelRoute(
    provider="openrouter",
    credential="openrouter",
    family="openai",
    template_id="openrouter-anthropic",
    template_family="anthropic",
    model_ref="openai/gpt-5.5",
)

_LITELLM_OPENAI_ROUTE = ModelRoute(
    provider="litellm",
    credential="litellm-remote",
    family="openai",
    template_id="litellm-openai",
    template_family="openai",
    model_ref="openai/gpt-5.5",
)


def _mock_entry(
    proxy_id="openrouter-openai",
    template="openrouter-openai",
    base_url="http://localhost:8096",
    port=8096,
    pid=1234,
    status="healthy",
):
    entry = MagicMock()
    entry.proxy_id = proxy_id
    entry.template = template
    entry.base_url = base_url
    entry.port = port
    entry.pid = pid
    entry.status = status
    return entry


class TestResolveSubprocessRouting:

    def test_step1_explicit_base_url_wins(self):
        result = resolve_subprocess_routing(
            explicit_base_url="http://custom:9999",
            routes=(_GPT_ROUTE,),
        )
        assert result.source == "explicit"
        assert result.base_url == "http://custom:9999"
        assert result.route is None

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_step2_explicit_proxy(self, mock_lookup, mock_health):
        entry = _mock_entry()
        mock_lookup.return_value = entry

        result = resolve_subprocess_routing(
            explicit_proxy="openrouter-openai",
            routes=(_GPT_ROUTE,),
        )
        assert result.source == "explicit"
        assert result.base_url == "http://localhost:8096"
        assert result.route is _GPT_ROUTE
        mock_lookup.assert_called_once_with("openrouter-openai")
        mock_health.assert_called_once()

    @patch(
        "forge.core.reactive.routing._probe_proxy_metadata",
        return_value={"advertised_models": ("anthropic/claude-sonnet-4.5",)},
    )
    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_advisory_check_warns_when_model_not_in_proxy_tiers(self, mock_lookup, mock_health, mock_probe):
        entry = _mock_entry()
        mock_lookup.return_value = entry

        result = resolve_subprocess_routing(
            explicit_proxy="openrouter-openai",
            routes=(_GPT_ROUTE,),
            advisory_check=True,
        )

        assert result.source == "explicit"
        assert result.warning is not None
        assert "tier mappings do not advertise model 'openai/gpt-5.5'" in result.warning
        mock_probe.assert_called_once_with("http://localhost:8096")

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_cross_family_route_warns_about_tier_overrides(self, mock_lookup, mock_health):
        entry = _mock_entry(proxy_id="openrouter-anthropic", template="openrouter-anthropic")
        mock_lookup.return_value = entry

        result = resolve_subprocess_routing(
            explicit_proxy="openrouter-anthropic",
            routes=(_GPT_CROSS_ROUTE,),
        )

        assert result.source == "explicit"
        assert result.warning is not None
        assert "tier overrides may differ" in result.warning

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=False)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_step2_explicit_proxy_unreachable_raises(self, mock_lookup, mock_health):
        mock_lookup.return_value = _mock_entry()

        with pytest.raises(ProxyRoutingError, match="not reachable"):
            resolve_subprocess_routing(
                explicit_proxy="openrouter-openai",
                routes=(_GPT_ROUTE,),
            )

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_step2_explicit_proxy_incompatible_raises(self, mock_lookup, mock_health):
        entry = _mock_entry(template="litellm-gemini-local")
        mock_lookup.return_value = entry

        with pytest.raises(ProxyRoutingError, match="not compatible"):
            resolve_subprocess_routing(
                explicit_proxy="litellm-gemini-local",
                routes=(_GPT_ROUTE,),
            )

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_step2_explicit_proxy_same_credential_different_template_raises(self, mock_lookup, mock_health):
        """Same credential is not enough: template compatibility is exact."""
        entry = _mock_entry(
            proxy_id="litellm-gemini",
            template="litellm-gemini",
            base_url="http://localhost:8084",
            port=8084,
        )
        mock_lookup.return_value = entry

        with pytest.raises(ProxyRoutingError, match="not compatible"):
            resolve_subprocess_routing(
                explicit_proxy="litellm-gemini",
                routes=(_LITELLM_OPENAI_ROUTE,),
            )

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry_strict")
    def test_step3_subprocess_proxy(self, mock_lookup, mock_health, monkeypatch):
        entry = _mock_entry()
        mock_lookup.return_value = entry
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "openrouter-openai")

        result = resolve_subprocess_routing(routes=(_GPT_ROUTE,))
        assert result.source == "subprocess_proxy"

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    @patch("forge.core.reactive.routing.lookup_proxy_entry")
    def test_step4_preferred_proxy(self, mock_lookup, mock_health):
        entry = _mock_entry()
        mock_lookup.return_value = entry

        result = resolve_subprocess_routing(
            preferred_proxy="openrouter-openai",
            routes=(_GPT_ROUTE,),
        )
        assert result.source == "preferred_proxy"

    @patch("forge.core.reactive.routing.lookup_proxy_entry")
    def test_step4_preferred_proxy_missing_skips(self, mock_lookup):
        mock_lookup.return_value = None

        result = resolve_subprocess_routing(
            preferred_proxy="nonexistent",
            routes=(),
        )
        assert result.source == "unresolved"

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    def test_step5_route_scan(self, mock_health):
        entry = _mock_entry()
        mock_registry = MagicMock()
        mock_registry.proxies = {"openrouter-openai": entry}

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            MockStore.return_value.read.return_value = mock_registry
            result = resolve_subprocess_routing(routes=(_GPT_ROUTE,))

        assert result.source == "route_scan"
        assert result.base_url == "http://localhost:8096"

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=False)
    def test_step5_route_scan_skips_unreachable(self, mock_health):
        entry = _mock_entry(pid=None)
        mock_registry = MagicMock()
        mock_registry.proxies = {"openrouter-openai": entry}

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            MockStore.return_value.read.return_value = mock_registry
            result = resolve_subprocess_routing(routes=(_GPT_ROUTE,))

        assert result.source == "unresolved"

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    def test_step5_route_scan_adopted_proxy_no_pid(self, mock_health):
        """Adopted proxy with pid=None but healthy endpoint is selected."""
        entry = _mock_entry(pid=None)
        mock_registry = MagicMock()
        mock_registry.proxies = {"openrouter-openai": entry}

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            MockStore.return_value.read.return_value = mock_registry
            result = resolve_subprocess_routing(routes=(_GPT_ROUTE,))

        assert result.source == "route_scan"

    def test_step6_session_proxy_accepted_by_supervisor(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8095")

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            mock_registry = MagicMock()
            mock_registry.proxies = {}
            MockStore.return_value.read.return_value = mock_registry

            result = resolve_subprocess_routing(require_route=False)

        assert result.source == "session_proxy"
        assert result.base_url == "http://localhost:8095"

    def test_step6_session_proxy_rejected_by_workflow(self, monkeypatch):
        """Opaque session proxy with no route match is unresolved for workflows."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8095")

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            mock_registry = MagicMock()
            mock_registry.proxies = {}
            MockStore.return_value.read.return_value = mock_registry

            result = resolve_subprocess_routing(
                routes=(_GPT_ROUTE,),
                require_route=True,
            )

        assert result.source == "unresolved"
        assert result.warning is not None

    def test_step6_session_proxy_metadata_satisfies_workflow(self, monkeypatch):
        """Live Forge proxy metadata can turn ANTHROPIC_BASE_URL into a routed workflow proxy."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8096")

        with (
            patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore,
            patch("forge.core.reactive.routing._probe_proxy_metadata") as mock_probe,
        ):
            mock_registry = MagicMock()
            mock_registry.proxies = {}
            MockStore.return_value.read.return_value = mock_registry
            mock_probe.return_value = {
                "template": "openrouter-openai",
                "proxy_id": "live-proxy",
            }

            result = resolve_subprocess_routing(
                routes=(_GPT_ROUTE,),
                require_route=True,
            )

        assert result.source == "session_proxy"
        assert result.base_url == "http://localhost:8096"
        assert result.proxy_id == "live-proxy"
        assert result.template == "openrouter-openai"
        assert result.route is _GPT_ROUTE
        mock_probe.assert_called_once_with("http://localhost:8096")

    @patch("forge.core.reactive.routing._probe_proxy_metadata", return_value=None)
    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=False)
    def test_step6_session_proxy_registry_match_must_be_reachable_for_workflow(
        self,
        mock_health,
        mock_probe,
        monkeypatch,
    ):
        """Registry reverse lookups are health-checked before workflows trust them."""
        monkeypatch.setenv("FORGE_SIDECAR", "1")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8096")
        entry = _mock_entry()
        registry = ProxyRegistry(proxies={"openrouter-openai": entry})

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            MockStore.return_value.read.return_value = registry
            result = resolve_subprocess_routing(
                routes=(_GPT_ROUTE,),
                require_route=True,
            )

        assert result.source == "unresolved"
        assert result.warning is not None
        assert "health validation" in result.warning
        mock_health.assert_called_once_with(entry)
        mock_probe.assert_called_once_with("http://localhost:8096")

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    def test_step6_session_proxy_registry_match_uses_reachable_route(self, mock_health, monkeypatch):
        monkeypatch.setenv("FORGE_SIDECAR", "1")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8096")
        entry = _mock_entry()
        registry = ProxyRegistry(proxies={"openrouter-openai": entry})

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            MockStore.return_value.read.return_value = registry
            result = resolve_subprocess_routing(
                routes=(_GPT_ROUTE,),
                require_route=True,
            )

        assert result.source == "session_proxy"
        assert result.route is _GPT_ROUTE
        mock_health.assert_called_once_with(entry)

    def test_step7_unresolved(self):
        result = resolve_subprocess_routing()
        assert result.source == "unresolved"
        assert result.route is None
        assert result.base_url is None

    def test_explicit_base_url_beats_explicit_proxy(self):
        """Step 1 wins over step 2."""
        result = resolve_subprocess_routing(
            explicit_base_url="http://custom:9999",
            explicit_proxy="openrouter-openai",
            routes=(_GPT_ROUTE,),
        )
        assert result.source == "explicit"
        assert result.base_url == "http://custom:9999"

    def test_sidecar_skips_registry_steps(self, monkeypatch):
        monkeypatch.setenv("FORGE_SIDECAR", "1")

        with pytest.raises(ProxyRoutingError, match="sidecar"):
            resolve_subprocess_routing(
                explicit_proxy="openrouter-openai",
                routes=(_GPT_ROUTE,),
            )

    def test_sidecar_allows_explicit_base_url(self, monkeypatch):
        monkeypatch.setenv("FORGE_SIDECAR", "1")

        result = resolve_subprocess_routing(
            explicit_base_url="http://host.docker.internal:8096",
        )
        assert result.source == "explicit"
        assert result.base_url == "http://host.docker.internal:8096"

    def test_sidecar_falls_through_to_session_proxy(self, monkeypatch):
        monkeypatch.setenv("FORGE_SIDECAR", "1")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://host.docker.internal:8095")

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            mock_registry = MagicMock()
            mock_registry.proxies = {}
            MockStore.return_value.read.return_value = mock_registry

            result = resolve_subprocess_routing(require_route=False)

        assert result.source == "session_proxy"

    def test_sidecar_uses_injected_subprocess_proxy_metadata(self, monkeypatch):
        monkeypatch.setenv("FORGE_SIDECAR", "1")
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "openrouter-openai")
        monkeypatch.setenv("FORGE_SUBPROCESS_BASE_URL", "http://host.docker.internal:8096")
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY_ID", "openrouter-openai")
        monkeypatch.setenv("FORGE_SUBPROCESS_TEMPLATE", "openrouter-openai")

        result = resolve_subprocess_routing(
            routes=(_GPT_ROUTE,),
            require_route=True,
        )

        assert result.source == "subprocess_proxy"
        assert result.base_url == "http://host.docker.internal:8096"
        assert result.proxy_id == "openrouter-openai"
        assert result.template == "openrouter-openai"
        assert result.route is _GPT_ROUTE

    def test_sidecar_injected_subprocess_proxy_requires_compatible_template(self, monkeypatch):
        monkeypatch.setenv("FORGE_SIDECAR", "1")
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY", "litellm-gemini")
        monkeypatch.setenv("FORGE_SUBPROCESS_BASE_URL", "http://host.docker.internal:8084")
        monkeypatch.setenv("FORGE_SUBPROCESS_PROXY_ID", "litellm-gemini")
        monkeypatch.setenv("FORGE_SUBPROCESS_TEMPLATE", "litellm-gemini")

        with pytest.raises(ProxyRoutingError, match="Injected subprocess proxy metadata"):
            resolve_subprocess_routing(
                routes=(_GPT_ROUTE,),
                require_route=True,
            )

    @patch("forge.core.reactive.routing._check_proxy_reachable", return_value=True)
    def test_route_scan_prefers_route_order(self, mock_health):
        """Route scan ranks by route derivation order, not alphabetical proxy."""
        entry_a = _mock_entry(proxy_id="a-proxy", template="openrouter-anthropic")
        entry_o = _mock_entry(proxy_id="o-proxy", template="openrouter-openai")
        mock_registry = MagicMock()
        mock_registry.proxies = {"a-proxy": entry_a, "o-proxy": entry_o}

        with patch("forge.proxy.proxies.ProxyRegistryStore") as MockStore:
            MockStore.return_value.read.return_value = mock_registry
            result = resolve_subprocess_routing(
                routes=(_GPT_ROUTE, _GPT_CROSS_ROUTE),
            )

        assert result.source == "route_scan"
        assert result.proxy_id == "o-proxy"
