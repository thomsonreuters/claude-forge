"""Forge runtime configuration (~/.forge/config.yaml).

Separate from forge.config (which the proxy imports) to avoid leaking
runtime preferences into routing. The proxy singleton must never see
these values — they control CLI/session behavior only.

File: ~/.forge/config.yaml (optional, fail-open if missing or invalid).

Three-layer resolution (highest precedence wins):
  1. Built-in defaults (dataclass field defaults)
  2. ~/.forge/config.yaml
  3. Environment variables (via _ENV_OVERRIDES mapping)
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from forge.core.paths import get_forge_home

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.yaml"

# Env var → field name mapping. Env vars override YAML values when present.
# This is the single source of truth for env-to-config overrides.
_ENV_OVERRIDES: dict[str, str] = {
    "FORGE_DEBUG": "log_level",
}


@dataclass
class RuntimeConfig:
    """Global Forge runtime preferences — always reflects effective values.

    Three-layer resolution: built-in defaults → config.yaml → env vars.
    After loading, all fields represent the effective runtime state.
    They do NOT affect proxy routing (that's ForgeConfig's domain).

    All fields have sensible defaults — the config file is optional.
    """

    # Proxy execution mode: "host" runs proxy on host, "sidecar" bundles in Docker
    proxy_mode: str = "host"

    sidecar_image: str = "forge-sidecar:latest"

    # Version string sent in the User-Agent header to upstream LLM providers.
    user_agent_claude_code_version: str = ""

    # Optional model override for direct (non-proxy) sessions.
    # Passed to Claude Code via ANTHROPIC_MODEL + ANTHROPIC_DEFAULT_*_MODEL.
    # Empty string = let Claude Code decide.
    default_direct_model: str = ""

    # Fallback auto-compact window for proxy mode when model lookup fails.
    # Passed as CLAUDE_CODE_AUTO_COMPACT_WINDOW to Claude Code.
    # Direct sessions don't use this — Claude Code handles its own context.
    context_limit: int = 200000

    # Status line timeout for proxy/git subprocess calls (seconds)
    status_timeout: float = 2.0

    # Handoff agent default timeout (seconds)
    handoff_timeout: int = 300

    # File logging level: "off" (no file logging), "debug", "info", "warning"
    # Override: FORGE_DEBUG env var (1/true/yes → "debug", 0/false/no/off → "off")
    log_level: str = "off"

    # Show Claude.ai rate limit usage in status line (direct sessions only).
    # Off by default — not relevant for enterprise plans.
    show_rate_limits: bool = False

    # Auto-delete log files older than N days on CLI startup.
    # 0 = disabled (no auto-cleanup). Positive integer = retention window in days.
    log_retention_days: int = 0

    # Auto-delete sessions older than N days on CLI startup.
    # 0 = disabled (no auto-cleanup). Positive integer = retention window in days.
    # Keeps worktrees and branches; removes manifests, index entries, and Claude
    # transcripts (*.jsonl in ~/.claude/projects/). Forge artifact snapshots
    # under .forge/artifacts/ are NOT removed.
    session_retention_days: int = 0

    # Policy summary feedback after evaluations: "on" (default), "off".
    # Gates post-hoc "[forge] Policy: checked ..." summary lines and additionalContext.
    # Does NOT affect deny output or substantive warning lines -- those stay visible always.
    policy_summary_feedback: str = "on"

    def __post_init__(self) -> None:
        valid_modes = {"host", "sidecar"}
        if self.proxy_mode not in valid_modes:
            raise ValueError(
                f"Invalid proxy_mode: '{self.proxy_mode}' " f"(must be one of: {', '.join(sorted(valid_modes))})"
            )
        if self.context_limit < 1:
            raise ValueError(f"context_limit must be >= 1, got {self.context_limit}")
        if self.status_timeout <= 0:
            raise ValueError(f"status_timeout must be > 0, got {self.status_timeout}")
        if self.handoff_timeout < 1:
            raise ValueError(f"handoff_timeout must be >= 1, got {self.handoff_timeout}")
        valid_log_levels = {"off", "debug", "info", "warning"}
        if self.log_level not in valid_log_levels:
            raise ValueError(
                f"Invalid log_level: '{self.log_level}' (must be one of: {', '.join(sorted(valid_log_levels))})"
            )
        if self.log_retention_days < 0:
            raise ValueError(f"log_retention_days must be >= 0, got {self.log_retention_days}")
        if self.session_retention_days < 0:
            raise ValueError(f"session_retention_days must be >= 0, got {self.session_retention_days}")
        valid_feedback = {"on", "off"}
        if self.policy_summary_feedback not in valid_feedback:
            raise ValueError(
                f"Invalid policy_summary_feedback: '{self.policy_summary_feedback}' "
                f"(must be one of: {', '.join(sorted(valid_feedback))})"
            )


def _coerce_debug_to_log_level(raw: str) -> str:
    """Coerce FORGE_DEBUG env var to a log_level string."""
    low = raw.lower()
    if low in ("1", "true", "yes"):
        return "debug"
    if low in ("0", "false", "no", "off"):
        return "off"
    if low in ("debug", "info", "warning"):
        return low
    raise ValueError(f"Cannot coerce FORGE_DEBUG={raw!r} to log level")


def _coerce_env_value(raw: str, field_info: Any) -> Any:
    """Coerce a raw env var string to the field's expected Python type."""
    ftype = field_info.type
    if ftype is int or ftype == "int":
        val = int(raw)
        if val < 1:
            raise ValueError(f"must be >= 1, got {val}")
        return val
    if ftype is float or ftype == "float":
        return float(raw)
    if ftype is bool or ftype == "bool":
        if raw.lower() in ("1", "true", "yes"):
            return True
        if raw.lower() in ("0", "false", "no"):
            return False
        raise ValueError(f"Cannot coerce {raw!r} to bool")
    return raw


def _apply_env_overrides(config: RuntimeConfig) -> RuntimeConfig:
    """Apply environment variable overrides to config values.

    Per-field: each env var is applied independently. If one parse fails,
    others still apply (fail-open per field, not all-or-nothing).
    Attaches _env_sources dict for display annotation by %config.
    """
    field_map = {f.name: f for f in fields(RuntimeConfig)}
    overrides: dict[str, Any] = {}
    env_sources: dict[str, str] = {}

    for env_var, field_name in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            continue
        try:
            if field_name == "log_level":
                coerced = _coerce_debug_to_log_level(raw)
            else:
                coerced = _coerce_env_value(raw, field_map[field_name])
            overrides[field_name] = coerced
            env_sources[field_name] = env_var
        except (ValueError, TypeError) as e:
            logger.warning("Ignoring env %s=%r: %s", env_var, raw, e)

    if not overrides:
        object.__setattr__(config, "_env_sources", {})
        return config

    merged = asdict(config)
    merged.update(overrides)
    try:
        result = RuntimeConfig(**merged)
    except (ValueError, TypeError) as e:
        logger.warning("Env override produced invalid config: %s — ignoring overrides", e)
        object.__setattr__(config, "_env_sources", {})
        return config

    object.__setattr__(result, "_env_sources", env_sources)
    return result


# Singleton cache (must be after RuntimeConfig definition)
_config: RuntimeConfig | None = None


def get_config_path() -> Path:
    """Get the path to ~/.forge/config.yaml."""
    return get_forge_home() / CONFIG_FILENAME


def ensure_config() -> Path:
    """Ensure the config file exists, creating with defaults if missing.

    Returns the path to the config file. Idempotent — existing files
    are never overwritten.
    """
    config_path = get_config_path()
    if not config_path.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(get_default_config_content())
        os.chmod(str(config_path), 0o600)
    return config_path


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Load runtime config from YAML file, then apply env var overrides.

    Three-layer resolution: built-in defaults → config.yaml → env vars.
    Fail-open: returns defaults if file is missing, unreadable, or invalid YAML.
    Unknown keys are warned and ignored (forward compatibility).

    Args:
        path: Override config file path (for testing). Defaults to ~/.forge/config.yaml.
    """
    config_path = path or get_config_path()

    if not config_path.is_file():
        return _apply_env_overrides(RuntimeConfig())

    try:
        import yaml

        raw = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as e:
        logger.warning("Failed to read %s: %s — using defaults", config_path, e)
        return _apply_env_overrides(RuntimeConfig())

    if not isinstance(data, dict):
        logger.warning("%s is not a YAML mapping — using defaults", config_path)
        return _apply_env_overrides(RuntimeConfig())

    return _apply_env_overrides(_dict_to_runtime_config(data, config_path))


def _dict_to_runtime_config(data: dict[str, Any], source: Path) -> RuntimeConfig:
    """Convert a dict to RuntimeConfig, warning on unknown keys.

    System boundary: user-edited config. Strict on value validation, best-effort
    on unknown keys for forward compat (coding-standards.md §5, system boundaries).
    """
    known_fields = {f.name for f in fields(RuntimeConfig)}
    unknown = set(data.keys()) - known_fields
    if unknown:
        logger.warning(
            "Unknown keys in %s (ignored): %s",
            source,
            ", ".join(sorted(unknown)),
        )

    kwargs: dict[str, Any] = {}
    for f in fields(RuntimeConfig):
        if f.name in data:
            val = data[f.name]
            # YAML parses "off"/"on"/"yes"/"no" as booleans — coerce back
            # for string fields (e.g., log_level: off → False → "off")
            if isinstance(val, bool) and f.type in ("str", str):
                val = "on" if val else "off"
            kwargs[f.name] = val

    try:
        return RuntimeConfig(**kwargs)
    except (ValueError, TypeError) as e:
        logger.warning("Invalid config in %s: %s — using defaults", source, e)
        return RuntimeConfig()


def get_runtime_config() -> RuntimeConfig:
    """Get cached runtime config singleton (lazy-loaded on first access)."""
    global _config
    if _config is None:
        _config = load_runtime_config()
    return _config


def get_default_direct_model() -> str | None:
    """Get the configured direct-session model override, or None if unset."""
    return get_runtime_config().default_direct_model.strip() or None


def reset_runtime_config() -> None:
    """Reset the cached singleton (for testing)."""
    global _config
    _config = None


def write_runtime_config(config_data: dict[str, Any], path: Path | None = None) -> Path:
    """Write runtime config to YAML file atomically.

    Args:
        config_data: Dict of config values to write.
        path: Override path (for testing).

    Returns:
        Path to the written file.
    """
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    from ruamel.yaml import YAML

    ruamel = YAML()
    ruamel.preserve_quotes = True
    ruamel.default_flow_style = False

    # Atomic write: unique temp file + os.replace (matches proxy config pattern)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent),
        prefix=f".{config_path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            ruamel.dump(config_data, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(config_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    reset_runtime_config()

    return config_path


def get_default_config_content() -> str:
    """Generate default config.yaml content with comments."""
    return """\
# Forge Runtime Configuration
# This file is optional — Forge works with built-in defaults.
# Edit with: forge config edit
# Set values: forge config set <key>=<value>

# Proxy execution mode:
#   host    — proxy runs on host (default, no Docker required)
#   sidecar — proxy bundled with Claude in Docker container
proxy_mode: host

# Docker image for sidecar mode
# sidecar_image: forge-sidecar:latest

# Version string for User-Agent header to upstream LLM providers
# user_agent_claude_code_version: "2.1.76"

# Optional model override for direct (non-proxy) sessions.
# Forge pins this through Claude Code's ANTHROPIC_DEFAULT_*_MODEL env vars.
# Set to "" to let Claude Code pick. Aliases like "opus" or "sonnet" also work.
# default_direct_model: claude-opus-4-6

# Fallback auto-compact window for proxy mode when model lookup fails.
# Passed as CLAUDE_CODE_AUTO_COMPACT_WINDOW to Claude Code.
# Direct sessions don't use this — Claude Code handles its own context.
# context_limit: 200000

# Status line timeout for proxy/git calls (seconds)
# status_timeout: 2.0

# Handoff agent timeout (seconds)
# handoff_timeout: 300

# File logging level: off (no file logging), debug, info, warning
# Logs written to $FORGE_HOME/logs/
# Override: FORGE_DEBUG env var (1/true/yes for debug, 0/false/no/off to disable)
# log_level: "off"

# Show Claude.ai rate limit usage in status line (direct sessions only).
# Not relevant for enterprise plans. Enable with: forge config set show_rate_limits=true
# show_rate_limits: false

# Auto-delete log files older than N days on CLI startup.
# 0 = disabled (no auto-cleanup). Example: 30 = keep last 30 days.
# Manual cleanup: forge logs --clean [--older-than DAYS]
# log_retention_days: 0

# Auto-delete sessions older than N days on CLI startup.
# 0 = disabled (no auto-cleanup). Example: 90 = keep last 90 days.
# Keeps worktrees and branches; removes manifests, index entries, and
# Claude transcripts (*.jsonl in ~/.claude/projects/).
# Forge artifact snapshots (.forge/artifacts/) are NOT removed.
# Manual cleanup: forge session clean --older-than DAYS
# session_retention_days: 0

# Policy summary feedback: show post-evaluation summary lines and additionalContext.
# "on" (default) prints what was checked and the verdict after each policy evaluation.
# "off" silences summary lines. Deny messages and substantive warnings stay visible always.
# policy_summary_feedback: "on"
"""
