"""Model pricing lookup and cost calculation.

Loads pricing.yaml (shipped with Forge) and provides cost estimates
in integer microdollars (1 USD = 1_000_000 microdollars) to avoid
float accumulation drift.

Cost formula:
  (input_tokens - cached_tokens) * input_rate
  + cached_tokens * cached_input_rate
  + output_tokens * output_rate

Cached tokens are a SUBSET of input_tokens (prompt cache hits).
Subtracting prevents double-counting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from forge.core.models.catalog import model_exists, resolve_model_id

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

_MICROS_PER_DOLLAR = 1_000_000

_pricing_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token rates in USD (floats from YAML) and source label."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float
    source: str  # "catalog", "default", or "override"


def _load_pricing_yaml() -> dict[str, Any]:
    """Load pricing.yaml from package resources."""
    try:
        ref = resources.files("forge.core.data").joinpath("pricing.yaml")
        content = ref.read_text(encoding="utf-8")
    except (TypeError, AttributeError):
        with resources.open_text("forge.core.data", "pricing.yaml") as f:
            content = f.read()
    return yaml.safe_load(content)


def _get_pricing_data() -> dict[str, Any]:
    """Return cached pricing data (lazy-loaded singleton)."""
    global _pricing_data
    if _pricing_data is None:
        raw = _load_pricing_yaml()
        version = raw.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported pricing schema_version: {version} " f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
            )
        _pricing_data = raw
    return _pricing_data


def reset_pricing_cache() -> None:
    """Reset the cached pricing data (for testing)."""
    global _pricing_data
    _pricing_data = None


def _parse_model_pricing(data: dict[str, Any], source: str) -> ModelPricing:
    return ModelPricing(
        input_per_mtok=float(data["input"]),
        output_per_mtok=float(data["output"]),
        cached_input_per_mtok=float(data.get("cached_input", data["input"] * 0.1)),
        source=source,
    )


def get_pricing(model: str) -> ModelPricing:
    """Look up pricing for a model, resolving aliases.

    Resolution order:
      1. Exact match in pricing.yaml models
      2. Resolve via model catalog alias, then match
      3. Fall back to pricing.yaml default section

    Args:
        model: Model ID (canonical or alias, e.g. "anthropic/claude-sonnet-4.6").

    Returns:
        ModelPricing with per-MTok rates and source label.
    """
    data = _get_pricing_data()
    models = data.get("models", {})

    if model in models:
        return _parse_model_pricing(models[model], "catalog")

    if model_exists(model):
        try:
            canonical = resolve_model_id(model)
            if canonical in models:
                return _parse_model_pricing(models[canonical], "catalog")
        except Exception:
            pass

    default = data.get("default")
    if default:
        logger.warning("No catalog pricing for model %r; using default rates", model)
        return _parse_model_pricing(default, "default")

    logger.warning("No pricing data for model %r; using hardcoded fallback rates", model)
    return ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cached_input_per_mtok=0.30,
        source="hardcoded",
    )


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
) -> int:
    """Calculate estimated cost in microdollars (integer, 1 USD = 1_000_000).

    Cached tokens are a subset of input_tokens. The formula avoids
    double-counting by charging cached tokens at the lower cached rate
    and only the remainder at the full input rate.

    Args:
        model: Model ID (canonical or alias).
        input_tokens: Total input/prompt tokens (includes cached).
        output_tokens: Completion tokens.
        cached_tokens: Prompt cache hit tokens (subset of input_tokens).

    Returns:
        Estimated cost in microdollars (integer).
    """
    pricing = get_pricing(model)

    cached = min(cached_tokens, input_tokens)
    fresh_input = input_tokens - cached

    cost_usd = (
        fresh_input * pricing.input_per_mtok / 1_000_000
        + cached * pricing.cached_input_per_mtok / 1_000_000
        + output_tokens * pricing.output_per_mtok / 1_000_000
    )

    return round(cost_usd * _MICROS_PER_DOLLAR)


def micros_to_usd(micros: int) -> float:
    """Convert microdollars to USD float for display."""
    return micros / _MICROS_PER_DOLLAR
