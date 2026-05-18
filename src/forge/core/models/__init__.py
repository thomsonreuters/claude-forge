"""Central model catalog for intrinsic model properties.

This module is the single source of truth for model capabilities:
- Context window sizes
- Maximum output tokens
- Thinking/reasoning support and configuration
- Temperature constraints
- Verbosity support
- API requirements (responses API, etc.)

Usage:
    from forge.core.models import (
        get_model_spec,
        get_context_window_tokens,
        get_max_output_tokens,
        resolve_model_id,
        model_exists,
    )

    # Get full spec for a model
    spec = get_model_spec("gpt-5.5")
    print(spec.context_window_tokens)  # 400000
    print(spec.litellm_reasoning_efforts)  # ('none', 'low', 'medium', 'high', 'xhigh')
    print(spec.supports_verbosity)  # True

    # Aliases work transparently
    spec = get_model_spec("openai/gpt-5.5")  # Same result

    # Convenience functions
    ctx = get_context_window_tokens("gemini-3.1-pro-preview")  # 1048576
    max_out = get_max_output_tokens("gpt-5.5")  # 128000

    # Check existence without raising
    if model_exists("unknown-model"):
        ...
"""

from forge.core.models.catalog import (
    ModelCatalogError,
    get_compact_name,
    get_context_window_tokens,
    get_default_model,
    get_max_output_tokens,
    get_model_spec,
    get_provider_defaults,
    get_system_prompt_addendum,
    load_model_catalog,
    model_exists,
    resolve_model_id,
)
from forge.core.models.pricing import (
    ModelPricing,
    calculate_cost,
    get_pricing,
    micros_to_usd,
)
from forge.core.models.types import (
    ModelCatalog,
    ModelSpec,
    TemperatureSpec,
)

__all__ = [
    # Catalog loader
    "load_model_catalog",
    # Lookup functions (strict)
    "resolve_model_id",
    "get_model_spec",
    "get_context_window_tokens",
    "get_max_output_tokens",
    # Non-strict check
    "model_exists",
    # Defaults
    "get_default_model",
    "get_provider_defaults",
    # Display
    "get_compact_name",
    # System prompt addendum
    "get_system_prompt_addendum",
    # Error type
    "ModelCatalogError",
    # Pricing
    "ModelPricing",
    "get_pricing",
    "calculate_cost",
    "micros_to_usd",
    # Types
    "ModelCatalog",
    "ModelSpec",
    "TemperatureSpec",
]
