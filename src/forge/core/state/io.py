"""Atomic file operations for Forge state files.

All write operations use the tempfile + os.replace() pattern for atomicity.
This ensures that readers never see partial writes.

Durability policy: No fsync. We rely on the filesystem's default behavior.
This is a deliberate simplicity choice - if crash safety is needed later,
add fsync before os.replace() and optionally fsync the directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .exceptions import StateCorruptedError, StateNotFoundError


def open_secure_append(path: Path) -> Any:
    """Open a file for append with 0600 permissions (owner read/write only).

    Used for log files that may contain sensitive payloads (request bodies,
    tool inputs, error messages). Creates the file with 0600 if missing;
    chmods to 0600 if it already exists.

    The post-open chmod has a tiny TOCTOU window for pre-existing files but
    closes it on every subsequent write. New files are created with 0600
    atomically (subject to umask, which only clears bits we already want clear).
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass  # best-effort: some filesystems (e.g., CIFS) may not support fchmod
    return os.fdopen(fd, "a", encoding="utf-8")


def atomic_write_text(
    path: Path,
    content: str,
    *,
    create_parents: bool = True,
) -> None:
    """Write text to a file atomically.

    Uses tempfile + os.replace() pattern to ensure readers never see
    partial writes. The temp file is created in the same directory as
    the target to ensure atomic rename works (same filesystem).

    Args:
        path: Target file path.
        content: Text content to write.
        create_parents: Create parent directories if they don't exist.

    Raises:
        OSError: If the write or rename fails.
    """
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)

    # Create temp file in same directory for atomic rename
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    data: dict[str, Any],
    *,
    indent: int = 2,
    create_parents: bool = True,
) -> None:
    """Write JSON to a file atomically.

    Serializes the dict to JSON and writes it atomically using
    tempfile + os.replace(). Adds a trailing newline for git-friendliness.

    Args:
        path: Target file path.
        data: Dict to serialize as JSON.
        indent: JSON indentation level (default 2).
        create_parents: Create parent directories if they don't exist.

    Raises:
        OSError: If the write or rename fails.
        TypeError: If data contains non-serializable values.
    """
    content = json.dumps(data, indent=indent)
    content += "\n"  # Trailing newline
    atomic_write_text(path, content, create_parents=create_parents)


def read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file.

    Args:
        path: Path to JSON file.

    Returns:
        Parsed JSON as a dict.

    Raises:
        StateNotFoundError: If the file does not exist.
        StateCorruptedError: If the file contains invalid JSON or is not a JSON object.
    """
    if not path.exists():
        raise StateNotFoundError(str(path))

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise StateCorruptedError(str(path), f"invalid JSON: {e}") from e
    except OSError as e:
        raise StateCorruptedError(str(path), f"read error: {e}") from e

    if not isinstance(data, dict):
        raise StateCorruptedError(
            str(path),
            f"expected JSON object, got {type(data).__name__}",
        )

    return data
