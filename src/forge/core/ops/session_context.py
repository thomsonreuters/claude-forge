"""Session context introspection (command-core).

Builds a structured view of everything Forge knows about a session:
metadata, proxy routing, model family, tier mappings, and policy state.

Used by:
- ``forge session context`` CLI command
- Skills auto-detecting model family via ``--field model_family``
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.session import (
    ForgeSessionError,
    SessionManager,
    SessionState,
    SessionStore,
    compute_effective_intent,
)
from forge.session.exceptions import AmbiguousSessionError
from forge.session.index import IndexStore

_log = logging.getLogger(__name__)

# Vendor prefix → normalized family name
_VENDOR_TO_FAMILY: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "vertex_ai": "gemini",
    "vertex_ai_beta": "gemini",
    "google": "gemini",
}


@dataclass(frozen=True)
class ProxyContext:
    """Proxy routing snapshot for a session."""

    template: str | None = None
    base_url: str | None = None
    proxy_id: str | None = None
    is_direct: bool = True


@dataclass(frozen=True)
class PolicyContext:
    """Policy state snapshot for a session."""

    enabled: bool = False
    fail_mode: str = "open"
    bundles: list[str] = field(default_factory=list)
    supervisor_resume_id: str | None = None


@dataclass(frozen=True)
class SessionContext:
    """Complete introspection view of a Forge session."""

    session_name: str
    claude_session_id: str | None = None
    worktree_path: str | None = None
    project_root: str | None = None
    created_at: str | None = None
    is_fork: bool = False
    is_incognito: bool = False
    parent_session: str | None = None
    proxy: ProxyContext = field(default_factory=ProxyContext)
    model_family: str = "anthropic"
    main_model: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    policy: PolicyContext = field(default_factory=PolicyContext)
    overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON output."""
        return {
            "session_name": self.session_name,
            "claude_session_id": self.claude_session_id,
            "worktree_path": self.worktree_path,
            "project_root": self.project_root,
            "created_at": self.created_at,
            "is_fork": self.is_fork,
            "is_incognito": self.is_incognito,
            "parent_session": self.parent_session,
            "proxy": {
                "template": self.proxy.template,
                "base_url": self.proxy.base_url,
                "proxy_id": self.proxy.proxy_id,
                "is_direct": self.proxy.is_direct,
            },
            "model_family": self.model_family,
            "main_model": self.main_model,
            "model_profile": self.main_model,
            "models": dict(self.models),
            "policy": {
                "enabled": self.policy.enabled,
                "fail_mode": self.policy.fail_mode,
                "bundles": list(self.policy.bundles),
                "supervisor_resume_id": self.policy.supervisor_resume_id,
            },
            "overrides": dict(self.overrides),
        }


def resolve_session_identifier(session: str | None = None) -> tuple[str, str | None]:
    """Resolve a session identifier to a Forge session name and forge_root.

    Accepts a Forge session name, a Claude session UUID, or None.

    Resolution order:
    1. If ``session`` provided: try as name, then as UUID
    2. ``$FORGE_SESSION`` env var

    Returns:
        (session_name, forge_root) tuple. forge_root may be None if
        resolved via env var without index context.

    Raises:
        SessionContextError: if no session can be resolved.
    """
    manager = SessionManager()

    # Derive forge_root from CWD for scoped lookups
    _cwd_forge_root: str | None = None
    try:
        from forge.core.ops.context import find_forge_root

        _fr = find_forge_root(Path.cwd().resolve())
        if _fr:
            _cwd_forge_root = str(_fr)
    except Exception:
        pass

    if session:
        # Try as Forge session name (scoped to current project first, then unscoped)
        try:
            entry = manager.get_session_entry(session, forge_root=_cwd_forge_root)
            return session, entry.root
        except AmbiguousSessionError:
            raise  # Propagate with location list intact
        except ForgeSessionError:
            pass

        # Unscoped fallback for cross-project explicit references
        try:
            entry = manager.get_session_entry(session)
            return session, entry.root
        except AmbiguousSessionError:
            raise  # Propagate with location list intact
        except ForgeSessionError as e:
            # Check if it's corruption (in index but manifest bad) vs not found
            try:
                manager.get_session_entry(session)
            except ForgeSessionError:
                pass  # Not in index — fall through to UUID lookup
            else:
                raise SessionContextError(str(e)) from e

        # Try as Claude session UUID (cross-project)
        index = IndexStore()
        uuid_result = index.find_session_by_uuid(session)
        if uuid_result:
            return uuid_result[0], uuid_result[1]

        # Fall back to scanning session manifests when the index is stale.
        scan_result = _scan_manifests_for_uuid(session)
        if scan_result:
            return scan_result

        raise SessionContextError(f"No session found for '{session}' (tried as name and UUID)")

    # Fall back to env var. FORGE_SESSION is set by the Forge launcher, so the
    # session is authoritative by convention. Try scoped first, then unscoped.
    env_session = os.environ.get("FORGE_SESSION")
    if env_session:
        try:
            entry = manager.get_session_entry(env_session, forge_root=_cwd_forge_root)
            return env_session, entry.root
        except ForgeSessionError:
            try:
                entry = manager.get_session_entry(env_session)
                return env_session, entry.root
            except ForgeSessionError:
                pass

    raise SessionContextError("No session found (no argument, no $FORGE_SESSION)")


class SessionContextError(RuntimeError):
    """Raised when session context cannot be built."""


def detect_model_family(template: str | None) -> str:
    """Map a proxy template to a normalized model family name.

    Loads the template config, reads the opus-tier model name,
    and extracts the vendor prefix.

    Returns:
        ``"openai"`` | ``"gemini"`` | ``"anthropic"``
    """
    if template is None:
        return "anthropic"

    try:
        from forge.config.loader import load_config

        cfg = load_config(template=template)
        provider = cfg.proxy.get_provider()

        # Get the opus-tier model (most representative)
        opus_model = provider.tiers.opus
        if not opus_model:
            return "anthropic"

        return _model_to_family(opus_model)
    except Exception:
        _log.debug("Failed to detect model family for template %r", template, exc_info=True)
        return "anthropic"


def _model_to_family(model_name: str) -> str:
    """Extract normalized family from a model name (possibly vendor-prefixed).

    Examples:
        ``"openai/gpt-5.5"`` -> ``"openai"``
        ``"vertex_ai/gemini-3.1-pro"`` -> ``"gemini"``
        ``"gpt-5.5"`` -> ``"openai"``
        ``"claude-opus-4-6"`` -> ``"anthropic"``
    """
    # If vendor-prefixed (e.g., "openai/gpt-5.5"), extract prefix
    if "/" in model_name:
        vendor = model_name.split("/", 1)[0]
        family = _VENDOR_TO_FAMILY.get(vendor)
        if family:
            return family

    # Infer from model name pattern
    bare = model_name.split("/", 1)[-1].lower()

    if bare.startswith("gpt-") or bare.startswith(("o1", "o3", "o4")):
        return "openai"
    if bare.startswith("claude-"):
        return "anthropic"
    if bare.startswith("gemini-"):
        return "gemini"

    return "anthropic"


def get_session_context(session: str | None = None) -> SessionContext:
    """Build a complete context view of a session.

    The ``session`` arg accepts a Forge session name, a Claude session UUID,
    or None (falls back to ``$FORGE_SESSION``).

    When ``session`` is None and no Forge session can be resolved, falls back
    to building context from environment variables (``ACTIVE_TEMPLATE``,
    ``ANTHROPIC_BASE_URL``). When an explicit session identifier is given
    but cannot be resolved, raises instead of falling back.

    Returns:
        SessionContext with all available metadata.

    Raises:
        SessionContextError: if an explicit session identifier cannot be
            resolved, or if the resolved session's state is corrupted.
    """
    try:
        name, resolved_forge_root = resolve_session_identifier(session)
    except SessionContextError:
        if session is not None:
            raise  # Explicit session not found — fail-closed
        return _build_env_context()

    manager = SessionManager()
    try:
        state = manager.get_session(name, forge_root=resolved_forge_root)
        entry = manager.get_session_entry(name, forge_root=resolved_forge_root)
    except ForgeSessionError as e:
        raise SessionContextError(str(e)) from e

    proxy_ctx = _build_proxy_context(state)
    family, models, main_model = _build_model_context(proxy_ctx, state)
    policy_ctx = _build_policy_context(state)

    return SessionContext(
        session_name=name,
        claude_session_id=state.confirmed.claude_session_id,
        worktree_path=entry.worktree_path,
        project_root=entry.project_root,
        created_at=state.created_at,
        is_fork=state.is_fork,
        is_incognito=state.is_incognito,
        parent_session=state.parent_session,
        proxy=proxy_ctx,
        model_family=family,
        main_model=main_model,
        models=models,
        policy=policy_ctx,
        overrides=dict(state.overrides),
    )


def _build_env_context() -> SessionContext:
    """Build a minimal context from environment variables when no Forge session exists.

    Uses ``ACTIVE_TEMPLATE`` and ``ANTHROPIC_BASE_URL`` to infer proxy/model info.
    Called only when ``session`` was None and resolution found nothing.
    """
    template = os.environ.get("ACTIVE_TEMPLATE")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")

    if template or base_url:
        proxy_ctx = ProxyContext(
            template=template,
            base_url=base_url,
            is_direct=False,
        )
    else:
        proxy_ctx = ProxyContext(is_direct=True)

    family, models, main_model = _build_model_context(proxy_ctx, None)

    return SessionContext(
        session_name=os.environ.get("FORGE_SESSION", "(unknown)"),
        claude_session_id=None,
        model_family=family,
        main_model=main_model,
        models=models,
        proxy=proxy_ctx,
    )


def _build_proxy_context(state: SessionState) -> ProxyContext:
    """Extract proxy info from confirmed (preferred) or intent."""
    confirmed = state.confirmed.started_with_proxy
    if confirmed:
        return ProxyContext(
            template=confirmed.template,
            base_url=confirmed.base_url,
            proxy_id=confirmed.proxy_id,
            is_direct=False,
        )

    intent = state.intent.proxy
    if intent:
        return ProxyContext(
            template=intent.template,
            base_url=intent.base_url,
            proxy_id=None,
            is_direct=False,
        )

    return ProxyContext(is_direct=True)


def _build_policy_context(state: SessionState) -> PolicyContext:
    """Extract effective policy state from intent + overrides."""
    enabled = False
    fail_mode = "open"
    bundles: list[str] = []
    supervisor_resume_id: str | None = None

    try:
        effective_intent = compute_effective_intent(state)
        policy = effective_intent.policy
    except Exception:
        _log.debug("Failed to compute effective policy state for session %r", state.name, exc_info=True)
        policy = state.intent.policy

    if policy:
        enabled = policy.enabled
        fail_mode = policy.fail_mode or "open"
        bundles = list(policy.bundles or [])
        if policy.supervisor:
            supervisor_resume_id = policy.supervisor.resume_id

    return PolicyContext(
        enabled=enabled,
        fail_mode=fail_mode,
        bundles=bundles,
        supervisor_resume_id=supervisor_resume_id,
    )


def _scan_manifests_for_uuid(session_uuid: str) -> tuple[str, str] | None:
    """Search session manifests for a Claude UUID when the index is stale.

    Returns (display_name, forge_root) to preserve project scope for
    subsequent lookups, or None if not found.
    """
    index = IndexStore()
    try:
        sessions = index.list_sessions(include_incognito=True)
    except Exception:
        _log.debug("Failed to list sessions while scanning manifests for UUID %r", session_uuid, exc_info=True)
        return None

    for name, entry in sessions:
        try:
            store = SessionStore(entry.root, name)
            if not store.exists():
                continue
            state = store.read()
        except Exception:
            _log.debug("Failed to read session manifest while scanning for UUID %r", session_uuid, exc_info=True)
            continue

        if state.confirmed.claude_session_id == session_uuid:
            return name, entry.root

    return None


def _build_model_context(proxy_ctx: ProxyContext, state: SessionState | None) -> tuple[str, dict[str, str], str | None]:
    """Return model family plus tier mappings using the best available proxy truth."""
    if proxy_ctx.is_direct:
        main_model = None
        if state is not None and state.intent.launch is not None:
            main_model = state.intent.launch.direct_model
        if main_model is None:
            main_model = _direct_main_model_from_env()
        return "anthropic", {}, main_model

    proxy_config = _load_proxy_instance_for_context(proxy_ctx)
    if proxy_config is not None:
        models = _proxy_instance_tier_models(proxy_config)
        family = _family_from_models(models)
        main_model = models.get(getattr(proxy_config, "default_tier", None) or "sonnet")
        return family, models, main_model

    template = proxy_ctx.template
    family = detect_model_family(template)
    models = _get_tier_models(template)
    main_model = models.get("sonnet") or models.get("opus") or models.get("haiku")
    return family, models, main_model


def _direct_main_model_from_env() -> str | None:
    """Infer the pinned direct Claude model from Claude Code env vars."""
    tier = (os.environ.get("ANTHROPIC_MODEL") or "").lower()
    env_by_tier = {
        "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    }
    if tier in env_by_tier:
        return os.environ.get(env_by_tier[tier])

    return None


def _load_proxy_instance_for_context(proxy_ctx: ProxyContext) -> Any | None:
    """Load proxy.yaml for this session when we can identify the concrete proxy."""
    proxy_id = proxy_ctx.proxy_id

    if proxy_id is None and proxy_ctx.base_url:
        try:
            from forge.proxy.proxies import ProxyRegistryStore

            entry = ProxyRegistryStore().find_by_base_url(proxy_ctx.base_url)
            if entry is not None:
                proxy_id = entry.proxy_id
        except Exception:
            _log.debug("Failed to resolve proxy_id from base_url %r", proxy_ctx.base_url, exc_info=True)

    if proxy_id is None:
        return None

    try:
        from forge.config.loader import load_proxy_instance_config

        return load_proxy_instance_config(proxy_id)
    except Exception:
        _log.debug("Failed to load proxy instance config for %r", proxy_id, exc_info=True)
        return None


def _proxy_instance_tier_models(proxy_config: Any) -> dict[str, str]:
    """Extract tier mappings from a proxy instance config."""
    result: dict[str, str] = {}
    if proxy_config.tiers.haiku:
        result["haiku"] = proxy_config.tiers.haiku
    if proxy_config.tiers.sonnet:
        result["sonnet"] = proxy_config.tiers.sonnet
    if proxy_config.tiers.opus:
        result["opus"] = proxy_config.tiers.opus
    return result


def _family_from_models(models: dict[str, str]) -> str:
    """Choose a representative family from tier mappings."""
    for tier in ("opus", "sonnet", "haiku"):
        model_name = models.get(tier)
        if model_name:
            return _model_to_family(model_name)
    return "anthropic"


def _get_tier_models(template: str | None) -> dict[str, str]:
    """Load tier→model mappings from a template."""
    if template is None:
        return {}

    try:
        from forge.config.loader import load_config

        cfg = load_config(template=template)
        provider = cfg.proxy.get_provider()
        result: dict[str, str] = {}
        if provider.tiers.haiku:
            result["haiku"] = provider.tiers.haiku
        if provider.tiers.sonnet:
            result["sonnet"] = provider.tiers.sonnet
        if provider.tiers.opus:
            result["opus"] = provider.tiers.opus
        return result
    except Exception:
        _log.debug("Failed to load tier models for template %r", template, exc_info=True)
        return {}


def extract_field(data: dict[str, Any], field_path: str) -> Any:
    """Extract a value from a nested dict using dot notation.

    Args:
        data: The dict to traverse.
        field_path: Dot-separated path (e.g., ``"proxy.template"``).

    Returns:
        The value at the path.

    Raises:
        KeyError: if the path does not exist.
    """
    current: Any = data
    for part in field_path.split("."):
        if isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Cannot traverse into {type(current).__name__} at '{part}'")
    return current
