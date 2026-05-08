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

# --bare (Claude Code >= 2.1.81) disables OAuth/keychain auth, requiring
# ANTHROPIC_API_KEY in the environment. Only safe when the key is present.
_BARE_AUTH_KEY = "ANTHROPIC_API_KEY"


def can_use_bare(env: Mapping[str, str] | None = None) -> bool:
    """True if ``--bare`` is safe for headless subprocesses.

    ``--bare`` disables OAuth/keychain auth, so it requires
    ANTHROPIC_API_KEY in the environment. Returns False when the
    key is absent (the subprocess would fail to authenticate).
    """
    source = env if env is not None else os.environ
    return bool(source.get(_BARE_AUTH_KEY))


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
    inherited ANTHROPIC_BASE_URL so the child hits Anthropic directly.
    Increments FORGE_DEPTH for the child process. Merges ``extra_vars``
    last (highest priority).

    Args:
        base_url: Proxy URL to route Claude requests through.
        extra_vars: Additional environment variables to set/override.
        direct: Force direct Anthropic routing (unset inherited proxy URL).

    Returns:
        Complete environment dict ready for ``subprocess.run(env=...)``.
    """
    env = os.environ.copy()

    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    elif direct:
        env.pop("ANTHROPIC_BASE_URL", None)
    else:
        # No explicit base_url and not forced direct: check subprocess proxy fallback.
        # FORGE_SUBPROCESS_PROXY is set by `forge session start --subprocess-proxy`
        # and inherited by all child processes.
        subprocess_proxy = env.get(FORGE_SUBPROCESS_PROXY_VAR)
        if subprocess_proxy:
            resolved = _resolve_subprocess_proxy(subprocess_proxy)
            if resolved:
                env["ANTHROPIC_BASE_URL"] = resolved
            else:
                env.pop("ANTHROPIC_BASE_URL", None)

    # Increment FORGE_DEPTH so child subprocesses know their nesting level
    current_depth = get_forge_depth(env)
    env[FORGE_DEPTH_VAR] = str(current_depth + 1)

    if extra_vars:
        env.update(extra_vars)

    return env


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
