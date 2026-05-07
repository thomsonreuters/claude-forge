"""Tests for config schema types (is_openai_model, dict_to_dataclass, ForgeConfig, ProxyInstanceConfig)."""

import pytest

from forge.config import load_config
from forge.config.dataclass_utils import dict_to_dataclass
from forge.config.schema import (
    OPENAI_MODELS,
    ForgeConfig,
    ProviderConfig,
    TierModels,
    is_openai_model,
)


class TestIsOpenAIModel:
    """Tests for is_openai_model helper function.

    Uses strict allowlist-only matching - no prefix heuristics.
    """

    def test_known_models_return_true(self):
        """All models in OPENAI_MODELS allowlist return True."""
        for model in OPENAI_MODELS:
            assert is_openai_model(model) is True, f"Expected {model} to be OpenAI model"

    def test_known_models_case_insensitive(self):
        """Model matching is case-insensitive."""
        assert is_openai_model("GPT-5.2") is True
        assert is_openai_model("gpt-5.2") is True
        assert is_openai_model("Gpt-5.2") is True

    def test_gpt52_codex_recognized(self):
        """gpt-5.2-codex is recognized as OpenAI."""
        assert is_openai_model("gpt-5.2-codex") is True
        assert is_openai_model("openai/gpt-5.2-codex") is True

    def test_openai_prefix_stripped(self):
        """openai/ prefix is stripped before matching."""
        assert is_openai_model("openai/gpt-5.2") is True
        assert is_openai_model("openai/gpt-4o") is True
        assert is_openai_model("openai/o3-mini") is True

    def test_anthropic_prefix_stripped(self):
        """anthropic/ prefix is stripped (returns False for Claude models)."""
        assert is_openai_model("anthropic/claude-3.5-sonnet") is False

    def test_unknown_gpt_model_returns_false(self):
        """Unknown gpt-* models return False (strict allowlist).

        This is the key behavioral change: no prefix heuristics.
        """
        assert is_openai_model("gpt-nonsense") is False
        assert is_openai_model("gpt-99") is False
        assert is_openai_model("gpt-6") is False  # Not in allowlist yet
        assert is_openai_model("gpt-4o-preview-special") is False  # Variant not in list

    def test_unknown_o_series_returns_false(self):
        """Unknown o-series models return False (strict allowlist)."""
        assert is_openai_model("o5") is False
        assert is_openai_model("o1-turbo") is False  # Not in allowlist
        assert is_openai_model("o99") is False

    def test_non_openai_models_return_false(self):
        """Non-OpenAI models return False."""
        assert is_openai_model("claude-3.5-sonnet") is False
        assert is_openai_model("gemini-2.0-flash") is False
        assert is_openai_model("llama-3") is False

    def test_litellm_prefix_not_stripped(self):
        """litellm/ prefix is NOT stripped (not a provider prefix)."""
        assert is_openai_model("litellm/gpt-5.2") is False


class TestDictToDataclass:
    """Tests for dict to dataclass conversion."""

    def test_simple_dataclass(self):
        """Converts simple dict to dataclass."""
        data = {"haiku": "model-a", "sonnet": "model-b", "opus": "model-c"}
        result = dict_to_dataclass(TierModels, data)

        assert result.haiku == "model-a"
        assert result.sonnet == "model-b"
        assert result.opus == "model-c"

    def test_nested_dataclass(self):
        """Converts nested dict to nested dataclass."""
        data = {
            "tiers": {"haiku": "h", "sonnet": "s", "opus": "o"},
            "cache_ttl": 1800,
        }
        result = dict_to_dataclass(ProviderConfig, data)

        assert result.tiers.haiku == "h"
        assert result.cache_ttl == 1800

    def test_missing_fields_use_defaults(self):
        """Missing fields get default values."""
        data = {"tiers": {"sonnet": "s"}}
        result = dict_to_dataclass(ProviderConfig, data)

        assert result.tiers.sonnet == "s"
        assert result.tiers.haiku == ""  # Default
        assert result.cache_ttl == 3600.0  # Default

    def test_optional_primitive_with_value(self):
        """Optional float field accepts value (PEP604 union syntax)."""
        data = {
            "tiers": {"sonnet": "s"},
            "top_p": 0.9,  # float | None field
        }
        result = dict_to_dataclass(ProviderConfig, data)

        assert result.top_p == 0.9

    def test_optional_primitive_with_none(self):
        """Optional float field accepts None explicitly."""
        data = {
            "tiers": {"sonnet": "s"},
            "top_p": None,
        }
        result = dict_to_dataclass(ProviderConfig, data)

        assert result.top_p is None

    def test_optional_primitive_missing(self):
        """Optional field defaults to None when missing."""
        data = {"tiers": {"sonnet": "s"}}
        result = dict_to_dataclass(ProviderConfig, data)

        # top_p is not in data, should be None (default)
        assert result.top_p is None

    def test_optional_nested_dataclass_with_dict(self):
        """Optional nested dataclass field converts dict correctly.

        Tests that Optional[DataClass] fields properly unwrap the Optional
        and recursively convert the dict to the dataclass type.
        """
        from forge.config.schema import TierOverride, TierOverrides

        data = {
            "tiers": {"sonnet": "s"},
            "tier_overrides": {"opus": {"reasoning_effort": "high", "temperature": 0.3}},
        }
        result = dict_to_dataclass(ProviderConfig, data)

        assert isinstance(result.tier_overrides, TierOverrides)
        assert isinstance(result.tier_overrides.opus, TierOverride)
        assert result.tier_overrides.opus.reasoning_effort == "high"
        assert result.tier_overrides.opus.temperature == 0.3

    def test_optional_nested_dataclass_with_none(self):
        """Optional nested dataclass field accepts None."""
        from forge.config.schema import TierOverrides

        data = {
            "tiers": {"sonnet": "s"},
            "tier_overrides": {"opus": None},
        }
        result = dict_to_dataclass(ProviderConfig, data)

        assert isinstance(result.tier_overrides, TierOverrides)
        assert result.tier_overrides.opus is None

    def test_optional_int_field(self):
        """Optional int field (int | None) works correctly."""
        from forge.config.schema import TierOverride

        # TierOverride.thinking_budget_tokens is int | None
        data = {"thinking_budget_tokens": 8192}
        result = dict_to_dataclass(TierOverride, data)

        assert result.thinking_budget_tokens == 8192

    def test_optional_str_field(self):
        """Optional str field (str | None) works correctly."""
        from forge.config.schema import TierOverride

        # TierOverride.reasoning_effort is str | None
        data = {"reasoning_effort": "high", "verbosity": "low"}
        result = dict_to_dataclass(TierOverride, data)

        assert result.reasoning_effort == "high"
        assert result.verbosity == "low"


class TestForgeConfigMethods:
    """Tests for ForgeConfig methods."""

    def test_to_dict(self):
        """to_dict returns nested dict."""
        config = load_config()

        d = config.to_dict()

        assert isinstance(d, dict)
        assert "proxy" in d
        assert "session" in d
        assert d["session"]["default_tier"] == "sonnet"

    def test_from_dict(self):
        """from_dict creates config from dict."""
        data = {
            "proxy": {
                "default_port": 1234,
                "preferred_provider": "test",
            },
            "session": {
                "default_tier": "opus",
            },
        }

        config = ForgeConfig.from_dict(data)

        assert config.proxy.default_port == 1234
        assert config.proxy.preferred_provider == "test"
        assert config.session.default_tier == "opus"


class TestProxyInstanceConfig:
    """Tests for ProxyInstanceConfig schema."""

    def test_proxy_config_creation(self):
        """ProxyInstanceConfig can be created with required fields."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        config = ProxyInstanceConfig(
            proxy_format=1,
            template="litellm-gemini",
            template_digest="sha256:abc123def456",
            provider="litellm",
            proxy_endpoint="http://localhost:8084",
            port=8084,
            upstream_base_url="https://litellm.test.example.com",
            tiers=TierModels(haiku="h", sonnet="s", opus="o"),
        )

        assert config.proxy_format == 1
        assert config.template == "litellm-gemini"
        assert config.template_digest == "sha256:abc123def456"
        assert config.provider == "litellm"
        assert config.proxy_endpoint == "http://localhost:8084"
        assert config.port == 8084
        assert config.tiers.haiku == "h"
        assert config.tiers.sonnet == "s"
        assert config.tiers.opus == "o"
        assert config.default_tier == "sonnet"  # Default

    def test_proxy_config_with_tier_overrides(self):
        """ProxyInstanceConfig supports tier_overrides."""
        from forge.config.schema import (
            ProxyInstanceConfig,
            TierModels,
            TierOverride,
            TierOverrides,
        )

        config = ProxyInstanceConfig(
            proxy_format=1,
            template="test",
            template_digest="sha256:test",
            provider="litellm",
            proxy_endpoint="http://localhost:8084",
            port=8084,
            upstream_base_url="https://litellm.test.example.com",
            tiers=TierModels(haiku="h", sonnet="s", opus="o"),
            tier_overrides=TierOverrides(
                opus=TierOverride(reasoning_effort="high", temperature=0.7),
            ),
        )

        assert config.tier_overrides.opus is not None
        assert config.tier_overrides.opus.reasoning_effort == "high"
        assert config.tier_overrides.opus.temperature == 0.7

    def test_proxy_config_with_provider_settings(self):
        """ProxyInstanceConfig supports provider_settings dict."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        config = ProxyInstanceConfig(
            proxy_format=1,
            template="test",
            template_digest="sha256:test",
            provider="litellm",
            proxy_endpoint="http://localhost:8084",
            port=8084,
            upstream_base_url="https://litellm.test.example.com",
            tiers=TierModels(haiku="h", sonnet="s", opus="o"),
            provider_settings={"openai_api_mode": "responses"},
        )

        assert config.provider_settings["openai_api_mode"] == "responses"


class TestLeaseIdValidation:
    """Tests for proxy_id path traversal prevention."""

    def test_valid_proxy_ids(self, tmp_path, monkeypatch):
        """Valid proxy IDs should be accepted."""
        from forge.config.loader import get_proxy_file_path

        monkeypatch.setenv("FORGE_HOME", str(tmp_path))

        # All these should work
        valid_ids = [
            "my-proxy",
            "proxy_123",
            "litellm-gemini",
            "test.proxy.v2",
            "A123",
            "x",
        ]
        for proxy_id in valid_ids:
            path = get_proxy_file_path(proxy_id)
            assert proxy_id in str(path)

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        """Path traversal attempts should be rejected."""
        from forge.config.loader import get_proxy_file_path

        monkeypatch.setenv("FORGE_HOME", str(tmp_path))

        invalid_ids = [
            "../escape",
            "foo/../bar",
            "/etc/passwd",
            "foo/bar",
            "..\\escape",
            "foo\\bar",
        ]
        for proxy_id in invalid_ids:
            with pytest.raises(ValueError, match=r"(Invalid proxy ID|path separator|parent reference)"):
                get_proxy_file_path(proxy_id)

    def test_empty_proxy_id_rejected(self, tmp_path, monkeypatch):
        """Empty proxy ID should be rejected."""
        from forge.config.loader import get_proxy_file_path

        monkeypatch.setenv("FORGE_HOME", str(tmp_path))

        with pytest.raises(ValueError, match="cannot be empty"):
            get_proxy_file_path("")

    def test_special_chars_rejected(self, tmp_path, monkeypatch):
        """Special characters should be rejected."""
        from forge.config.loader import get_proxy_file_path

        monkeypatch.setenv("FORGE_HOME", str(tmp_path))

        invalid_ids = [
            "foo bar",  # space
            "foo@bar",  # @
            "foo#bar",  # #
            "-start-with-dash",  # starts with dash
            ".start-with-dot",  # starts with dot
        ]
        for proxy_id in invalid_ids:
            with pytest.raises(ValueError, match="Invalid proxy ID"):
                get_proxy_file_path(proxy_id)


class TestProxyInstanceConfigValidation:
    """Tests for ProxyInstanceConfig __post_init__ validation."""

    def test_invalid_proxy_format(self):
        """Unsupported proxy_format should be rejected."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        with pytest.raises(ValueError, match="Unsupported proxy_format"):
            ProxyInstanceConfig(
                proxy_format=2,  # Invalid
                template="test",
                template_digest="sha256:test",
                provider="litellm",
                proxy_endpoint="http://localhost:8084",
                port=8084,
                upstream_base_url="https://litellm.test.example.com",
                tiers=TierModels(haiku="h", sonnet="s", opus="o"),
            )

    def test_invalid_provider(self):
        """Invalid provider should be rejected."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        with pytest.raises(ValueError, match="Invalid provider"):
            ProxyInstanceConfig(
                proxy_format=1,
                template="test",
                template_digest="sha256:test",
                provider="invalid_provider",  # Invalid
                proxy_endpoint="http://localhost:8084",
                port=8084,
                upstream_base_url="https://litellm.test.example.com",
                tiers=TierModels(haiku="h", sonnet="s", opus="o"),
            )

    def test_openrouter_provider_accepted(self):
        """OpenRouter should be a valid provider."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        config = ProxyInstanceConfig(
            proxy_format=1,
            template="openrouter",
            template_digest="sha256:test",
            provider="openrouter",
            proxy_endpoint="http://localhost:8095",
            port=8095,
            upstream_base_url="https://openrouter.ai/api/v1",
            tiers=TierModels(
                haiku="anthropic/claude-haiku-4.5",
                sonnet="anthropic/claude-sonnet-4.6",
                opus="anthropic/claude-opus-4.6",
            ),
        )
        assert config.provider == "openrouter"

    def test_invalid_port_too_low(self):
        """Port 0 should be rejected."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        with pytest.raises(ValueError, match="Invalid port"):
            ProxyInstanceConfig(
                proxy_format=1,
                template="test",
                template_digest="sha256:test",
                provider="litellm",
                proxy_endpoint="http://localhost:8084",
                port=0,  # Invalid
                upstream_base_url="https://litellm.test.example.com",
                tiers=TierModels(haiku="h", sonnet="s", opus="o"),
            )

    def test_invalid_port_too_high(self):
        """Port > 65535 should be rejected."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        with pytest.raises(ValueError, match="Invalid port"):
            ProxyInstanceConfig(
                proxy_format=1,
                template="test",
                template_digest="sha256:test",
                provider="litellm",
                proxy_endpoint="http://localhost:8084",
                port=70000,  # Invalid
                upstream_base_url="https://litellm.test.example.com",
                tiers=TierModels(haiku="h", sonnet="s", opus="o"),
            )

    def test_missing_sonnet_tier(self):
        """Tiers without sonnet should be rejected."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        with pytest.raises(ValueError, match="must define at least 'sonnet'"):
            ProxyInstanceConfig(
                proxy_format=1,
                template="test",
                template_digest="sha256:test",
                provider="litellm",
                proxy_endpoint="http://localhost:8084",
                port=8084,
                upstream_base_url="https://litellm.test.example.com",
                tiers=TierModels(haiku="h", sonnet="", opus="o"),  # Empty sonnet
            )

    def test_invalid_default_tier(self):
        """Invalid default_tier should be rejected."""
        from forge.config.schema import ProxyInstanceConfig, TierModels

        with pytest.raises(ValueError, match="Invalid default_tier"):
            ProxyInstanceConfig(
                proxy_format=1,
                template="test",
                template_digest="sha256:test",
                provider="litellm",
                proxy_endpoint="http://localhost:8084",
                port=8084,
                upstream_base_url="https://litellm.test.example.com",
                tiers=TierModels(haiku="h", sonnet="s", opus="o"),
                default_tier="invalid_tier",  # Invalid
            )
