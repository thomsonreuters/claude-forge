"""Exception hierarchy for LLM client abstraction."""


class LLMError(Exception):
    """Base exception for all LLM-related errors."""

    pass


class NoApiKeyError(LLMError):
    """Raised when required API key is not configured."""

    def __init__(self, provider: str, env_var: str, *, detail: str | None = None) -> None:
        self.provider = provider
        self.env_var = env_var
        self.detail = detail
        msg = detail if detail else f"API key not configured for {provider}. Set {env_var}."
        super().__init__(msg)


class AuthenticationError(LLMError):
    """Raised when authentication fails."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"Authentication failed for {provider}: {message}")


class ProviderError(LLMError):
    """Wrapper for provider-specific errors."""

    def __init__(self, provider: str, original: Exception) -> None:
        self.provider = provider
        self.original = original
        super().__init__(f"{provider} error: {original}")


class UnsupportedParamError(LLMError):
    """Raised when strict mode encounters unsupported parameter."""

    def __init__(self, param: str, provider: str) -> None:
        self.param = param
        self.provider = provider
        super().__init__(f"Parameter '{param}' not supported by {provider}")
