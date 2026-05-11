"""Tier-aware client factory for proxy model routing.

Creates and caches LLM client instances keyed by (model_name, tier),
with resolved hyperparameters from env vars, tier overrides, and provider config.
Actual credential fetching is delegated to core.llm.CredentialManager.
"""

import asyncio
import logging
import os
import time
from enum import Enum
from threading import Lock
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from forge.config import config
from forge.core.llm.types import ModelHyperparameters
from forge.core.models import (
    ModelCatalogError,
    get_max_output_tokens,
    model_exists,
)

logger = logging.getLogger(__name__)


DEFAULT_MAX_OUTPUT_TOKENS = 16384

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_local_url(url: str) -> bool:
    """Check if a URL points to a local host."""
    try:
        parsed = urlparse(url)
        return (parsed.hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


def _enforce_max_output_tokens_cap(model_name: str, requested: int | None, *, strict: bool = True) -> int:
    """Enforce the catalog's max_output_tokens as a hard cap.

    The model catalog defines the maximum output tokens each model can produce.
    This function ensures requested values don't exceed that ceiling.

    Args:
        model_name: Model ID (canonical or alias).
        requested: Requested max_tokens from config/env/request (or None for default).
        strict: If True (default), raise on unknown models. If False, return
                requested or a safe default for models not in the catalog
                (used by OpenRouter where the model space is open).

    Returns:
        Effective max_tokens, capped to catalog limit.

    Raises:
        ModelCatalogError: If model is unknown (strict mode) or requested exceeds catalog cap.
    """
    if not model_exists(model_name):
        if strict:
            raise ModelCatalogError(f"Model {model_name!r} not in catalog. Add it to core/data/model_catalog.yaml.")
        logger.debug(f"Model {model_name!r} not in catalog, using default max_output_tokens")
        return requested if requested is not None else DEFAULT_MAX_OUTPUT_TOKENS

    catalog_cap = get_max_output_tokens(model_name)

    if requested is None:
        return catalog_cap

    if requested > catalog_cap:
        raise ModelCatalogError(
            f"Requested max_tokens ({requested}) exceeds model {model_name!r} catalog cap ({catalog_cap}). "
            f"Update catalog or reduce max_tokens override."
        )

    return requested


class ModelProvider(Enum):
    """Supported model providers."""

    LITELLM = "litellm"
    OPENROUTER = "openrouter"
    UNKNOWN = "unknown"


class TierClientFactory:
    """Tier-aware client factory for proxy model routing.

    Creates and caches LLM client instances keyed by (model_name, tier)
    with resolved hyperparameters. Delegates credential fetching to
    core.llm.CredentialManager via CoreLLMClientAdapter.

    Features:
    - Automatic model type detection
    - Tier-specific hyperparameter resolution (env > tier_override > config)
    - Unified caching with configurable TTL
    - Retry on authentication failure
    - Thread-safe client management
    """

    _instance: "TierClientFactory | None" = None
    _lock = Lock()
    _initialized: bool = False

    def __new__(cls):
        """Singleton pattern to ensure only one manager exists."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TierClientFactory, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, default_ttl: Optional[float] = 3600):
        """Initialize the tier client factory."""
        if self._initialized:
            return

        # Tier is included in key to support same model with different hyperparameters
        self._cache: Dict[tuple[str, str], tuple[Any, float, ModelProvider]] = {}

        self._default_ttl = float(os.getenv("CREDENTIAL_CACHE_TTL", str(default_ttl)))  # 1 hour default
        self._litellm_ttl = float(os.getenv("LITELLM_CACHE_TTL", str(self._default_ttl)))
        self._upstream_base_url_cache: tuple[str, str | None] | None = None

        self._refresh_lock = asyncio.Lock()
        self._initialized = True

        # Lazy imports to avoid circular dependencies
        self._client_classes: Dict[ModelProvider, type] = {}

        ttl_config = []
        if os.getenv("CREDENTIAL_CACHE_TTL"):
            ttl_config.append(f"Default: {self._default_ttl}s")
        if os.getenv("LITELLM_CACHE_TTL"):
            ttl_config.append(f"LiteLLM (custom): {self._litellm_ttl}s")
        else:
            ttl_config.append(f"LiteLLM: {self._litellm_ttl}s (using default)")

        logger.info(f"TierClientFactory initialized - TTL configuration: {', '.join(ttl_config)}")

    def _detect_provider(self, model_name: str) -> ModelProvider:
        """Detect the model provider from the model name or PREFERRED_PROVIDER.

        PREFERRED_PROVIDER (set by the proxy server from the template) takes
        precedence over model-name prefix detection. This prevents OpenRouter
        model IDs like ``anthropic/claude-sonnet-4.6`` from being misrouted
        to LiteLLM via the ``anthropic/`` prefix match.

        Args:
            model_name: The model identifier

        Returns:
            ModelProvider enum indicating the provider
        """
        preferred = os.getenv("PREFERRED_PROVIDER", "")
        if preferred == "openrouter":
            return ModelProvider.OPENROUTER

        model_family = os.getenv("MODEL_FAMILY", "").upper()
        if model_family == "OPENROUTER":
            return ModelProvider.OPENROUTER

        clean_name = model_name.lower()

        if "/" in clean_name and any(
            clean_name.startswith(prefix)
            for prefix in [
                "openai/",
                "anthropic/",
                "vertex_ai/",
                "bedrock/",
                "replicate/",
                "together_ai/",
                "gemini/",
            ]
        ):
            return ModelProvider.LITELLM

        if model_family == "LITELLM":
            return ModelProvider.LITELLM

        logger.warning(f"Unknown model provider for model: {model_name}, defaulting to LiteLLM")
        return ModelProvider.LITELLM

    def _get_upstream_base_url(self) -> str | None:
        """Get the proxy's upstream base URL from the instance config.

        Reads the proxy.yaml for the current proxy instance to determine
        whether the upstream is local or remote.
        """
        proxy_id = os.getenv("FORGE_PROXY_ID")
        if not proxy_id:
            return None
        if self._upstream_base_url_cache and self._upstream_base_url_cache[0] == proxy_id:
            return self._upstream_base_url_cache[1]
        try:
            from forge.config.loader import load_proxy_instance_config

            instance = load_proxy_instance_config(proxy_id)
            upstream = instance.upstream_base_url if instance else None
            if upstream:
                self._upstream_base_url_cache = (proxy_id, upstream)
            return upstream
        except Exception:
            logger.debug("Failed to resolve upstream base URL for proxy %s", proxy_id, exc_info=True)
            return None

    def _get_ttl_for_provider(self, provider: ModelProvider) -> float:
        """Get the TTL for a specific provider."""
        if provider == ModelProvider.LITELLM:
            return self._litellm_ttl
        return self._default_ttl

    def _get_tier_for_model(self, model_name: str, provider: ModelProvider) -> Optional[str]:
        """Detect which tier (haiku/sonnet/opus) a model belongs to.

        Args:
            model_name: The model identifier (e.g., "openai/gpt-4o-mini")
            provider: The provider type

        Returns:
            Tier name (haiku/sonnet/opus) or None if not found
        """
        prefix_map = {
            ModelProvider.LITELLM: "LITELLM",
            ModelProvider.OPENROUTER: "OPENROUTER",
        }
        prefix = prefix_map.get(provider)
        if not prefix:
            return None

        for tier in ["haiku", "sonnet", "opus"]:
            tier_model = os.getenv(f"{prefix}_{tier.upper()}_MODEL")
            if tier_model and tier_model.lower() == model_name.lower():
                return tier

        return None

    def _import_client_class(self, provider: ModelProvider):
        """Lazy import client classes to avoid circular dependencies."""
        if provider not in self._client_classes:
            if provider in (ModelProvider.LITELLM, ModelProvider.OPENROUTER):
                from forge.proxy.client_adapter import CoreLLMClientAdapter

                self._client_classes[provider] = CoreLLMClientAdapter

    def detect_provider_for_model(self, model_name: str) -> ModelProvider:
        """
        Public method to detect provider for a given model name.

        This allows the server to determine the provider before converting
        requests, enabling provider-specific schema handling.

        Args:
            model_name: The model identifier

        Returns:
            ModelProvider enum indicating the provider
        """
        return self._detect_provider(model_name)

    async def get_client(
        self, model_name: str, tier: Optional[str] = None
    ) -> Any:  # Returns AbstractLLMClient instances
        """
        Get client for the specified model.

        Automatically detects model type and returns appropriate LiteLLM client.

        Args:
            model_name: The model identifier
            tier: The tier name (haiku/sonnet/opus) for tier-specific hyperparameters.
                  If not provided, attempts to auto-detect from model name.

        Returns:
            Client instance for the appropriate provider

        Raises:
            AuthenticationError: If credentials cannot be obtained
        """
        provider = self._detect_provider(model_name)
        ttl = self._get_ttl_for_provider(provider)

        # Auto-detect tier as a fallback for backwards compatibility
        if tier is None:
            tier = self._get_tier_for_model(model_name, provider) or "sonnet"
            logger.debug(f"Auto-detected tier '{tier}' for model {model_name}")

        # Cache key includes tier to support same model with different hyperparameters
        cache_key = (model_name, tier)

        if cache_key in self._cache:
            cached_data, fetch_time, cached_provider = self._cache[cache_key]
            age = time.monotonic() - fetch_time

            if age < ttl and cached_provider == provider:
                logger.debug(f"Using cached client for {model_name} (tier={tier}, {provider.value}, age: {age:.0f}s)")
                return cached_data
            else:
                logger.info(
                    f"Cache expired or provider changed for {model_name} (tier={tier}, age: {age:.0f}s, ttl: {ttl}s)"
                )

        async with self._refresh_lock:
            # Double-check after acquiring lock (use cache_key which includes tier)
            if cache_key in self._cache:
                cached_data, fetch_time, cached_provider = self._cache[cache_key]
                if time.monotonic() - fetch_time < ttl and cached_provider == provider:
                    return cached_data

            if provider not in (ModelProvider.LITELLM, ModelProvider.OPENROUTER):
                raise ValueError(f"Unsupported provider: {provider}")

            self._import_client_class(provider)

            # Resolve hyperparameters via the single source of truth
            default_hyperparams = self._resolve_tier_hyperparams(provider, tier, model_name)

            if provider == ModelProvider.OPENROUTER:
                core_provider = "openrouter"
            else:
                from forge.core.llm.detection import (
                    detect_provider as core_detect_provider,
                )

                core_provider = core_detect_provider(model_name)

                # Override to litellm_local when upstream is localhost.
                # detect_provider uses model prefix (openai/ -> litellm_remote),
                # but local templates route through a local LiteLLM that needs
                # no API key. The proxy instance config's upstream_base_url is
                # the authoritative source for local vs remote.
                if core_provider == "litellm_remote":
                    upstream = self._get_upstream_base_url()
                    if upstream and _is_local_url(upstream):
                        core_provider = "litellm_local"

            client = self._client_classes[provider](
                model=model_name,
                provider=core_provider,
                max_tokens_override=default_hyperparams.max_tokens,
                tier=tier,
                default_hyperparams=default_hyperparams,
            )

            self._cache[cache_key] = (client, time.monotonic(), provider)
            logger.info(f"Cached new {provider.value} client (core.llm) for {model_name} (tier={tier})")

            return client

    async def invalidate_and_retry(
        self, model_name: str, tier: Optional[str] = None
    ) -> Any:  # Returns AbstractLLMClient instances
        """
        Invalidate cached credentials and fetch new ones.

        Called when authentication fails, indicating credentials may be expired.

        Args:
            model_name: The model whose credentials should be refreshed
            tier: The tier name (haiku/sonnet/opus). If None, invalidates all tiers for this model.

        Returns:
            Fresh credentials or client
        """
        logger.warning(f"Invalidating cached credentials for {model_name} (tier={tier}) due to auth failure")

        async with self._refresh_lock:
            # Remove from cache - handle both specific tier and all tiers
            if tier is not None:
                cache_key = (model_name, tier)
                if cache_key in self._cache:
                    del self._cache[cache_key]
            else:
                keys_to_remove = [k for k in self._cache if k[0] == model_name]
                for key in keys_to_remove:
                    del self._cache[key]

            return await self.get_client(model_name, tier=tier)

    def _resolve_tier_hyperparams(
        self,
        provider: ModelProvider,
        tier: str,
        model_name: str,
    ) -> ModelHyperparameters:
        """Single source of truth for tier-specific hyperparameters.

        Used by both get_client() (actual client creation) and
        get_default_hyperparams_for_tier() (runtime truth reporting).

        Priority chain per field:
        - max_tokens: env ({PREFIX}_{TIER}_MAX_TOKENS) > provider config (tokens.override), capped by catalog
        - reasoning/verbosity/thinking: env > tier_override > provider config
        - temperature: tier_override > provider config override > provider config default
        - top_p: provider config only

        Fields left as None fall through to core.llm's own defaults.
        """
        from forge.core.llm.types import ThinkingConfig

        if provider == ModelProvider.LITELLM:
            provider_cfg = config.proxy.litellm
            env_prefix = "LITELLM"
        elif provider == ModelProvider.OPENROUTER:
            provider_cfg = config.proxy.openrouter
            env_prefix = "OPENROUTER"
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        tier_upper = tier.upper()
        tier_override = provider_cfg.tier_overrides.get(tier)

        # max_tokens: env > catalog cap (lenient for OpenRouter's open model space)
        tier_max_tokens = os.getenv(f"{env_prefix}_{tier_upper}_MAX_TOKENS")
        requested_max_tokens = int(tier_max_tokens) if tier_max_tokens else None
        catalog_strict = provider != ModelProvider.OPENROUTER
        max_tokens_override = _enforce_max_output_tokens_cap(model_name, requested_max_tokens, strict=catalog_strict)

        # reasoning_effort: env > tier_override
        tier_reasoning: str | None
        tier_reasoning_env = os.getenv(f"{env_prefix}_{tier_upper}_REASONING_EFFORT")
        if tier_reasoning_env:
            tier_reasoning = tier_reasoning_env
        elif tier_override and tier_override.reasoning_effort is not None:
            tier_reasoning = tier_override.reasoning_effort
        else:
            tier_reasoning = None

        # verbosity: env > tier_override
        tier_verbosity: str | None
        tier_verbosity_env = os.getenv(f"{env_prefix}_{tier_upper}_VERBOSITY")
        if tier_verbosity_env:
            tier_verbosity = tier_verbosity_env
        elif tier_override and tier_override.verbosity is not None:
            tier_verbosity = tier_override.verbosity
        else:
            tier_verbosity = None

        # thinking: env > tier_override
        tier_thinking_type = os.getenv(f"{env_prefix}_{tier_upper}_THINKING_TYPE")
        if tier_thinking_type:
            tier_thinking: dict[str, str | int] | None = {
                "type": tier_thinking_type,
                "budget_tokens": int(os.getenv(f"{env_prefix}_{tier_upper}_THINKING_BUDGET_TOKENS", "1024")),
            }
        elif tier_override and tier_override.thinking_budget_tokens is not None:
            if tier_override.thinking_budget_tokens <= 0:
                tier_thinking = None
            else:
                tier_thinking = {
                    "type": "enabled",
                    "budget_tokens": tier_override.thinking_budget_tokens,
                }
        else:
            tier_thinking = None

        thinking_config = ThinkingConfig(**tier_thinking) if tier_thinking else None  # type: ignore[arg-type]

        default_hyperparams = ModelHyperparameters(
            max_tokens=max_tokens_override,
            reasoning_effort=tier_reasoning,  # type: ignore[arg-type]
            verbosity=tier_verbosity,  # type: ignore[arg-type]
            thinking=thinking_config,
        )

        # temperature: tier_override only
        if tier_override and tier_override.temperature is not None:
            default_hyperparams.temperature = tier_override.temperature

        # top_p: provider config only
        if provider_cfg.top_p is not None:
            default_hyperparams.top_p = provider_cfg.top_p

        return default_hyperparams

    def get_default_hyperparams_for_tier(self, *, provider: str, tier: str, model_name: str) -> ModelHyperparameters:
        """Return the computed default hyperparameters for a provider/tier.

        Used by runtime truth reporting (GET /) and any other caller that needs
        the effective baseline hyperparameters without creating a client.

        Delegates to _resolve_tier_hyperparams() — the single source of truth.
        """
        if provider.lower() == "litellm":
            provider_enum = ModelProvider.LITELLM
        elif provider.lower() == "openrouter":
            provider_enum = ModelProvider.OPENROUTER
        else:
            raise ValueError(f"Unsupported provider for default hyperparams: {provider}")

        return self._resolve_tier_hyperparams(provider_enum, tier, model_name)

    def get_cache_status(self) -> Dict[str, Any]:
        """Get current cache status for monitoring."""
        status: Dict[str, Any] = {
            "ttl_configuration": {
                "default": self._default_ttl,
                "litellm": self._litellm_ttl,
            },
            "cached_models": {},
        }
        current_time = time.monotonic()

        for cache_key, (_, fetch_time, provider) in self._cache.items():
            model_name, tier = cache_key
            ttl = self._get_ttl_for_provider(provider)
            age = current_time - fetch_time
            remaining_ttl = max(0, ttl - age)

            # Use "model_name:tier" as display key for readability
            display_key = f"{model_name}:{tier}"
            status["cached_models"][display_key] = {
                "model": model_name,
                "tier": tier,
                "provider": provider.value,
                "age_seconds": round(age, 1),
                "remaining_ttl_seconds": round(remaining_ttl, 1),
                "ttl_seconds": ttl,
                "expired": age >= ttl,
            }

        return status

    def clear_cache(self):
        """Clear all cached credentials."""
        logger.info("Clearing all cached credentials")
        self._cache.clear()
