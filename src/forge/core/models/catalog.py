"""Model catalog loader and validator.

This module loads the repo-owned model_catalog.yaml and provides
strict validation and lookup functions. The catalog is cached at
module level for efficiency.
"""

import logging
from importlib import resources
from typing import Any

import yaml

from forge.core.models.types import (
    REQUIRED_TIERS,
    ModelCatalog,
    ModelSpec,
    TemperatureSpec,
)

logger = logging.getLogger(__name__)

# Supported schema versions (reject unknown)
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# Module-level singleton (lazy-loaded)
_catalog: ModelCatalog | None = None


class ModelCatalogError(ValueError):
    """Raised when the model catalog is invalid or a lookup fails."""

    pass


def _load_catalog_yaml() -> dict[str, Any]:
    """Load the raw YAML from package resources.

    Works in both editable installs and built wheels.
    """
    try:
        # Python 3.9+ style
        catalog_ref = resources.files("forge.core.data").joinpath("model_catalog.yaml")
        yaml_content = catalog_ref.read_text(encoding="utf-8")
    except (TypeError, AttributeError):
        # Fallback for older Python or edge cases
        with resources.open_text("forge.core.data", "model_catalog.yaml") as f:
            yaml_content = f.read()

    return yaml.safe_load(yaml_content)


def _parse_temperature(model_id: str, temp_data: Any, constraint: str) -> TemperatureSpec:
    """Parse temperature field which can be a single value or dict.

    Args:
        model_id: The model ID (for error messages).
        temp_data: Either a float/int or a dict with min/default/max.
        constraint: The temperature_constraint value ("fixed" or "range").

    Returns:
        A TemperatureSpec instance.

    Raises:
        ModelCatalogError: If the temperature spec is invalid.
    """
    if isinstance(temp_data, (int, float)):
        temp_val = float(temp_data)
        return TemperatureSpec(min=temp_val, default=temp_val, max=temp_val)

    if isinstance(temp_data, dict):
        if not all(k in temp_data for k in ("min", "default", "max")):
            raise ModelCatalogError(
                f"Model {model_id!r} temperature dict must have min/default/max keys, got {temp_data}"
            )
        try:
            return TemperatureSpec(
                min=float(temp_data["min"]),
                default=float(temp_data["default"]),
                max=float(temp_data["max"]),
            )
        except (TypeError, ValueError) as e:
            raise ModelCatalogError(f"Model {model_id!r} temperature spec error: {e}") from e

    raise ModelCatalogError(f"Model {model_id!r} has invalid temperature: {temp_data!r} (expected number or dict)")


def _parse_tuple_or_none(model_id: str, field_name: str, data: Any) -> tuple[str, ...] | None:
    """Parse a field that should be a list of strings or null.

    Args:
        model_id: The model ID (for error messages).
        field_name: The field name (for error messages).
        data: The raw data from YAML.

    Returns:
        A tuple of strings, or None if data is None/null.

    Raises:
        ModelCatalogError: If the data is invalid.
    """
    if data is None:
        return None
    if not isinstance(data, list):
        raise ModelCatalogError(f"Model {model_id!r} {field_name} must be a list or null, got {type(data).__name__}")
    return tuple(str(item) for item in data)


def _parse_model_spec(model_id: str, data: dict[str, Any]) -> ModelSpec:
    """Parse and validate a single model spec from YAML data.

    Args:
        model_id: The canonical model ID (for error messages).
        data: The raw YAML dict for this model.

    Returns:
        A validated ModelSpec instance.

    Raises:
        ModelCatalogError: If required fields are missing or invalid.
    """
    required_fields = {
        "friendly_name",
        "context_window_tokens",
        "max_output_tokens",
        "supports_thinking",
        "supports_images",
        "temperature_constraint",
        "temperature",
        "intelligence_score",
    }

    missing = required_fields - set(data.keys())
    if missing:
        raise ModelCatalogError(f"Model {model_id!r} missing required fields: {sorted(missing)}")

    constraint = data["temperature_constraint"]
    valid_constraints = {"fixed", "range"}
    if constraint not in valid_constraints:
        raise ModelCatalogError(
            f"Model {model_id!r} has invalid temperature_constraint: {constraint!r} "
            f"(must be one of {sorted(valid_constraints)})"
        )

    temperature = _parse_temperature(model_id, data["temperature"], constraint)
    litellm_reasoning_efforts = _parse_tuple_or_none(
        model_id, "litellm_reasoning_efforts", data.get("litellm_reasoning_efforts")
    )
    verbosity_levels = _parse_tuple_or_none(model_id, "verbosity_levels", data.get("verbosity_levels"))
    thinking_levels = _parse_tuple_or_none(model_id, "thinking_levels", data.get("thinking_levels"))
    thinking_modes = _parse_tuple_or_none(model_id, "thinking_modes", data.get("thinking_modes"))

    tags_raw = data.get("tags", [])
    if not isinstance(tags_raw, list):
        raise ModelCatalogError(f"Model {model_id!r} tags must be a list, got {type(tags_raw).__name__}")
    tags = tuple(str(t) for t in tags_raw)

    short_name = data.get("short_name")
    if short_name is not None:
        short_name = str(short_name)

    try:
        return ModelSpec(
            friendly_name=str(data["friendly_name"]),
            short_name=short_name,
            intelligence_score=int(data["intelligence_score"]),
            context_window_tokens=int(data["context_window_tokens"]),
            max_output_tokens=int(data["max_output_tokens"]),
            max_thinking_tokens=int(data["max_thinking_tokens"]) if data.get("max_thinking_tokens") else None,
            supports_thinking=bool(data["supports_thinking"]),
            supports_images=bool(data["supports_images"]),
            supports_verbosity=bool(data.get("supports_verbosity", False)),
            supports_top_p=bool(data.get("supports_top_p", True)),
            supports_sampling_overrides=bool(data.get("supports_sampling_overrides", True)),
            supports_1m_context=bool(data.get("supports_1m_context", False)),
            temperature_constraint=constraint,
            temperature=temperature,
            verbosity_levels=verbosity_levels,
            use_responses_api=bool(data.get("use_responses_api", False)),
            native_thinking_param=data.get("native_thinking_param"),
            litellm_reasoning_efforts=litellm_reasoning_efforts,
            default_reasoning_effort=data.get("default_reasoning_effort"),
            thinking_modes=thinking_modes,
            thinking_levels=thinking_levels,
            default_thinking_level=data.get("default_thinking_level"),
            token_estimate_multiplier=float(data.get("token_estimate_multiplier", 1.0)),
            tags=tags,
        )
    except (TypeError, ValueError) as e:
        raise ModelCatalogError(f"Model {model_id!r} validation error: {e}") from e


def _validate_and_build_catalog(raw: dict[str, Any]) -> ModelCatalog:
    """Validate raw YAML data and build a ModelCatalog.

    Args:
        raw: The parsed YAML dict.

    Returns:
        A validated ModelCatalog instance.

    Raises:
        ModelCatalogError: If the catalog is invalid.
    """
    schema_version = raw.get("schema_version")
    if schema_version is None:
        raise ModelCatalogError("Model catalog missing required 'schema_version' field")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ModelCatalogError(
            f"Unsupported model catalog schema_version: {schema_version} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    models_raw = raw.get("models", {})
    if not isinstance(models_raw, dict):
        raise ModelCatalogError(f"'models' must be a dict, got {type(models_raw).__name__}")

    models: dict[str, ModelSpec] = {}
    for model_id, model_data in models_raw.items():
        if not isinstance(model_data, dict):
            raise ModelCatalogError(f"Model {model_id!r} must be a dict, got {type(model_data).__name__}")
        models[model_id] = _parse_model_spec(model_id, model_data)

    aliases_raw = raw.get("aliases", {})
    if not isinstance(aliases_raw, dict):
        raise ModelCatalogError(f"'aliases' must be a dict, got {type(aliases_raw).__name__}")

    aliases: dict[str, str] = {}
    for alias, target in aliases_raw.items():
        if not isinstance(target, str):
            raise ModelCatalogError(f"Alias {alias!r} target must be a string, got {type(target).__name__}")
        # Validate alias target exists in models (this also prevents chaining
        # since aliases cannot be in the models dict)
        if target not in models:
            raise ModelCatalogError(f"Alias {alias!r} points to unknown model {target!r}")
        aliases[alias] = target

    # Parse defaults (optional; empty dict if missing for backward compat with tests)
    defaults_raw = raw.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ModelCatalogError(f"'defaults' must be a dict, got {type(defaults_raw).__name__}")

    defaults: dict[str, dict[str, str]] = {}
    for provider, tiers in defaults_raw.items():
        if not isinstance(tiers, dict):
            raise ModelCatalogError(f"defaults.{provider} must be a dict, got {type(tiers).__name__}")
        missing_tiers = REQUIRED_TIERS - set(tiers.keys())
        if missing_tiers:
            raise ModelCatalogError(f"defaults.{provider} missing required tiers: {sorted(missing_tiers)}")
        provider_defaults: dict[str, str] = {}
        for tier, model_id in tiers.items():
            if not isinstance(model_id, str):
                raise ModelCatalogError(f"defaults.{provider}.{tier} must be a string, got {type(model_id).__name__}")
            if model_id not in models:
                raise ModelCatalogError(f"defaults.{provider}.{tier} references unknown model {model_id!r}")
            provider_defaults[tier] = model_id
        defaults[provider] = provider_defaults

    logger.info(f"Loaded model catalog v{schema_version}: {len(models)} models, {len(aliases)} aliases")

    return ModelCatalog(
        schema_version=schema_version,
        models=models,
        aliases=aliases,
        defaults=defaults,
    )


def load_model_catalog(*, force_reload: bool = False) -> ModelCatalog:
    """Load and cache the model catalog.

    The catalog is loaded once and cached at module level. Subsequent
    calls return the cached instance unless force_reload is True.

    Args:
        force_reload: If True, reload from YAML even if cached.

    Returns:
        The validated ModelCatalog.

    Raises:
        ModelCatalogError: If the catalog is invalid.
    """
    global _catalog

    if _catalog is not None and not force_reload:
        return _catalog

    raw = _load_catalog_yaml()
    _catalog = _validate_and_build_catalog(raw)
    return _catalog


def resolve_model_id(model_or_alias: str) -> str:
    """Resolve a model ID or alias to its canonical ID.

    Args:
        model_or_alias: A canonical model ID or an alias.

    Returns:
        The canonical model ID.

    Raises:
        ModelCatalogError: If the model/alias is not found.
    """
    catalog = load_model_catalog()
    try:
        return catalog.resolve(model_or_alias)
    except KeyError as e:
        raise ModelCatalogError(str(e)) from e


def get_model_spec(model_or_alias: str) -> ModelSpec:
    """Get the model spec for a model ID or alias.

    Args:
        model_or_alias: A canonical model ID or an alias.

    Returns:
        The ModelSpec for the resolved model.

    Raises:
        ModelCatalogError: If the model/alias is not found.
    """
    catalog = load_model_catalog()
    try:
        return catalog.get(model_or_alias)
    except KeyError as e:
        raise ModelCatalogError(str(e)) from e


def get_context_window_tokens(model_or_alias: str) -> int:
    """Get the context window size for a model.

    Args:
        model_or_alias: A canonical model ID or an alias.

    Returns:
        The context window size in tokens.

    Raises:
        ModelCatalogError: If the model/alias is not found.
    """
    return get_model_spec(model_or_alias).context_window_tokens


def get_max_output_tokens(model_or_alias: str) -> int:
    """Get the maximum output tokens for a model.

    Args:
        model_or_alias: A canonical model ID or an alias.

    Returns:
        The maximum output tokens.

    Raises:
        ModelCatalogError: If the model/alias is not found.
    """
    return get_model_spec(model_or_alias).max_output_tokens


def model_exists(model_or_alias: str) -> bool:
    """Check if a model or alias exists in the catalog.

    This is a non-strict check that doesn't raise on unknown models.

    Args:
        model_or_alias: A canonical model ID or an alias.

    Returns:
        True if the model/alias exists, False otherwise.
    """
    catalog = load_model_catalog()
    return model_or_alias in catalog


def get_default_model(provider: str, tier: str) -> str:
    """Return the canonical model ID for a provider+tier default.

    Args:
        provider: Provider name (e.g., "openai", "gemini", "anthropic").
        tier: Tier name (e.g., "haiku", "sonnet", "opus").

    Raises:
        ModelCatalogError: If the provider or tier is not in defaults.
    """
    catalog = load_model_catalog()
    try:
        return catalog.get_default(provider, tier)
    except KeyError:
        raise ModelCatalogError(f"No default model for {provider}/{tier}")


def get_provider_defaults() -> dict[str, dict[str, str]]:
    """Return the full defaults dict (provider -> tier -> canonical model ID).

    Returns a copy so callers cannot mutate the cached catalog.
    """
    return {p: dict(tiers) for p, tiers in load_model_catalog().defaults.items()}


def get_compact_name(model: str) -> str:
    """Get a compact display name for a model.

    Strips provider prefix, checks catalog for a short_name override,
    and applies generic shortening rules. Safe for models not in the catalog.

    Args:
        model: Model ID, possibly with provider prefix (e.g., "vertex_ai/gemini-3.1-pro-preview").

    Returns:
        A compact display name (e.g., "gemini-3-pro").
    """
    if "/" in model:
        model = model.split("/")[-1]

    catalog = load_model_catalog()
    if model in catalog:
        spec = catalog.get(model)
        if spec.short_name is not None:
            return spec.short_name

    model = model.removesuffix("-preview")

    return model
