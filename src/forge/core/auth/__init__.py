"""Consolidated authentication module for Claude Forge.

This package provides:
1. SecretsProvider - unified interface for accessing secrets from env/config
2. Error types - re-exported from core.llm.errors for convenience

Usage:
    from forge.core.auth import (
        EnvSecretsProvider,
        ChainSecretsProvider,
        NoApiKeyError,
    )

    # Simple env-only secrets
    secrets = EnvSecretsProvider()
    api_key = secrets.require("ANTHROPIC_API_KEY")
"""

from forge.core.auth.capabilities import (
    CREDENTIALS,
    RETIRED_NAMES,
    Credential,
    EnvVar,
    credential_for_env_var,
    credentials_for_template,
    format_missing_credential_error,
)
from forge.core.auth.credentials_file import CredentialVersionError
from forge.core.auth.protocols import SecretsProvider
from forge.core.auth.secrets import (
    ChainSecretsProvider,
    ConfigSecretsProvider,
    EnvSecretsProvider,
    FileSecretsProvider,
)
from forge.core.auth.template_secrets import (
    TEMPLATE_SECRETS,
    resolve_env_or_credential,
)

# Re-export errors from core.llm.errors (no new types)
from forge.core.llm.errors import AuthenticationError, NoApiKeyError

__all__ = [
    # Credential registry (capabilities.py)
    "CREDENTIALS",
    "RETIRED_NAMES",
    "Credential",
    "EnvVar",
    "credential_for_env_var",
    "credentials_for_template",
    "format_missing_credential_error",
    # SecretsProvider protocol and implementations
    "SecretsProvider",
    "EnvSecretsProvider",
    "ConfigSecretsProvider",
    "FileSecretsProvider",
    "ChainSecretsProvider",
    # Template credential resolution
    "TEMPLATE_SECRETS",
    "resolve_env_or_credential",
    # Credential file errors
    "CredentialVersionError",
    # Re-exported errors (canonical source: core.llm.errors)
    "AuthenticationError",
    "NoApiKeyError",
]
