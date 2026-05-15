"""Workflow-specific routing types and functions.

Builds on the shared primitives in ``forge.core.reactive.routing``
to add workflow-specific types (``WorkerRoutingPlan``) and functions
(``derive_model_routes``, ``resolve_invocation_routing``,
``resolve_model_flag``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from forge.core.reactive.routing import ModelRoute, RoutingResult


@runtime_checkable
class RoutableSpec(Protocol):
    """Structural protocol for model specs that can be routed.

    Decouples routing from the concrete ``ModelSpec`` dataclass so
    Phase 2 is type-check clean before Phase 3 adds these fields.
    Once ``ModelSpec`` gains these fields, it satisfies this protocol
    implicitly (structural subtyping).
    """

    @property
    def name(self) -> str: ...
    @property
    def family(self) -> str: ...
    @property
    def provider_refs(self) -> tuple[tuple[str, str], ...]: ...
    @property
    def preferred_proxy(self) -> str | None: ...


_log = logging.getLogger(__name__)

# Direct workers run claude -p --bare which needs ANTHROPIC_API_KEY
_DIRECT_CREDENTIAL = "anthropic-api"

# Providers that pass model IDs through to the upstream (OpenRouter routes
# by explicit model ref regardless of the template's native family).
_PASSTHROUGH_PROVIDERS = frozenset({"openrouter"})


@dataclass(frozen=True)
class WorkerRoutingPlan:
    """Pre-resolved routing for all workers in a workflow invocation.

    Created once at invocation start. Frozen and passed to each worker.
    ``routes`` is indexed by worker position (same order as spec list).
    """

    routes: tuple[RoutingResult, ...]
    resolved_at: str
    via_override: str | None


def resolve_model_flag(route: ModelRoute) -> str | None:
    """Return the ``--model`` flag for a routed workflow worker.

    Direct workers use Claude Code env pins instead of ``--model``.
    Proxied workers always send an explicit model ref so ``--models``
    means the same thing regardless of which compatible proxy was selected.
    """
    if route.provider == "direct":
        return None
    return route.model_ref


# ── Template metadata cache ──────────────────────────────────────


@dataclass(frozen=True)
class _TemplateMeta:
    """Cached static metadata for one proxy template."""

    name: str
    family: str
    preferred_provider: str
    credentials: tuple[str, ...]


_template_cache: dict[str, _TemplateMeta] = {}


def _get_template_meta(template_name: str) -> _TemplateMeta | None:
    """Load and cache template metadata (family, provider, credentials)."""
    if template_name in _template_cache:
        return _template_cache[template_name]

    try:
        import yaml

        from forge.config.loader import read_template

        content = read_template(template_name)
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return None

        proxy_block = data.get("proxy", {})
        if not isinstance(proxy_block, dict):
            return None

        family = proxy_block.get("family", "")
        provider = proxy_block.get("preferred_provider", "")
        if not family or not provider:
            return None

        from forge.core.auth.capabilities import credentials_for_template

        creds = credentials_for_template(template_name)
        cred_names = tuple(c.name for c in creds)

        meta = _TemplateMeta(
            name=template_name,
            family=family,
            preferred_provider=provider,
            credentials=cred_names,
        )
        _template_cache[template_name] = meta
        return meta
    except Exception:
        _log.debug("Could not load metadata for template '%s'", template_name, exc_info=True)
        return None


def _all_template_metas() -> list[_TemplateMeta]:
    """Load metadata for all available templates."""
    from forge.config.loader import list_template_names

    result: list[_TemplateMeta] = []
    for name in list_template_names(include_internal=False):
        meta = _get_template_meta(name)
        if meta:
            result.append(meta)
    return result


def clear_template_cache() -> None:
    """Clear the template metadata cache (for testing)."""
    _template_cache.clear()


# ── Route derivation ─────────────────────────────────────────────


def derive_model_routes(spec: RoutableSpec) -> tuple[ModelRoute, ...]:
    """Expand compact model metadata into concrete routing options.

    For each provider ref on the model, inspect known proxy templates
    and credential metadata to build route records. Does not inspect
    the proxy registry or check running state.

    Ranking (deterministic):
    1. preferred_proxy match first (if it matches a derived route)
    2. provider_refs order
    3. Native-family templates before cross-family passthrough
    4. Alphabetical template name tiebreaker
    """
    all_metas = _all_template_metas()
    routes: list[ModelRoute] = []
    preferred_routes: list[ModelRoute] = []

    for provider_ns, model_ref in spec.provider_refs:
        if provider_ns == "direct":
            route = ModelRoute(
                provider="direct",
                credential=_DIRECT_CREDENTIAL,
                family=spec.family,
                template_id=None,
                template_family=None,
                model_ref=model_ref,
            )
            if spec.preferred_proxy is None:
                preferred_routes.append(route)
            else:
                routes.append(route)
            continue

        matching = _find_matching_templates(provider_ns, spec.family, all_metas)

        for meta in matching:
            cred = meta.credentials[0] if meta.credentials else provider_ns
            route = ModelRoute(
                provider=provider_ns,
                credential=cred,
                family=spec.family,
                template_id=meta.name,
                template_family=meta.family,
                model_ref=model_ref,
            )
            if spec.preferred_proxy and meta.name == spec.preferred_proxy:
                preferred_routes.append(route)
            else:
                routes.append(route)

    return tuple(preferred_routes + routes)


def _find_matching_templates(
    provider_ns: str,
    model_family: str,
    all_metas: list[_TemplateMeta],
) -> list[_TemplateMeta]:
    """Find templates compatible with a provider namespace.

    Returns native-family templates first, then cross-family passthrough
    templates (for passthrough providers like OpenRouter), sorted
    alphabetically within each group.
    """
    native: list[_TemplateMeta] = []
    cross_family: list[_TemplateMeta] = []

    for meta in all_metas:
        if meta.preferred_provider != provider_ns:
            continue

        if meta.family == model_family:
            native.append(meta)
        elif provider_ns in _PASSTHROUGH_PROVIDERS:
            cross_family.append(meta)

    native.sort(key=lambda m: m.name)
    cross_family.sort(key=lambda m: m.name)
    return native + cross_family


# ── Invocation routing ───────────────────────────────────────────


def resolve_invocation_routing(
    specs: Sequence[Any],
    via: str | None = None,
) -> WorkerRoutingPlan:
    """Resolve routing for all workers at invocation start.

    Called once by the workflow CLI command. Fail-closed: raises if
    any worker has no route.
    """
    from forge.core.reactive.routing import resolve_subprocess_routing
    from forge.core.state import now_iso

    results: list[RoutingResult] = []

    for spec in specs:
        routes = derive_model_routes(spec)

        direct_only = bool(routes) and all(r.provider == "direct" for r in routes)
        if direct_only:
            result = _resolve_direct_spec(spec, routes, via)
        else:
            result = resolve_subprocess_routing(
                explicit_proxy=via,
                preferred_proxy=spec.preferred_proxy,
                routes=routes,
                require_route=True,
                advisory_check=True,
            )

        if result.route is None:
            _raise_no_route_error(spec, routes)

        _log_routing_decision(spec, result)
        results.append(result)

    return WorkerRoutingPlan(
        routes=tuple(results),
        resolved_at=now_iso(),
        via_override=via,
    )


def _log_routing_decision(spec: RoutableSpec, result: RoutingResult) -> None:
    """Emit one consolidated line for workflow routing observability."""
    route = result.route
    model_ref = route.model_ref if route else "(none)"
    template = result.template or (route.template_id if route else None) or "(direct)"
    proxy = result.proxy_id or "(direct)"
    _log.info(
        "Routing decision: model=%s source=%s proxy=%s template=%s model_ref=%s",
        spec.name,
        result.source,
        proxy,
        template,
        model_ref,
    )


def _resolve_direct_spec(
    spec: RoutableSpec,
    routes: tuple[ModelRoute, ...],
    via: str | None,
) -> RoutingResult:
    """Build a RoutingResult for a direct-only spec, bypassing the resolver."""
    direct_route = next((r for r in routes if r.provider == "direct"), None)
    if direct_route is None:
        _raise_no_route_error(spec, routes)

    warning = None
    if via:
        warning = f"Worker '{spec.name}' uses direct Anthropic routing; --via ignored."

    return RoutingResult(
        base_url=None,
        proxy_id=None,
        template=None,
        source="direct",
        route=direct_route,
        credential=_DIRECT_CREDENTIAL,
        warning=warning,
    )


def _raise_no_route_error(spec: RoutableSpec, routes: tuple[ModelRoute, ...]) -> None:
    """Raise an actionable error when no route resolves for a workflow worker.

    Distinguishes "missing credential" from "credential configured but no
    proxy running" to avoid sending the user to forge auth login when they
    need forge proxy create/start instead.
    """
    try:
        from forge.core.auth.capabilities import (
            CREDENTIALS,
            format_missing_credential_error,
        )
        from forge.core.auth.template_secrets import resolve_env_or_credential

        if not routes:
            raise RuntimeError(
                f"No routes derived for model '{spec.name}' (family={spec.family}).\n"
                f"Tip: Run 'forge proxy create <template>' for a compatible proxy,\n"
                f"     or 'forge auth login' to configure credentials."
            )

        cred_name = routes[0].credential
        cred = CREDENTIALS.get(cred_name)

        if cred:
            missing_vars = [ev.name for ev in cred.env_vars if ev.required and not resolve_env_or_credential(ev.name)]
            if missing_vars:
                raise RuntimeError(
                    format_missing_credential_error(
                        cred,
                        missing_vars=missing_vars,
                        context=f"Workflow model '{spec.name}'",
                    )
                )

        template_ids = [r.template_id for r in routes if r.template_id]
        if template_ids:
            templates_str = ", ".join(template_ids[:3])
            raise RuntimeError(
                f"No running proxy found for model '{spec.name}'.\n"
                f"  Compatible templates: {templates_str}\n"
                f"  Tip: Run 'forge proxy create {template_ids[0]}' to create one,\n"
                f"       or 'forge proxy start <id>' if one exists."
            )

        raise RuntimeError(f"No route found for model '{spec.name}' (family={spec.family}).")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError(f"No route found for model '{spec.name}' (family={spec.family}).")
