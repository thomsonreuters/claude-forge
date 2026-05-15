"""Environment builder for Claude subprocess invocation.

Provides ``build_claude_env()`` for constructing subprocess environments,
and ``FORGE_DEPTH`` helpers for recursion-guarding hook → subprocess chains.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Defense-in-depth: --bare prevents hook recursion in child processes,
# but FORGE_DEPTH still guards against subprocess spawning at depth >= 2.
FORGE_DEPTH_VAR = "FORGE_DEPTH"
FORGE_MAX_DEPTH = 2

FORGE_SUBPROCESS_PROXY_VAR = "FORGE_SUBPROCESS_PROXY"
FORGE_SUBPROCESS_BASE_URL_VAR = "FORGE_SUBPROCESS_BASE_URL"
FORGE_SUBPROCESS_PROXY_ID_VAR = "FORGE_SUBPROCESS_PROXY_ID"
FORGE_SUBPROCESS_TEMPLATE_VAR = "FORGE_SUBPROCESS_TEMPLATE"
FORGE_SIDECAR_VAR = "FORGE_SIDECAR"
FORGE_LAUNCH_MODE_VAR = "FORGE_LAUNCH_MODE"

# --bare (Claude Code >= 2.1.81) disables OAuth/keychain auth, requiring
# ANTHROPIC_API_KEY in the environment. Only safe when the key is present.
_BARE_AUTH_KEY = "ANTHROPIC_API_KEY"


def can_use_bare(env: Mapping[str, str] | None = None) -> bool:
    """True if ``--bare`` is safe for headless subprocesses.

    ``--bare`` disables OAuth/keychain auth, so it requires
    ANTHROPIC_API_KEY. When an explicit ``env`` dict is given, checks
    only that dict (caller owns the env). When using os.environ
    (default), also falls back to the credential file via
    ``resolve_env_or_credential`` (which respects ``auth_ignore_env``).
    """
    if env is not None:
        return bool(env.get(_BARE_AUTH_KEY))

    from forge.core.auth.template_secrets import resolve_env_or_credential

    return bool(resolve_env_or_credential(_BARE_AUTH_KEY))


def get_forge_depth(env: Mapping[str, str] | None = None) -> int:
    """Read current FORGE_DEPTH from the given env (or os.environ).

    Invalid or missing values are treated as 0 (fail-open).
    """
    source = env if env is not None else os.environ
    raw = source.get(FORGE_DEPTH_VAR, "0")
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return 0


def should_spawn_subprocesses(env: Mapping[str, str] | None = None) -> bool:
    """True if current depth allows spawning ``claude -p`` subprocesses.

    Returns False when depth >= FORGE_MAX_DEPTH, meaning hooks should skip
    subprocess-spawning work (supervisor, handoff agent, etc.) to prevent
    runaway recursion.
    """
    return get_forge_depth(env) < FORGE_MAX_DEPTH


def build_claude_env(
    base_url: str | None = None,
    extra_vars: dict[str, str] | None = None,
    direct: bool = False,
) -> dict[str, str]:
    """Build environment dict for a Claude subprocess.

    Starts with the current process environment. Sets ANTHROPIC_BASE_URL
    if ``base_url`` is provided. When ``direct`` is True, removes any
    inherited ANTHROPIC_BASE_URL and subprocess proxy so the child hits
    Anthropic directly.
    Applies ``extra_vars`` before routing and depth handling so explicit
    function arguments remain authoritative.

    Hydrates ANTHROPIC_API_KEY from the credential file when it's not in
    the env (or when ``auth_ignore_env`` overrides it). This ensures
    ``can_use_bare(env)`` and the subprocess both see the resolved key.

    Args:
        base_url: Proxy URL to route Claude requests through.
        extra_vars: Additional environment variables to set/override.
        direct: Force direct Anthropic routing (unset inherited proxy URL).

    Returns:
        Complete environment dict ready for ``subprocess.run(env=...)``.
    """
    env = os.environ.copy()
    _hydrate_credentials(env)

    # Apply extra_vars AFTER hydration so explicit caller overrides
    # take precedence over credential-file values.
    if extra_vars:
        env.update(extra_vars)

    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    elif direct:
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop(FORGE_SUBPROCESS_PROXY_VAR, None)
        env.pop(FORGE_SUBPROCESS_BASE_URL_VAR, None)
        env.pop(FORGE_SUBPROCESS_PROXY_ID_VAR, None)
        env.pop(FORGE_SUBPROCESS_TEMPLATE_VAR, None)
    else:
        # No explicit base_url and not forced direct: check subprocess proxy fallback.
        # FORGE_SUBPROCESS_PROXY is set by `forge session start --subprocess-proxy`
        # and inherited by all child processes.
        injected_subprocess_base_url = env.get(FORGE_SUBPROCESS_BASE_URL_VAR)
        if injected_subprocess_base_url:
            env["ANTHROPIC_BASE_URL"] = injected_subprocess_base_url
        elif subprocess_proxy := env.get(FORGE_SUBPROCESS_PROXY_VAR):
            resolved = _resolve_subprocess_proxy(subprocess_proxy)
            if resolved:
                env["ANTHROPIC_BASE_URL"] = resolved
            else:
                env.pop("ANTHROPIC_BASE_URL", None)

    # Increment FORGE_DEPTH so child subprocesses know their nesting level
    current_depth = get_forge_depth(env)
    env[FORGE_DEPTH_VAR] = str(current_depth + 1)

    return env


def _hydrate_credentials(env: dict[str, str]) -> None:
    """Ensure resolved credentials are in the subprocess env dict.

    When ``auth_ignore_env`` is active, removes the inherited env value
    for ANTHROPIC_API_KEY and injects the credential-file value instead.
    When inactive, injects the credential-file value only if the env
    var is absent (so ``can_use_bare(env)`` and the subprocess agree).
    """
    from forge.core.auth.template_secrets import resolve_env_or_credential

    resolved = resolve_env_or_credential(_BARE_AUTH_KEY)

    try:
        from forge.runtime_config import get_runtime_config

        ignore_env = get_runtime_config().auth_ignore_env
    except Exception as e:
        logger.debug("Could not read auth_ignore_env; using environment credentials: %s", e)
        ignore_env = False

    if ignore_env:
        if resolved:
            env[_BARE_AUTH_KEY] = resolved
        else:
            env.pop(_BARE_AUTH_KEY, None)
    elif resolved and not env.get(_BARE_AUTH_KEY):
        env[_BARE_AUTH_KEY] = resolved


def _resolve_subprocess_proxy(proxy_id: str) -> str | None:
    """Resolve subprocess proxy to a base URL, or None if unavailable."""
    try:
        from forge.core.reactive.proxy import lookup_proxy_base_url

        url = lookup_proxy_base_url(proxy_id)
        if url:
            logger.debug("Subprocess proxy %r resolved to %s", proxy_id, url)
        return url
    except Exception as e:
        logger.warning("Subprocess proxy %r unavailable: %s", proxy_id, e)
        return None
