"""Backend registry for Forge backend services.

A backend is a service that proxies depend on (e.g., LiteLLM on port 4000).
The backend registry is stored at:

- ~/.forge/backends/index.json

This module implements a small, versioned JSON store with atomic writes.

Ownership: Forge Backend Manager (`forge backend` CLI).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

import dacite

from forge.core.paths import get_forge_home
from forge.core.state import (
    StateCorruptedError,
    atomic_write_json,
    file_lock_for_target,
)

logger = logging.getLogger(__name__)

BACKEND_REGISTRY_VERSION = 1
BACKENDS_DIR = "backends"
BACKEND_INDEX_FILENAME = "index.json"

CLI_LOCK_TIMEOUT_S = 5.0


from forge.core.process import is_pid_alive as is_pid_alive  # noqa: E402, F401  # re-export


class BackendRegistryCorruptedError(StateCorruptedError):
    """Raised when the backend registry cannot be parsed."""

    pass


@dataclass
class BackendInstance:
    """A backend instance (used both in registry and at runtime).

    Timestamps are stored as ISO8601 strings.
    """

    backend_id: str  # e.g., "litellm-4000"
    adapter_type: str  # e.g., "litellm"
    port: int
    pid: int | None = None
    status: Literal["healthy", "unhealthy", "stopped", "unknown"] = "unknown"
    created_at: str | None = None


@dataclass
class BackendRegistry:
    """Backend registry file format."""

    version: int = BACKEND_REGISTRY_VERSION
    backends: dict[str, BackendInstance] = field(default_factory=dict)


def get_backend_registry_path() -> Path:
    """Return the full path to the backend registry file."""

    return get_forge_home() / BACKENDS_DIR / BACKEND_INDEX_FILENAME


class BackendRegistryStore:
    """Manage the backend registry at ~/.forge/backends/index.json.

    Error handling:
    - Missing file: returns empty registry (self-healing)
    - Corrupted file: raises BackendRegistryCorruptedError
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        self._registry_path = registry_path or get_backend_registry_path()

    @property
    def registry_path(self) -> Path:
        return self._registry_path

    def exists(self) -> bool:
        return self._registry_path.is_file()

    def read(self) -> BackendRegistry:
        if not self.exists():
            return BackendRegistry()

        try:
            with open(self._registry_path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise BackendRegistryCorruptedError(str(self._registry_path), f"invalid JSON: {e}")
        except OSError as e:
            raise BackendRegistryCorruptedError(str(self._registry_path), f"read error: {e}")

        version = data.get("version")
        if version is None:
            raise BackendRegistryCorruptedError(str(self._registry_path), "missing version field")
        if version != BACKEND_REGISTRY_VERSION:
            raise BackendRegistryCorruptedError(
                str(self._registry_path),
                f"incompatible version {version} (this Forge expects {BACKEND_REGISTRY_VERSION}). "
                f"Delete this file and retry.",
            )

        try:
            return dacite.from_dict(
                data_class=BackendRegistry,
                data=data,
                config=dacite.Config(strict=True),
            )
        except (dacite.DaciteError, TypeError, KeyError) as e:
            raise BackendRegistryCorruptedError(str(self._registry_path), f"deserialization error: {e}")

    def write(self, registry: BackendRegistry) -> None:
        data = asdict(registry)
        atomic_write_json(self._registry_path, data)

    def update(self, *, timeout_s: float, mutate: Callable[[BackendRegistry], None]) -> BackendRegistry:
        """Update registry via a locked read-modify-write cycle."""

        with file_lock_for_target(target_path=self._registry_path, timeout_s=timeout_s):
            registry = self.read()
            mutate(registry)
            self.write(registry)
            return registry

    def prune_dead_pids(self, *, timeout_s: float = CLI_LOCK_TIMEOUT_S) -> list[str]:
        """Remove registry entries whose Forge-spawned pid is no longer running.

        Definition of stale (normative):
        - Only entries with pid != None are considered (Forge-spawned backends).
        - Entries with pid == None are never auto-pruned.

        Returns:
            List of backend IDs removed from the registry.
        """

        with file_lock_for_target(target_path=self._registry_path, timeout_s=timeout_s):
            registry = self.read()

            stale_ids: list[str] = []
            for backend_id, entry in list(registry.backends.items()):
                if entry.pid is None:
                    continue
                if not is_pid_alive(entry.pid):
                    del registry.backends[backend_id]
                    stale_ids.append(backend_id)

            if stale_ids:
                self.write(registry)

            return stale_ids

    def list_backends(self) -> list[BackendInstance]:
        """List all backends (prunes dead PIDs first).

        Returns:
            List of backend instances, ordered by creation time (oldest first).
        """

        self.prune_dead_pids()
        registry = self.read()

        backends = list(registry.backends.values())
        backends.sort(key=lambda x: x.created_at or "")
        return backends
