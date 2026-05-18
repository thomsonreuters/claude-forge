"""Proxy identity discovery for runtime truth.

This module provides a lightweight, testable way to discover the proxy's
identity for the GET / runtime truth endpoint.

The discovery uses a 2-tier approach:
1. Registry lookup by (template, port) (primary)
2. Derived from request/env (fallback - for unregistered proxies)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from forge.proxy.proxies import ProxyRegistryCorruptedError, ProxyRegistryStore

logger = logging.getLogger(__name__)

# Default port when neither request nor env port is available.
# This is intentionally a simple constant (not derived from config) to keep
# this module lightweight and avoid circular imports. The value matches
# the base.yaml default, but if they drift, the impact is only on the
# "derived" fallback path (manual/unregistered proxies).
DEFAULT_PROXY_PORT = 8082

# Type aliases for clarity and correctness
ProxySource = Literal["registry", "derived"]
ProxyStatus = Literal["registered", "unregistered"]


@dataclass(frozen=True)
class ProxyIdentity:
    """Proxy identity for runtime truth.

    Immutable (frozen) to prevent accidental mutation.
    All fields are always populated - no None values except proxy_id
    when the proxy is unregistered.
    """

    proxy_id: str | None
    template: str
    port: int
    base_url: str
    source: ProxySource
    status: ProxyStatus


def get_proxy_identity(
    *,
    active_template: str,
    request_host: str | None = None,
    request_port: int | None = None,
    env_port: int | None = None,
    process_proxy_id: str | None = None,
) -> ProxyIdentity:
    """Discover proxy identity using 2-tier approach.

    Priority for port determination:
        request_port > env_port > DEFAULT_PROXY_PORT

    Priority for proxy identity:
        1. Process proxy id + registry lookup by (template, port) (source="registry")
        2. Registry lookup by (template, port) (source="registry")
        3. Derived values (source="derived", status="unregistered")

    The base_url is always derived from the effective host/port, not from
    the registry, to ensure accuracy with the actual request endpoint.

    Args:
        active_template: The proxy template name (e.g., "litellm-openai")
        request_host: Host from the incoming request (preferred)
        request_port: Port from the incoming request (preferred)
        env_port: Port from ACTIVE_PORT env var (fallback)
        process_proxy_id: Proxy id this process was started with (FORGE_PROXY_ID).

    Returns:
        ProxyIdentity with all fields populated. proxy_id may be None
        when the proxy is unregistered.
    """
    effective_port = request_port or env_port or DEFAULT_PROXY_PORT
    effective_host = request_host or "localhost"
    base_url = f"http://{effective_host}:{effective_port}"

    try:
        store = ProxyRegistryStore()
        registry = store.read()

        # Multiple matches shouldn't happen, but corruption could cause it;
        # sort by proxy_id for deterministic selection.
        matches = [
            entry
            for entry in registry.proxies.values()
            if entry.template == active_template and entry.port == effective_port
        ]
        if process_proxy_id:
            for entry in matches:
                if entry.proxy_id == process_proxy_id:
                    return ProxyIdentity(
                        proxy_id=entry.proxy_id,
                        template=active_template,
                        port=effective_port,
                        base_url=base_url,
                        source="registry",
                        status="registered",
                    )

        if matches:
            best_match = sorted(matches, key=lambda e: e.proxy_id)[0]
            return ProxyIdentity(
                proxy_id=best_match.proxy_id,
                template=active_template,
                port=effective_port,
                base_url=base_url,  # Use derived base_url, not registry
                source="registry",
                status="registered",
            )
    except ProxyRegistryCorruptedError as e:
        logger.warning(f"Proxy registry corrupted during identity lookup: {e}")
    except Exception as e:
        # Don't fail if registry is unavailable (e.g., missing file is handled
        # by ProxyRegistryStore.read() returning empty registry, but other
        # unexpected errors should be logged)
        logger.debug(f"Proxy registry lookup failed: {e}")

    return ProxyIdentity(
        proxy_id=process_proxy_id,
        template=active_template,
        port=effective_port,
        base_url=base_url,
        source="derived",
        status="unregistered",
    )
