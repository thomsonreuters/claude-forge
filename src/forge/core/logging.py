"""File logging for Forge.

Activated by ``log_level`` config setting or ``FORGE_DEBUG=1`` env var.
Attaches a RotatingFileHandler to the ``forge`` logger namespace so all child
loggers (forge.cli.*, forge.session.*, forge.core.*, etc.) emit to disk.

No file I/O occurs when the effective log_level is "off".
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
}


def get_effective_log_level() -> str:
    """Resolve effective log level (env var overrides are applied in RuntimeConfig).

    Returns:
        "off", "debug", "info", or "warning".
    """
    try:
        from forge.runtime_config import get_runtime_config

        return get_runtime_config().log_level
    except Exception:
        return "off"


def configure_debug_logging(component: str, subdirectory: str) -> None:
    """Attach a RotatingFileHandler to the 'forge' namespace.

    Activates when log_level config is not "off" or FORGE_DEBUG=1.
    Per-PID log files avoid multi-process rotation conflicts.

    Fail-open: permission errors or missing dirs don't crash the command.

    Args:
        component: Log filename stem (e.g., "session-start", "session").
        subdirectory: Directory under $FORGE_HOME/logs/ (e.g., "hooks", "cli").
    """
    level = get_effective_log_level()
    if level == "off":
        return

    forge_logger = logging.getLogger("forge")

    try:
        from forge.core.paths import get_forge_home

        logs_dir = get_forge_home() / "logs" / subdirectory
        logs_dir.mkdir(exist_ok=True, parents=True)

        pid = os.getpid()
        log_file = logs_dir / f"{component}.{pid}.log"

        # Idempotency: skip if THIS exact file handler is already attached.
        for h in forge_logger.handlers:
            if isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == str(log_file):
                return

        py_level = _LEVEL_MAP.get(level, logging.DEBUG)
        forge_logger.setLevel(py_level)
        forge_logger.propagate = False

        fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(process)d | "
            "%(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler = RotatingFileHandler(str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5)
        handler.setLevel(py_level)
        handler.setFormatter(fmt)
        forge_logger.addHandler(handler)

        # Set 0600 perms — log files may contain payload fragments / hostnames.
        try:
            os.chmod(str(log_file), 0o600)
        except OSError:
            pass
    except Exception:
        pass  # Fail-open: don't crash the command because logging couldn't start


def configure_console_logging() -> None:
    """Attach a stderr StreamHandler to the 'forge' namespace.

    For long-running processes (proxy server) that need visible console output
    in addition to file logging. Idempotent: skips if a stderr handler exists.

    No-op when log_level is "off".
    """
    level = get_effective_log_level()
    if level == "off":
        return

    py_level = _LEVEL_MAP.get(level, logging.DEBUG)
    forge_logger = logging.getLogger("forge")

    import sys

    has_stderr = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stderr
        for h in forge_logger.handlers
    )
    if has_stderr:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(py_level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(process)d | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    forge_logger.addHandler(handler)


def find_latest_log(subdirectory: str, glob_pattern: str) -> Path | None:
    """Find the most recently modified log file in a subdirectory.

    Args:
        subdirectory: Directory under $FORGE_HOME/logs/ (e.g., "proxy").
        glob_pattern: Glob pattern to match (e.g., "proxy.*.log").
    """
    try:
        from forge.core.paths import get_forge_home

        logs_dir = get_forge_home() / "logs" / subdirectory
        if not logs_dir.exists():
            return None
        log_files = sorted(logs_dir.glob(glob_pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return log_files[0] if log_files else None
    except Exception:
        return None
