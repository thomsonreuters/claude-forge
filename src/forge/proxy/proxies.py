"""Proxy registry for Forge proxy endpoints.

A proxy is a first-class identity for a running proxy endpoint (base_url/port) bound to a template.
The proxy registry is stored at:

- ~/.forge/proxies/index.json

This module implements a small, versioned JSON store with atomic writes.

Ownership: Forge Proxy Orchestrator (`forge proxy` CLI).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import dacite

from forge.core.paths import get_forge_home
from forge.core.state import (
    StateCorruptedError,
    atomic_write_json,
    file_lock_for_target,
)

_log = logging.getLogger(__name__)

# A "starting" entry with no PID older than this is considered orphaned.
STARTING_STALENESS_THRESHOLD_S = 60

PROXY_REGISTRY_VERSION = 1
PROXIES_DIR = "proxies"
PROXY_INDEX_FILENAME = "index.json"

CLI_LOCK_TIMEOUT_S = 5.0


from forge.core.process import is_pid_alive as is_pid_alive  # noqa: E402, F401  # re-export


def _is_orphaned_starting(entry: ProxyEntry) -> bool:
    """Return True if a 'starting' entry with no PID is stale.

    A proxy in "starting" state should transition to "healthy" within seconds.
    If it's been in "starting" for longer than STARTING_STALENESS_THRESHOLD_S,
    it was orphaned by an interrupted start_proxy() call (e.g., Ctrl+C).
    """
    if entry.created_at is None:
        # No timestamp — can't determine age, treat as stale (defensive).
        return True
    try:
        created = datetime.fromisoformat(entry.created_at)
        # Ensure timezone-aware comparison
        now = datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_s = (now - created).total_seconds()
        return age_s > STARTING_STALENESS_THRESHOLD_S
    except (ValueError, TypeError):
        # Unparseable timestamp — treat as stale.
        return True


class ProxyRegistryCorruptedError(StateCorruptedError):
    """Raised when the proxy registry cannot be parsed."""

    pass


@dataclass
class ProxyEntry:
    """A single proxy entry.

    Timestamps are stored as ISO8601 strings.
    """

    proxy_id: str
    template: str
    base_url: str
    port: int
    pid: int | None = None
    created_at: str | None = None
    last_seen_at: str | None = None
    status: str | None = None


@dataclass
class ProxyRegistry:
    """Proxy registry file format."""

    version: int = PROXY_REGISTRY_VERSION
    proxies: dict[str, ProxyEntry] = field(default_factory=dict)


def get_proxy_registry_path() -> Path:
    """Return the full path to the proxy registry file."""

    return get_forge_home() / PROXIES_DIR / PROXY_INDEX_FILENAME


def lookup_proxy_by_base_url(registry: ProxyRegistry, base_url: str) -> ProxyEntry | None:
    """Reverse lookup: find proxy entry by base_url.

    Args:
        registry: The proxy registry to search.
        base_url: The URL to match (e.g., "http://localhost:8084").

    Returns:
        The matching ProxyEntry, or None if no proxy owns that base_url.
    """
    for entry in registry.proxies.values():
        if entry.base_url == base_url:
            return entry
    return None


# Proxy statuses that represent a routable (active) proxy.
ROUTABLE_STATUSES = frozenset({"healthy", "starting"})


class ProxyResolutionError(Exception):
    """Base for proxy resolution failures."""


class ProxyNotFoundError(ProxyResolutionError):
    """No proxy matches the given name (neither proxy_id nor template)."""

    def __init__(self, name: str, *, inactive_ids: list[str] | None = None) -> None:
        self.name = name
        self.inactive_ids = inactive_ids or []
        if self.inactive_ids:
            ids = ", ".join(self.inactive_ids)
            super().__init__(
                f"found {len(self.inactive_ids)} proxy(s) with template '{name}' " f"but none are active: {ids}"
            )
        else:
            super().__init__(f"no proxy found matching '{name}' (checked proxy_id and template)")


class AmbiguousProxyError(ProxyResolutionError):
    """Multiple active proxies match the given template name."""

    def __init__(self, name: str, proxy_ids: list[str]) -> None:
        self.name = name
        self.proxy_ids = proxy_ids
        ids = ", ".join(proxy_ids)
        super().__init__(
            f"ambiguous: template '{name}' matches {len(proxy_ids)} active proxies: {ids}. "
            f"Use a specific proxy_id instead"
        )


def resolve_proxy(registry: ProxyRegistry, name: str) -> ProxyEntry:
    """Resolve a proxy by ID or template name.

    Resolution order:
    1. Exact proxy_id match (any status -- user asked for it by name).
    2. Template fallback among active (routable) entries only.
       Succeeds if exactly one active proxy uses that template.

    Raises:
        ProxyNotFoundError: No match found.
        AmbiguousProxyError: Multiple active proxies share the template.
    """
    # 1. Exact proxy_id match
    if name in registry.proxies:
        return registry.proxies[name]

    # 2. Template fallback (active entries only)
    all_matches = [e for e in registry.proxies.values() if e.template == name]
    active = [e for e in all_matches if e.status in ROUTABLE_STATUSES]

    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise AmbiguousProxyError(name, [e.proxy_id for e in active])
    if all_matches:
        raise ProxyNotFoundError(name, inactive_ids=[e.proxy_id for e in all_matches])
    raise ProxyNotFoundError(name)


def resolve_proxy_optional(registry: ProxyRegistry, name: str) -> ProxyEntry | None:
    """Fail-open variant of resolve_proxy.

    Returns None on not-found. Logs a warning on ambiguous match
    (silent fallback to direct could bypass enterprise proxy policies).
    Intended for headless consumers (supervisor, handoff, workflows)
    where a missing proxy should degrade gracefully.
    """
    try:
        return resolve_proxy(registry, name)
    except AmbiguousProxyError as e:
        _log.warning("Proxy resolution ambiguous, falling back to direct: %s", e)
        return None
    except ProxyResolutionError:
        return None


class ProxyRegistryStore:
    """Manage the proxy registry at ~/.forge/proxies/index.json.

    Error handling:
    - Missing file: returns empty registry (self-healing)
    - Corrupted file: raises ProxyRegistryCorruptedError
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        self._registry_path = registry_path or get_proxy_registry_path()

    @property
    def registry_path(self) -> Path:
        return self._registry_path

    def exists(self) -> bool:
        return self._registry_path.is_file()

    def read(self) -> ProxyRegistry:
        if not self.exists():
            return ProxyRegistry()

        try:
            with open(self._registry_path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ProxyRegistryCorruptedError(str(self._registry_path), f"invalid JSON: {e}")
        except OSError as e:
            raise ProxyRegistryCorruptedError(str(self._registry_path), f"read error: {e}")

        version = data.get("version")
        if version is None:
            raise ProxyRegistryCorruptedError(str(self._registry_path), "missing version field")
        if version != PROXY_REGISTRY_VERSION:
            raise ProxyRegistryCorruptedError(
                str(self._registry_path),
                f"incompatible version {version} (this Forge expects {PROXY_REGISTRY_VERSION}). "
                f"Delete this file and retry.",
            )

        try:
            return dacite.from_dict(
                data_class=ProxyRegistry,
                data=data,
                config=dacite.Config(strict=True),
            )
        except (dacite.DaciteError, TypeError, KeyError) as e:
            raise ProxyRegistryCorruptedError(str(self._registry_path), f"deserialization error: {e}")

    def write(self, registry: ProxyRegistry) -> None:
        data = asdict(registry)
        atomic_write_json(self._registry_path, data)

    def update(self, *, timeout_s: float, mutate: Callable[[ProxyRegistry], None]) -> ProxyRegistry:
        """Update registry via a locked read-modify-write cycle."""

        with file_lock_for_target(target_path=self._registry_path, timeout_s=timeout_s):
            registry = self.read()
            mutate(registry)
            self.write(registry)
            return registry

    def prune_dead_pids(self, *, timeout_s: float = CLI_LOCK_TIMEOUT_S) -> list[str]:
        """Remove stale proxy entries from the registry.

        Definition of stale (normative):
        - Entries with a pid that is no longer running (dead process).
        - Entries with status "starting", pid == None, and created_at older than
          STARTING_STALENESS_THRESHOLD_S (orphaned from interrupted start_proxy).

        Entries with pid == None and status other than "starting" (e.g.,
        "configured", "stopped") are intentional user states and never pruned.

        Returns:
            List of proxy IDs removed from the registry.
        """

        with file_lock_for_target(target_path=self._registry_path, timeout_s=timeout_s):
            registry = self.read()

            stale_ids: list[str] = []
            for proxy_id, entry in list(registry.proxies.items()):
                if entry.pid is not None:
                    if not is_pid_alive(entry.pid):
                        del registry.proxies[proxy_id]
                        stale_ids.append(proxy_id)
                elif entry.status == "starting" and _is_orphaned_starting(entry):
                    del registry.proxies[proxy_id]
                    stale_ids.append(proxy_id)

            if stale_ids:
                self.write(registry)

            return stale_ids

    def list_proxies(self) -> list[ProxyEntry]:
        """List proxy entries in deterministic order.

        Entries with last_seen_at sort before entries without, then by proxy_id ASC
        within each group. Does not sort by timestamp value (format-agnostic).
        """

        registry = self.read()
        proxies = list(registry.proxies.values())

        def _sort_key(entry: ProxyEntry) -> tuple[int, str]:
            # Prefer entries with last_seen_at; sort them newest-first.
            if entry.last_seen_at is None:
                return (0, entry.proxy_id)
            return (1, entry.proxy_id)

        # We can't parse timestamps here without committing to a format; keep stable ordering.
        # The CLI will display timestamps; orchestration later can implement richer sorting.
        proxies.sort(key=_sort_key, reverse=True)
        return proxies

    def find_by_base_url(self, base_url: str) -> ProxyEntry | None:
        """Find a proxy entry by its base_url.

        This is a convenience method that combines read() + lookup_proxy_by_base_url().
        Useful for status line and other consumers that need reverse lookup from URL.

        Args:
            base_url: The URL to match (e.g., "http://localhost:8084").

        Returns:
            The matching ProxyEntry, or None if no proxy owns that base_url.
        """
        registry = self.read()
        return lookup_proxy_by_base_url(registry, base_url)
