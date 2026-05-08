"""Configuration schema definitions using dataclasses.

This module defines the structure of all Forge configuration using dataclasses.
Each dataclass represents a configuration section with typed fields and defaults.

The schema is hierarchical:
    ForgeConfig
    ├── proxy: ProxyConfig
    │   ├── gemini: ProviderConfig
    │   ├── openai: ProviderConfig
    │   └── litellm: ProviderConfig
    ├── session: SessionConfig
    └── (future: mcp, guard, status, etc.)

Usage:
    from forge.config import config

    model = config.proxy.litellm.tiers.opus
    overrides = config.proxy.litellm.tier_overrides.get("opus")
"""

from dataclasses import dataclass, field
from typing import Any

# --- CONSTANTS ---

OPENAI_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-pro",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.1-mini",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.2-pro",
    "gpt-5.3-codex",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.4-pro",
    "o1",
    "o1-mini",
    "o3",
    "o3-mini",
    "o3-pro",
    "o4-mini",
    "o4-mini-high",
]


# --- HELPER FUNCTIONS ---


def is_openai_model(model_name: str) -> bool:
    """Check if a model name refers to an OpenAI model.

    Uses strict allowlist-only matching against OPENAI_MODELS.
    No prefix heuristics - unknown gpt-* models will return False.

    Strips known provider prefixes (openai/, anthropic/) before matching.
    """
    clean_name = model_name.lower()

    if clean_name.startswith("anthropic/"):
        clean_name = clean_name[10:]
    elif clean_name.startswith("openai/"):
        clean_name = clean_name[7:]

    return clean_name in {m.lower() for m in OPENAI_MODELS}


# --- DATACLASSES ---


@dataclass
class TierModels:
    """Model mappings for each tier (haiku/sonnet/opus)."""

    haiku: str = ""
    sonnet: str = ""
    opus: str = ""

    def get(self, tier: str) -> str:
        """Get model for tier name."""
        return getattr(self, tier.lower(), self.sonnet)


@dataclass
class TierOverride:
    """Per-tier hyperparameter overrides.

    Use this to differentiate tiers that map to the same model.
    For example, if both sonnet and opus map to gpt-5.2, use tier_overrides
    to give opus higher reasoning_effort than sonnet.

    Values here override model catalog defaults. None means "use catalog default".
    """

    reasoning_effort: str | None = None  # none, low, medium, high, xhigh (model-dependent)
    verbosity: str | None = None  # low, medium, high
    temperature: float | None = None  # Override temperature for this tier
    thinking_budget_tokens: int | None = None  # For models with thinking budgets


@dataclass
class TierOverrides:
    """Per-tier overrides for hyperparameters.

    This structure allows families and proxies to customize behavior per tier,
    which is essential when multiple tiers map to the same underlying model.

    Flow:
    1. Family config defines tier_overrides as template defaults
    2. Proxy acquisition copies these to proxy overlay
    3. CLI args can override at acquisition time
    4. Proxy overlay can be modified at runtime
    """

    haiku: TierOverride | None = None
    sonnet: TierOverride | None = None
    opus: TierOverride | None = None

    def get(self, tier: str) -> TierOverride | None:
        """Get override for tier name, or None if not set."""
        return getattr(self, tier.lower(), None)


@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider (Gemini, OpenAI, LiteLLM)."""

    tiers: TierModels = field(default_factory=TierModels)
    tier_overrides: TierOverrides = field(default_factory=TierOverrides)
    auth_url: str = ""
    base_url: str = ""
    cache_ttl: float = 3600.0
    top_p: float | None = None
    enable_preamble: bool = False

    # LiteLLM-specific: API mode for OpenAI models
    openai_api_mode: str = "auto"  # auto, responses, chat_completions

    # Prompt caching mode (only affects Anthropic/Bedrock models via LiteLLM)
    # "passthrough": forward client cache_control unchanged (default)
    # "auto_inject": auto-add cache_control for long prompts
    prompt_caching: str = "passthrough"
    auto_cache_min_tokens: int = 1024

    # Error hint enrichment: append corrective hints to tool_result errors
    # before forwarding to the LLM, helping non-Claude models recover faster.
    error_hints: bool = False


def _coerce_optional_usd_cap(name: str, value: Any) -> float | None:
    """Coerce an optional USD cap to a positive float."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}: must be a positive number of USD")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}: must be a positive number of USD") from None
    if amount <= 0:
        raise ValueError(f"Invalid {name}: must be greater than 0")
    return amount


@dataclass
class CostCaps:
    """Spend cap configuration for a proxy."""

    per_day: float | None = None  # USD, rolling 24h window
    per_month: float | None = None  # USD, calendar month

    def __post_init__(self) -> None:
        self.per_day = _coerce_optional_usd_cap("costs.caps.per_day", self.per_day)
        self.per_month = _coerce_optional_usd_cap("costs.caps.per_month", self.per_month)


@dataclass
class CostConfig:
    """Cost tracking and cap configuration for a proxy."""

    caps: CostCaps = field(default_factory=CostCaps)
    cap_mode: str = "post"  # "post" (block after exceeded) or "strict" (pre-flight estimate)
    on_cap_hit: str = "reject"  # "reject" (HTTP 429) or "warn" (header only)

    def __post_init__(self) -> None:
        valid_modes = {"post", "strict"}
        if self.cap_mode not in valid_modes:
            raise ValueError(f"Invalid cap_mode: '{self.cap_mode}' (must be one of: {', '.join(sorted(valid_modes))})")
        valid_actions = {"reject", "warn"}
        if self.on_cap_hit not in valid_actions:
            raise ValueError(
                f"Invalid on_cap_hit: '{self.on_cap_hit}' (must be one of: {', '.join(sorted(valid_actions))})"
            )


@dataclass
class BackendDependency:
    """Backend dependency declaration (proxy runtime requirement).

    Declares that a proxy template requires a backend service to be running.
    Example: local LiteLLM proxies require LiteLLM backend on port 4000.
    """

    adapter: str  # e.g., "litellm"
    port: int
    required_env_vars: list[str] = field(default_factory=list)


@dataclass
class ProxyConfig:
    """Proxy server configuration."""

    gemini: ProviderConfig = field(default_factory=ProviderConfig)
    openai: ProviderConfig = field(default_factory=ProviderConfig)
    litellm: ProviderConfig = field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = field(default_factory=ProviderConfig)

    preferred_provider: str = ""  # set by --template flag
    active_template: str = ""
    default_tier: str = "sonnet"
    backend_dependency: BackendDependency | None = None
    default_port: int = 8082
    host: str = "0.0.0.0"
    tool_prefixes_to_ignore: list[str] = field(default_factory=list)
    costs: CostConfig = field(default_factory=CostConfig)

    def get_provider(self, name: str | None = None) -> ProviderConfig:
        """Get provider config by name, defaulting to preferred_provider."""
        provider = name or self.preferred_provider or "litellm"
        return getattr(self, provider, self.litellm)

    def get_model_for_tier(self, tier: str) -> str:
        """Get the configured model for a tier based on preferred_provider."""
        provider = self.get_provider()
        return provider.tiers.get(tier)


@dataclass
class SessionConfig:
    """Session management configuration."""

    default_tier: str = "sonnet"
    manifest_filename: str = "forge.session.json"
    forge_home: str = ""  # default: ~/.forge


@dataclass
class ProxyInstanceConfig:
    """Complete proxy instance configuration owned by the user.

    Unlike the previous overlay model where proxies only stored tier_overrides
    and merged with templates at runtime, this dataclass contains the full
    configuration. The user owns the entire file and can edit it directly.

    Flow:
    1. User runs `forge proxy create litellm-gemini`
    2. Template is copied to ~/.forge/proxies/{id}/proxy.yaml
    3. User can edit the file with `forge proxy edit {id}`
    4. Proxy reads this file directly at startup (no merge logic)

    The template and template_digest fields are informational only —
    they enable future `forge proxy rebase` functionality.
    """

    proxy_format: int

    template: str  # e.g., "litellm-gemini"
    template_digest: str  # SHA256 at creation time

    provider: str  # litellm | openai | gemini
    proxy_endpoint: str  # e.g., http://localhost:8085
    port: int
    upstream_base_url: str  # e.g., https://litellm.corp.com

    tiers: TierModels
    tier_overrides: TierOverrides = field(default_factory=TierOverrides)
    default_tier: str = "sonnet"

    provider_settings: dict[str, Any] = field(default_factory=dict)

    # Copied from template into proxy.yaml; controls Anthropic/Bedrock prompt caching via LiteLLM.
    prompt_caching: str = "passthrough"
    auto_cache_min_tokens: int = 1024

    costs: dict[str, Any] = field(default_factory=dict)

    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        """Validate proxy instance configuration fields."""
        if self.proxy_format != 1:
            raise ValueError(f"Unsupported proxy_format: {self.proxy_format} (expected 1)")

        valid_providers = {"litellm", "openai", "gemini", "openrouter"}
        if self.provider not in valid_providers:
            raise ValueError(
                f"Invalid provider: '{self.provider}' (must be one of: {', '.join(sorted(valid_providers))})"
            )

        if not self.proxy_endpoint:
            raise ValueError("proxy_endpoint is required (e.g., 'http://localhost:8085')")
        if not self.upstream_base_url:
            raise ValueError("upstream_base_url is required (e.g., 'https://litellm.corp.com')")

        if not 1 <= self.port <= 65535:
            raise ValueError(f"Invalid port: {self.port} (must be 1-65535)")

        if not self.tiers.sonnet:
            raise ValueError("Tiers must define at least 'sonnet' model")

        valid_tiers = {"haiku", "sonnet", "opus"}
        if self.default_tier not in valid_tiers:
            raise ValueError(
                f"Invalid default_tier: '{self.default_tier}' (must be one of: {', '.join(sorted(valid_tiers))})"
            )

        if self.costs is None:
            self.costs = {}
        if not isinstance(self.costs, dict):
            raise ValueError("Invalid costs: must be a mapping")
        if self.costs:
            raw_caps = self.costs.get("caps", {}) or {}
            if not isinstance(raw_caps, dict):
                raise ValueError("Invalid costs.caps: must be a mapping")
            CostConfig(
                caps=CostCaps(
                    per_day=raw_caps.get("per_day"),
                    per_month=raw_caps.get("per_month"),
                ),
                cap_mode=self.costs.get("cap_mode", "post"),
                on_cap_hit=self.costs.get("on_cap_hit", "reject"),
            )


@dataclass
class ForgeConfig:
    """Root configuration for all Forge components.

    This is the top-level config that aggregates all component configs.
    Access via the singleton: `from forge.config import config`
    """

    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    session: SessionConfig = field(default_factory=SessionConfig)

    # Future: mcp, guard, status

    def to_dict(self) -> dict[str, Any]:
        """Convert config to nested dict (for serialization)."""
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ForgeConfig":
        """Create config from nested dict."""
        from forge.config.dataclass_utils import dict_to_dataclass

        return dict_to_dataclass(cls, data)
