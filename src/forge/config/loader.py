"""Configuration loader for Forge.

This module handles loading configuration from three sources:

    1. Template (for proxy creation): defaults/templates/{t}.yaml
    2. Proxy file (runtime): ~/.forge/proxies/{id}/proxy.yaml
    3. Secrets (env vars): *_API_KEY, *_AUTH_URL, FORGE_HOME

Schema defaults in dataclasses handle missing fields.
No user/project/local config file support - proxies own full config.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from importlib.abc import Traversable
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from ruamel.yaml import YAML

from forge.config.dataclass_utils import dict_to_dataclass
from forge.config.schema import (
    ForgeConfig,
    ProviderConfig,
    ProxyConfig,
    ProxyInstanceConfig,
    SessionConfig,
    TierModels,
    TierOverride,
    TierOverrides,
)
from forge.core.paths import get_forge_home

logger = logging.getLogger(__name__)


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge overlay into base dict (kustomize-style).

    - Dicts are merged recursively
    - Other values are replaced
    - None values in overlay are skipped (don't override with None)

    Args:
        base: Base dictionary
        overlay: Overlay dictionary (takes precedence)

    Returns:
        Merged dictionary (new dict, inputs not modified)
    """
    result = base.copy()

    for key, overlay_value in overlay.items():
        if overlay_value is None:
            continue

        if key in result and isinstance(result[key], dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(result[key], overlay_value)
        else:
            result[key] = overlay_value

    return result


def load_yaml(path: Path) -> dict:
    """Load YAML file, returning empty dict if not found.

    Notes:
        - Missing file: returns {}
        - Invalid YAML: returns {} (best-effort)

    For strict parsing (fail fast), use load_yaml_strict().
    """
    if not path.exists():
        logger.debug(f"Config file not found: {path}")
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse {path}: {e}")
        return {}


def load_yaml_strict(path: Path) -> dict:
    """Load YAML file strictly.

    Raises:
        ValueError: if the file exists but cannot be parsed as a dict.
    """
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse YAML at {path}: {e}")

    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ValueError(f"YAML at {path} must be a mapping (dict), got {type(data)}")

    return data


def get_defaults_dir() -> Path:
    """Get the defaults directory (relative to this module).

    .. deprecated::
        Prefer ``list_template_names()``, ``template_exists()``, and
        ``read_template()`` for template access.  This helper is retained for
        callers that need a concrete ``Path`` (e.g. display-only).
    """
    return Path(__file__).parent / "defaults"


# --- Template access helpers (C3 — importlib.resources + user templates) ---
# Shipped templates live in the package (importlib.resources). User templates
# live at ~/.forge/templates/<name>.yaml and take precedence when present.
# A user template is a full replacement, not a YAML merge.
#
# User templates that shadow a shipped template are created via
# ``forge proxy template edit``; manually placed templates without a
# shipped counterpart also work (advanced/manual path).

TEMPLATE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_template_name(name: str) -> None:
    """Validate template name to prevent path traversal.

    Raises:
        ValueError: If name contains invalid characters or patterns.
    """
    if not name:
        raise ValueError("Template name cannot be empty")
    if not TEMPLATE_NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid template name '{name}': must be 1-64 characters, "
            "start with alphanumeric, and contain only alphanumeric, underscore, dot, or hyphen"
        )
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid template name '{name}': contains path separator or parent reference")


def _templates_path() -> "Traversable":
    """Return a traversable path to the shipped templates package."""
    from importlib import resources

    return resources.files("forge.config.defaults.templates")


def _user_templates_dir() -> Path:
    """Return the user templates directory (~/.forge/templates/)."""
    return get_forge_home() / "templates"


def get_user_template_path(name: str) -> Path:
    """Return the filesystem path for a user template.

    Validates the name to prevent path traversal before constructing
    the path. All user-template filesystem operations go through this
    function, so it is the security boundary.

    Raises:
        ValueError: If name fails validation (path traversal, etc.).
    """
    validate_template_name(name)
    return _user_templates_dir() / f"{name}.yaml"


def is_user_template(name: str) -> bool:
    """Check if a user-customized template exists for this name."""
    return get_user_template_path(name).is_file()


def shipped_template_exists(name: str) -> bool:
    """Check if a shipped (built-in) template exists, ignoring user copies."""
    tpl = _templates_path()
    return tpl.joinpath(f"{name}.yaml").is_file()


def read_shipped_template(name: str) -> str:
    """Read shipped template content, ignoring any user copy.

    Used by ``forge proxy template edit`` to seed the first user copy,
    and by display logic to show the built-in baseline.

    Raises:
        FileNotFoundError: If no shipped template with this name exists.
    """
    tpl = _templates_path()
    target = tpl.joinpath(f"{name}.yaml")
    if not target.is_file():
        raise FileNotFoundError(f"Shipped template not found: {name}")
    return target.read_text(encoding="utf-8")


def list_template_names(*, include_internal: bool = False) -> list[str]:
    """Return sorted list of available template names (shipped + user).

    User templates at ~/.forge/templates/ are merged with shipped templates.
    Deduplication ensures each name appears once. User templates bypass the
    ``internal`` filter (only shipped templates can be marked internal).

    Args:
        include_internal: If False (default), excludes shipped templates with
            ``internal: true`` at the top level (e.g. test-only templates).
    """
    import yaml

    names: set[str] = set()

    # Shipped templates (importlib.resources)
    tpl = _templates_path()
    for p in tpl.iterdir():
        if not (hasattr(p, "name") and p.name.endswith(".yaml")):
            continue
        if not include_internal:
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("internal") is True:
                    continue
            except Exception:
                pass  # If we can't parse it, include it
        names.add(p.name.removesuffix(".yaml"))

    # User templates (~/.forge/templates/)
    # Skip files with invalid names (e.g. .hidden.yaml) to avoid
    # downstream ValueError from validate_template_name().
    user_dir = _user_templates_dir()
    if user_dir.is_dir():
        for p in user_dir.iterdir():
            if p.suffix == ".yaml" and p.is_file():
                stem = p.stem
                if TEMPLATE_NAME_PATTERN.match(stem) and "/" not in stem and ".." not in stem:
                    names.add(stem)

    return sorted(names)


def template_exists(template: str) -> bool:
    """Check if a template exists (user copy or shipped)."""
    if is_user_template(template):
        return True
    tpl = _templates_path()
    return tpl.joinpath(f"{template}.yaml").is_file()


def read_template(template: str) -> str:
    """Read template content, preferring user copy over shipped.

    Resolution order: user copy at ~/.forge/templates/ first,
    then shipped template in the package.

    Raises:
        FileNotFoundError: If template does not exist in either location.
    """
    user_path = get_user_template_path(template)
    if user_path.is_file():
        return user_path.read_text(encoding="utf-8")
    tpl = _templates_path()
    target = tpl.joinpath(f"{template}.yaml")
    if not target.is_file():
        raise FileNotFoundError(f"Template not found: {template}")
    return target.read_text(encoding="utf-8")


def env_to_dict() -> dict:
    """Map secret environment variables to config dict.

    Only maps secrets (API keys, auth URLs). Configuration belongs in
    templates/proxies, not environment variables.
    """
    result: dict = {"proxy": {"gemini": {}, "openai": {}, "litellm": {}}, "session": {}}

    secret_mappings = {
        # Auth URLs (remote endpoints)
        "GEMINI_AUTH_URL": ("proxy", "gemini", "auth_url"),
        "OPENAI_AUTH_URL": ("proxy", "openai", "auth_url"),
        # Forge home (user-specific path override)
        "FORGE_HOME": ("session", "forge_home"),
    }

    for env_key, config_path in secret_mappings.items():
        value = os.environ.get(env_key)
        if value is not None:
            # Secrets are opaque strings — no type coercion (H6: "007" must not become 7)
            _set_nested(result, config_path, value)

    return result


def _set_nested(d: dict, path: tuple, value: Any) -> None:
    """Set a value in a nested dict using a path tuple."""
    for key in path[:-1]:
        if key not in d:
            d[key] = {}
        d = d[key]
    d[path[-1]] = value


# --- PROXY FILE I/O (Full Ownership Model) ---

# Proxy ID validation: alphanumeric with underscores, dots, hyphens; 1-64 chars
# Must start with alphanumeric. Prevents path traversal attacks.
PROXY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_proxy_id(proxy_id: str) -> None:
    """Validate proxy_id to prevent path traversal attacks.

    Args:
        proxy_id: The proxy identifier to validate

    Raises:
        ValueError: If proxy_id contains invalid characters or patterns
    """
    if not proxy_id:
        raise ValueError("Proxy ID cannot be empty")
    if not PROXY_ID_PATTERN.match(proxy_id):
        raise ValueError(
            f"Invalid proxy ID '{proxy_id}': must be 1-64 characters, "
            "start with alphanumeric, and contain only alphanumeric, underscore, dot, or hyphen"
        )
    # Extra safety: reject any path separators or parent references
    if "/" in proxy_id or "\\" in proxy_id or ".." in proxy_id:
        raise ValueError(f"Invalid proxy ID '{proxy_id}': contains path separator or parent reference")


def get_proxy_file_path(proxy_id: str) -> Path:
    """Return the proxy file path for a proxy id (new format).

    New format uses proxy.yaml instead of config.yaml overlay.
    The user owns the entire file (no template merge at runtime).

    Args:
        proxy_id: The proxy identifier (validated for safety)

    Raises:
        ValueError: If proxy_id is invalid (path traversal prevention)
    """
    validate_proxy_id(proxy_id)
    return get_forge_home() / "proxies" / proxy_id / "proxy.yaml"


def get_template_path(template: str) -> Path:
    """Return the active template file path (user copy if it exists, else shipped).

    Display-only — internal resolution should use read_template() or
    read_shipped_template() instead.
    """
    if is_user_template(template):
        return get_user_template_path(template)
    return get_defaults_dir() / "templates" / f"{template}.yaml"


def compute_template_digest(template: str) -> str:
    """Compute SHA256 digest of template file content.

    Args:
        template: Template name (e.g., "litellm-openai").

    Returns a truncated digest in format "sha256:abc123..." (12 hex chars).
    This enables drift detection for future `forge proxy rebase` functionality.
    """
    content = read_template(template).encode("utf-8")
    full_hash = hashlib.sha256(content).hexdigest()
    return f"sha256:{full_hash[:12]}"


def load_proxy_instance_config(proxy_id: str) -> "ProxyInstanceConfig | None":
    """Load and parse proxy.yaml for a proxy id.

    Returns None if the file doesn't exist.
    Raises ValueError for invalid YAML or schema violations.

    Args:
        proxy_id: The proxy identifier

    Returns:
        ProxyInstanceConfig instance or None if not found
    """
    path = get_proxy_file_path(proxy_id)
    if not path.exists():
        logger.debug(f"Proxy file not found: {path}")
        return None

    try:
        ruamel = YAML()
        ruamel.preserve_quotes = True
        with open(path, encoding="utf-8") as f:
            data = ruamel.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse proxy file {path}: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Proxy file {path} must be a mapping, got {type(data)}")

    return load_proxy_instance_config_from_dict(data)


def load_proxy_instance_config_from_dict(data: dict) -> "ProxyInstanceConfig":
    """Parse a dict into a validated ProxyInstanceConfig.

    Used by both file loading and edit/set validation (CR-006).
    Raises ValueError/TypeError on invalid data (__post_init__ validates).
    """
    tiers_data = data.get("tiers", {})
    tiers = TierModels(
        haiku=tiers_data.get("haiku", ""),
        sonnet=tiers_data.get("sonnet", ""),
        opus=tiers_data.get("opus", ""),
    )

    tier_overrides_data = data.get("tier_overrides", {})
    tier_overrides = TierOverrides(
        haiku=TierOverride(**tier_overrides_data["haiku"]) if tier_overrides_data.get("haiku") else None,
        sonnet=TierOverride(**tier_overrides_data["sonnet"]) if tier_overrides_data.get("sonnet") else None,
        opus=TierOverride(**tier_overrides_data["opus"]) if tier_overrides_data.get("opus") else None,
    )

    return ProxyInstanceConfig(
        proxy_format=data.get("proxy_format", 1),
        template=data.get("template", ""),
        template_digest=data.get("template_digest", ""),
        provider=data.get("provider", ""),
        proxy_endpoint=data.get("proxy_endpoint", ""),
        port=data.get("port", 0),
        upstream_base_url=data.get("upstream_base_url", ""),
        tiers=tiers,
        tier_overrides=tier_overrides,
        model_alternatives=data.get("model_alternatives", {}),
        default_tier=data.get("default_tier", "sonnet"),
        provider_settings=data.get("provider_settings", {}),
        prompt_caching=data.get("prompt_caching", "passthrough"),
        auto_cache_min_tokens=data.get("auto_cache_min_tokens", 1024),
        costs=data.get("costs", {}),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def write_proxy_instance_config(proxy_id: str, config: "ProxyInstanceConfig") -> Path:
    """Write proxy config to proxy.yaml with atomic write.

    Uses temp file + rename for atomicity (POSIX).
    Sets 0600 permissions for security (configs may contain sensitive data).

    Args:
        proxy_id: The proxy identifier
        config: ProxyInstanceConfig to write

    Returns:
        Path to the written file
    """
    from dataclasses import asdict

    path = get_proxy_file_path(proxy_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(config)

    # Clean up None values from tier_overrides for cleaner YAML
    if data.get("tier_overrides"):
        for tier in ("haiku", "sonnet", "opus"):
            if data["tier_overrides"].get(tier) is None:
                del data["tier_overrides"][tier]
            elif data["tier_overrides"].get(tier):
                data["tier_overrides"][tier] = {k: v for k, v in data["tier_overrides"][tier].items() if v is not None}
                if not data["tier_overrides"][tier]:
                    del data["tier_overrides"][tier]

    # Use ruamel.yaml for round-trip (preserves comments on re-read)
    ruamel = YAML()
    ruamel.default_flow_style = False
    ruamel.preserve_quotes = True

    # Write to unique temp file in same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # Add header comment
            f.write("# Forge Proxy Configuration\n")
            f.write("# This file is owned by the user - edit freely\n")
            f.write("# Use `forge proxy edit` or edit directly\n\n")
            ruamel.dump(data, f)

        # Set permissions before rename (more secure)
        os.chmod(tmp_path, 0o600)

        # Atomic replace (works across filesystems, unlike Path.rename)
        os.replace(tmp_path, str(path))

    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.debug(f"Wrote proxy config to {path}")
    return path


def _proxy_instance_to_forge_config(
    proxy_config: "ProxyInstanceConfig",
) -> "ForgeConfig":
    """Convert a ProxyInstanceConfig to a ForgeConfig.

    This is used when loading from the new proxy.yaml format.
    The ProxyInstanceConfig contains everything needed to configure the proxy.
    Secrets (auth_url) are applied from environment variables.
    """
    secrets = env_to_dict()

    provider = proxy_config.provider
    auth_url: str | None = None
    if provider == "gemini":
        auth_url = secrets.get("proxy", {}).get("gemini", {}).get("auth_url")
    elif provider == "openai":
        auth_url = secrets.get("proxy", {}).get("openai", {}).get("auth_url")
    # Note: litellm uses underlying provider auth, not a separate auth_url

    provider_config = ProviderConfig(
        tiers=proxy_config.tiers,
        tier_overrides=proxy_config.tier_overrides,
        model_alternatives=proxy_config.model_alternatives,
        base_url=proxy_config.upstream_base_url,
        auth_url=auth_url or "",  # Empty string if no secret set
        openai_api_mode=proxy_config.provider_settings.get("openai_api_mode", "auto"),
        prompt_caching=proxy_config.prompt_caching,
        auto_cache_min_tokens=proxy_config.auto_cache_min_tokens,
        error_hints=proxy_config.provider_settings.get("error_hints", False),
    )

    proxy_server_config = ProxyConfig(
        preferred_provider=proxy_config.provider,
        active_template=proxy_config.template,
        default_tier=proxy_config.default_tier,
        default_port=proxy_config.port,
        costs=proxy_config.costs,
    )

    if proxy_config.provider == "gemini":
        proxy_server_config.gemini = provider_config
    elif proxy_config.provider == "openai":
        proxy_server_config.openai = provider_config
    elif proxy_config.provider == "openrouter":
        proxy_server_config.openrouter = provider_config
    else:  # litellm is default
        proxy_server_config.litellm = provider_config

    session_config = SessionConfig()
    if secrets.get("session", {}).get("forge_home"):
        session_config.forge_home = secrets["session"]["forge_home"]

    return ForgeConfig(
        proxy=proxy_server_config,
        session=session_config,
    )


def _load_template_config(template: str) -> "ForgeConfig":
    """Load template config (internal use for proxy creation).

    This loads a template YAML and applies secrets from environment.
    Used by `forge proxy create` to initialize new proxies.

    Args:
        template: Template name (e.g., "litellm-gemini")

    Returns:
        ForgeConfig populated from template + secrets

    Raises:
        ValueError: If template not found
    """
    if not template_exists(template):
        raise ValueError(f"Template not found: {template}")

    content = read_template(template)
    template_data = yaml.safe_load(content)
    if not isinstance(template_data, dict):
        raise ValueError(f"Template '{template}' must be a mapping (dict)")
    template_data.pop("internal", None)
    secrets = env_to_dict()
    config_dict = deep_merge(template_data, secrets)

    # Set active_template so proxy knows which template is in use
    config_dict.setdefault("proxy", {})["active_template"] = template

    return dict_to_dataclass(ForgeConfig, config_dict, strict=True)


def load_config(
    *,
    template: str | None = None,
    proxy_id: str | None = None,
) -> "ForgeConfig":
    """Load configuration from proxy file or template.

    Three-source model:
        1. Proxy file: ~/.forge/proxies/{id}/proxy.yaml (user owns full config)
        2. Template: defaults/templates/{t}.yaml (for proxy creation)
        3. Secrets: env vars (*_AUTH_URL, FORGE_HOME)

    Args:
        proxy_id: Load from ~/.forge/proxies/{id}/proxy.yaml
        template: Load template for proxy creation (internal use)

    Returns:
        ForgeConfig instance

    Raises:
        ValueError: If proxy_id provided but proxy.yaml not found (fail fast)
    """
    # Do not override already-set environment variables — tests set FORGE_HOME / endpoints explicitly.
    load_dotenv(override=False)

    if proxy_id:
        proxy_instance_config = load_proxy_instance_config(proxy_id)
        if proxy_instance_config is None:
            raise ValueError(f"Proxy not found: {proxy_id}")
        logger.debug(f"Loaded config from proxy.yaml for proxy_id={proxy_id}")
        return _proxy_instance_to_forge_config(proxy_instance_config)

    if template:
        return _load_template_config(template)

    return ForgeConfig()


def reload_config(
    config: "ForgeConfig",
    *,
    template: str | None = None,
    proxy_id: str | None = None,
) -> "ForgeConfig":
    """Reload configuration (for runtime updates).

    Re-reads the proxy file or template to pick up changes.
    """
    load_dotenv(override=False)

    return load_config(
        template=template or config.proxy.active_template,
        proxy_id=proxy_id,
    )
