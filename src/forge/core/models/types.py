"""Type definitions for the model catalog.

This module defines the dataclasses that represent model specifications
and the catalog structure. All types are immutable (frozen) to ensure
the catalog remains a stable reference.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class TemperatureSpec:
    """Temperature constraints for a model."""

    min: float
    default: float
    max: float

    def __post_init__(self) -> None:
        if not (self.min <= self.default <= self.max):
            raise ValueError(
                f"Temperature invariant violated: min ({self.min}) <= default ({self.default}) <= max ({self.max})"
            )
        if self.min < 0:
            raise ValueError(f"Temperature min must be >= 0, got {self.min}")


@dataclass(frozen=True)
class ModelSpec:
    """Intrinsic properties of a model.

    These are facts about what the model CAN do, not operational config.
    Operational config (tier mappings, routing, defaults) belongs in template YAMLs.
    """

    # Basic identity
    friendly_name: str
    intelligence_score: int

    # Token limits
    context_window_tokens: int
    max_output_tokens: int
    max_thinking_tokens: int | None = None

    # Display
    short_name: str | None = None  # Compact display name (e.g., "gemini-flash"); None = derive algorithmically

    # Capability flags
    supports_thinking: bool = False
    supports_images: bool = False
    supports_verbosity: bool = False
    supports_top_p: bool = True
    supports_sampling_overrides: bool = True
    supports_1m_context: bool = False

    # Temperature configuration
    temperature_constraint: Literal["fixed", "range"] = "range"
    temperature: TemperatureSpec = field(default_factory=lambda: TemperatureSpec(0.0, 1.0, 2.0))

    # Verbosity configuration (GPT-5 non-Codex models via Responses API)
    verbosity_levels: tuple[str, ...] | None = None

    # API configuration
    use_responses_api: bool = False

    # Reasoning/thinking configuration
    # Native parameter name varies by provider:
    # - OpenAI: "reasoning_effort"
    # - Anthropic: "output_config.effort"
    # - Gemini 2.5: "thinking_budget"
    # - Gemini 3: "thinking_level"
    native_thinking_param: str | None = None

    # LiteLLM abstraction - supported reasoning_effort values
    # These are the values LiteLLM accepts and maps to native params
    litellm_reasoning_efforts: tuple[str, ...] | None = None
    default_reasoning_effort: str | None = None
    thinking_modes: tuple[str, ...] | None = None

    # Gemini 3 specific - thinking levels (different from reasoning_effort)
    thinking_levels: tuple[str, ...] | None = None
    default_thinking_level: str | None = None

    # Metadata
    token_estimate_multiplier: float = 1.0
    system_prompt_addendum: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.context_window_tokens <= 0:
            raise ValueError(f"context_window_tokens must be > 0, got {self.context_window_tokens}")
        if self.max_output_tokens <= 0:
            raise ValueError(f"max_output_tokens must be > 0, got {self.max_output_tokens}")
        if self.max_thinking_tokens is not None and self.max_thinking_tokens <= 0:
            raise ValueError(f"max_thinking_tokens must be > 0 or null, got {self.max_thinking_tokens}")
        if not (0 <= self.intelligence_score <= 100):
            raise ValueError(f"intelligence_score must be 0-100, got {self.intelligence_score}")
        if self.token_estimate_multiplier <= 0:
            raise ValueError(f"token_estimate_multiplier must be > 0, got {self.token_estimate_multiplier}")
        if self.temperature_constraint == "fixed":
            if self.temperature.min != self.temperature.max:
                raise ValueError(
                    f"Fixed temperature constraint requires min == max, "
                    f"got min={self.temperature.min}, max={self.temperature.max}"
                )


REQUIRED_TIERS = frozenset({"haiku", "sonnet", "opus"})


@dataclass(frozen=True)
class ModelCatalog:
    """The complete model catalog.

    Immutable container for all models and their aliases.
    Use the module-level functions to query the catalog.
    """

    schema_version: int
    models: dict[str, ModelSpec]
    aliases: dict[str, str]
    defaults: dict[str, dict[str, str]] = field(default_factory=dict)  # provider -> tier -> canonical model ID

    def resolve(self, model_or_alias: str) -> str:
        """Resolve a model ID or alias to its canonical ID.

        Args:
            model_or_alias: A canonical model ID or an alias.

        Returns:
            The canonical model ID.

        Raises:
            KeyError: If the model/alias is not found in the catalog.
        """
        if model_or_alias in self.models:
            return model_or_alias
        if model_or_alias in self.aliases:
            return self.aliases[model_or_alias]
        raise KeyError(f"Unknown model or alias: {model_or_alias!r}")

    def get(self, model_or_alias: str) -> ModelSpec:
        """Get the model spec for a model ID or alias.

        Args:
            model_or_alias: A canonical model ID or an alias.

        Returns:
            The ModelSpec for the resolved model.

        Raises:
            KeyError: If the model/alias is not found in the catalog.
        """
        canonical_id = self.resolve(model_or_alias)
        return self.models[canonical_id]

    def get_default(self, provider: str, tier: str) -> str:
        """Return the canonical model ID for a provider+tier default.

        Raises KeyError if provider or tier is not in defaults.
        """
        return self.defaults[provider][tier]

    def __contains__(self, model_or_alias: str) -> bool:
        """Check if a model or alias exists in the catalog."""
        return model_or_alias in self.models or model_or_alias in self.aliases
