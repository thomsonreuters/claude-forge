"""Credential manager for LLM providers.

Injectable singleton with TTL caching and proactive refresh.
Per-provider async locks prevent thundering herd on token refresh.

Note: Does NOT call load_dotenv() - that's the responsibility of the
CLI entrypoint or config loader to avoid import-time side effects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from forge.core.auth.protocols import SecretsProvider

from .detection import ProviderType
from .errors import NoApiKeyError

logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600.0


def _get_litellm_remote_base_url() -> str:
    """Get remote LiteLLM base URL.

    Resolution order:
    1. Template/proxy config (config.proxy.litellm.base_url)
    2. LITELLM_BASE_URL environment variable
    3. Credential file (~/.forge/credentials.yaml)
    4. Error if none set
    """
    try:
        from forge.config import config

        base_url = config.proxy.litellm.base_url
        if base_url:
            logger.debug(f"Using LiteLLM base_url from template: {base_url}")
            return base_url
    except (ImportError, AttributeError):
        pass

    env_url = os.environ.get("LITELLM_BASE_URL")
    if env_url:
        logger.debug(f"Using LiteLLM base_url from LITELLM_BASE_URL: {env_url}")
        return env_url

    from forge.core.auth.template_secrets import resolve_env_or_credential

    cred_url = resolve_env_or_credential("LITELLM_BASE_URL")
    if cred_url:
        logger.debug("Using LiteLLM base_url from credential file")
        return cred_url

    raise ValueError(
        "LiteLLM remote base_url not configured. "
        "Use 'forge proxy create <template> --base-url <url>' or "
        "'forge auth login -p litellm-remote' to store it."
    )


def _get_litellm_local_base_url() -> str:
    """Get local LiteLLM base URL.

    Resolution order:
    1. Template config (config.proxy.litellm.base_url)
    2. LITELLM_LOCAL_BASE_URL environment variable
    3. Derive from backend_dependency.port (http://localhost:{port})
    4. Error if none available
    """
    try:
        from forge.config import config

        base_url = config.proxy.litellm.base_url
        if base_url:
            logger.debug(f"Using LiteLLM base_url from template: {base_url}")
            return base_url
    except (ImportError, AttributeError):
        pass

    env_url = os.environ.get("LITELLM_LOCAL_BASE_URL")
    if env_url:
        logger.debug(f"Using LiteLLM base_url from LITELLM_LOCAL_BASE_URL: {env_url}")
        return env_url

    try:
        from forge.config import config

        dep = config.proxy.backend_dependency
        if dep and dep.port:
            derived = f"http://localhost:{dep.port}"
            logger.debug(f"Using LiteLLM base_url derived from backend_dependency: {derived}")
            return derived
    except (ImportError, AttributeError):
        pass

    raise ValueError(
        "LiteLLM local base_url not configured. "
        "Set LITELLM_LOCAL_BASE_URL environment variable or use a template with backend_dependency."
    )


OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def _get_openrouter_base_url() -> str:
    """Get OpenRouter base URL.

    Resolution order:
    1. Template/proxy config (config.proxy.openrouter.base_url)
    2. OPENROUTER_BASE_URL environment variable or credential file
    3. Default: https://openrouter.ai/api/v1
    """
    try:
        from forge.config import config

        base_url = config.proxy.openrouter.base_url
        if base_url:
            logger.debug(f"Using OpenRouter base_url from template: {base_url}")
            return base_url
    except (ImportError, AttributeError):
        pass

    from forge.core.auth.template_secrets import resolve_env_or_credential

    resolved = resolve_env_or_credential("OPENROUTER_BASE_URL")
    if resolved:
        logger.debug(f"Using OpenRouter base_url from env/credential file: {resolved}")
        return resolved

    return OPENROUTER_DEFAULT_BASE_URL


class CredentialManager:
    """Injectable credential manager with TTL caching and proactive refresh.

    Use CredentialManager.default() to get the global instance,
    or create your own instance for testing.

    Configuration:
        Base URLs are read from templates (config.proxy.litellm.base_url).
        Remote templates can also fall back to LITELLM_BASE_URL env var.

    Environment Variables (Secrets Only):
        CREDENTIAL_CACHE_TTL: Global default TTL in seconds (default: 3600)
        LITELLM_API_KEY: Remote LiteLLM API key (secret)
        LITELLM_LOCAL_API_KEY: Local LiteLLM API key (optional secret)

    Args:
        default_ttl: Default TTL for cached credentials in seconds.
        secrets: Optional SecretsProvider for reading secrets. If not provided,
                 falls back to reading directly from os.environ.
    """

    _default_instance: "CredentialManager | None" = None

    def __init__(
        self,
        default_ttl: float = DEFAULT_TTL,
        secrets: SecretsProvider | None = None,
    ) -> None:
        # Cache structure: provider -> (credentials, fetch_time, ttl)
        self._cache: dict[str, tuple[dict[str, Any], float, float]] = {}
        self._default_ttl = float(os.getenv("CREDENTIAL_CACHE_TTL", str(default_ttl)))
        # Per-provider locks to prevent concurrent credential fetches
        self._locks: dict[str, asyncio.Lock] = {}
        self._secrets = secrets

    @classmethod
    def default(cls) -> "CredentialManager":
        """Get or create the default global instance.

        Wires EnvSecretsProvider -> FileSecretsProvider chain so credentials
        from ~/.forge/credentials.yaml are available as fallback to env vars.
        """
        if cls._default_instance is None:
            from forge.core.auth import ChainSecretsProvider, EnvSecretsProvider
            from forge.core.auth.secrets import FileSecretsProvider

            secrets = ChainSecretsProvider(
                EnvSecretsProvider(),
                FileSecretsProvider(),
            )
            cls._default_instance = cls(secrets=secrets)
        return cls._default_instance

    @classmethod
    def reset_default(cls) -> None:
        """Reset the default instance (for testing)."""
        cls._default_instance = None

    def _get_lock(self, provider: str) -> asyncio.Lock:
        """Get or create a lock for the given provider."""
        if provider not in self._locks:
            self._locks[provider] = asyncio.Lock()
        return self._locks[provider]

    def _resolve_secrets(self) -> SecretsProvider:
        """Return configured secrets provider, falling back to env-only."""
        if self._secrets is not None:
            return self._secrets
        from forge.core.auth import EnvSecretsProvider

        return EnvSecretsProvider()

    async def get_credentials(
        self,
        provider: ProviderType,
    ) -> dict[str, Any]:
        """Get credentials for a provider, refreshing if needed.

        Args:
            provider: Provider type to get credentials for.

        Returns:
            Dictionary with provider-specific credentials.

        Raises:
            NoApiKeyError: If required credentials are not configured.
        """
        # Check without lock; common path avoids lock contention.
        if provider in self._cache:
            creds, fetch_time, ttl = self._cache[provider]
            age = time.monotonic() - fetch_time
            if age < ttl:
                logger.debug(f"Using cached credentials for {provider} (age: {age:.0f}s)")
                return creds

        async with self._get_lock(provider):
            # Double-checked locking: another coroutine may have refreshed while we waited.
            if provider in self._cache:
                creds, fetch_time, ttl = self._cache[provider]
                if time.monotonic() - fetch_time < ttl:
                    return creds

            creds = await self._fetch_credentials(provider)
            self._cache[provider] = (creds, time.monotonic(), self._default_ttl)
            logger.info(f"Cached fresh credentials for {provider}")
            return creds

    async def _fetch_credentials(self, provider: ProviderType) -> dict[str, Any]:
        """Fetch credentials for a provider from environment.

        Args:
            provider: Provider type to fetch credentials for.

        Returns:
            Dictionary with provider-specific credentials.

        Raises:
            NoApiKeyError: If required credentials are not configured.
        """
        if provider == "litellm_remote":
            return self._get_litellm_remote_credentials()
        elif provider == "litellm_local":
            return self._get_litellm_local_credentials()
        elif provider == "anthropic":
            return self._get_anthropic_credentials()
        elif provider == "openrouter":
            return self._get_openrouter_credentials()
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def _get_litellm_remote_credentials(self) -> dict[str, Any]:
        """Get credentials for remote LiteLLM.

        Uses unified config for base_url (set by template), with LITELLM_BASE_URL env fallback.
        API key is required for remote endpoints (non-localhost).
        SSL certificate from SSL_CERT_FILE or REQUESTS_CA_BUNDLE for remote proxies.
        """
        secrets = self._resolve_secrets()
        base_url = _get_litellm_remote_base_url()
        api_key = secrets.get("LITELLM_API_KEY", "")

        # For remote LiteLLM, API key is required (unless localhost)
        is_local = "localhost" in base_url or "127.0.0.1" in base_url
        if not api_key and not is_local:
            raise NoApiKeyError("litellm_remote", "LITELLM_API_KEY")

        # SSL cert paths are non-secret, read directly from env
        ssl_cert = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")

        result = {
            "base_url": base_url,
            "api_key": api_key,
        }

        if ssl_cert:
            result["ssl_cert"] = ssl_cert
            logger.debug(f"Using SSL certificate for remote LiteLLM: {ssl_cert}")

        return result

    def _get_litellm_local_credentials(self) -> dict[str, Any]:
        """Get credentials for local LiteLLM (personal API keys).

        Uses unified config for base_url (set by template overlay), with env fallback.
        API key is optional for local - proxy handles auth via GEMINI_API_KEY etc.
        """
        secrets = self._resolve_secrets()
        base_url = _get_litellm_local_base_url()
        api_key = secrets.get("LITELLM_LOCAL_API_KEY", "not-needed")

        return {
            "base_url": base_url,
            "api_key": api_key,
        }

    def _get_anthropic_credentials(self) -> dict[str, Any]:
        """Get credentials for direct Anthropic API."""
        secrets = self._resolve_secrets()
        api_key = secrets.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise NoApiKeyError("anthropic", "ANTHROPIC_API_KEY")

        return {
            "api_key": api_key,
        }

    def _get_openrouter_credentials(self) -> dict[str, Any]:
        """Get credentials for OpenRouter.

        Resolution mirrors the LiteLLM remote pattern: config base_url first,
        then env var, then default. API key is always required.
        """
        secrets = self._resolve_secrets()
        api_key = secrets.get("OPENROUTER_API_KEY")
        if not api_key:
            raise NoApiKeyError("openrouter", "OPENROUTER_API_KEY")

        base_url = _get_openrouter_base_url()

        return {
            "api_key": api_key,
            "base_url": base_url,
            "extra_headers": {
                "HTTP-Referer": "https://github.com/thomsonreuters/claude-forge",
                "X-OpenRouter-Title": "Claude Forge",
            },
        }

    async def invalidate(
        self,
        provider: ProviderType,
    ) -> None:
        """Invalidate cached credentials for a provider.

        Call this when authentication fails to force a refresh.

        Args:
            provider: Provider whose credentials should be invalidated.
        """
        async with self._get_lock(provider):
            if provider in self._cache:
                del self._cache[provider]
                logger.info(f"Invalidated cached credentials for {provider}")

    def get_cache_status(self) -> dict[str, Any]:
        """Get current cache status for monitoring.

        Returns:
            Dictionary with cache information per provider.
        """
        status: dict[str, Any] = {
            "default_ttl": self._default_ttl,
            "providers": {},
        }
        current_time = time.monotonic()

        for provider, (_, fetch_time, ttl) in self._cache.items():
            age = current_time - fetch_time
            status["providers"][provider] = {
                "age_seconds": round(age, 1),
                "ttl_seconds": ttl,
                "remaining_seconds": round(max(0, ttl - age), 1),
                "expired": age >= ttl,
            }

        return status

    def clear_cache(self) -> None:
        """Clear all cached credentials."""
        self._cache.clear()
        logger.info("Cleared all cached credentials")
