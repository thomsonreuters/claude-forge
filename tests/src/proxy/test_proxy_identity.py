"""Tests for proxy identity discovery.

These tests verify the 2-tier proxy identity discovery logic without
importing the heavy proxy server module.
"""

from __future__ import annotations

import json
from pathlib import Path

from forge.proxy.proxy_identity import (
    DEFAULT_PROXY_PORT,
    get_proxy_identity,
)


class TestProxyIdentityFromRegistry:
    """Tests for tier 1: Registry lookup by (template, port)."""

    def test_registry_match_by_family_and_port(self, mock_registry_path: Path) -> None:
        """Registry lookup should match by (family, port)."""
        mock_registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "proxy_reg": {
                            "proxy_id": "proxy_reg",
                            "template": "litellm-openai",
                            "base_url": "http://127.0.0.1:8085",  # Different from request
                            "port": 8085,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_host="localhost",
            request_port=8085,
        )

        assert result.proxy_id == "proxy_reg"
        assert result.source == "registry"
        assert result.status == "registered"
        # base_url should be derived from request, not registry
        assert result.base_url == "http://localhost:8085"

    def test_process_proxy_id_wins_when_aliases_share_template_and_port(self, mock_registry_path: Path) -> None:
        """A running proxy should report its own id even when configured aliases share its port."""
        mock_registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "openrouter-openai": {
                            "proxy_id": "openrouter-openai",
                            "template": "openrouter-openai",
                            "base_url": "http://localhost:8096",
                            "port": 8096,
                            "status": "configured",
                        },
                        "qa-openai": {
                            "proxy_id": "qa-openai",
                            "template": "openrouter-openai",
                            "base_url": "http://localhost:8096",
                            "port": 8096,
                            "status": "healthy",
                        },
                    },
                }
            )
        )

        result = get_proxy_identity(
            active_template="openrouter-openai",
            request_host="localhost",
            request_port=8096,
            process_proxy_id="qa-openai",
        )

        assert result.proxy_id == "qa-openai"
        assert result.source == "registry"
        assert result.status == "registered"

    def test_registry_no_match_different_family(self, mock_registry_path: Path) -> None:
        """Registry lookup should not match if family differs."""
        mock_registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "proxy_1": {
                            "proxy_id": "proxy_1",
                            "template": "litellm-gemini",  # Different family
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        result = get_proxy_identity(
            active_template="litellm-openai",  # Looking for openai, not gemini
            request_port=8085,
        )

        assert result.proxy_id is None
        assert result.source == "derived"
        assert result.status == "unregistered"

    def test_registry_no_match_different_port(self, mock_registry_path: Path) -> None:
        """Registry lookup should not match if port differs."""
        mock_registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "proxy_1": {
                            "proxy_id": "proxy_1",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8086",  # Different port
                            "port": 8086,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_port=8085,  # Looking for 8085, not 8086
        )

        assert result.proxy_id is None
        assert result.source == "derived"


class TestProxyIdentityDerived:
    """Tests for tier 2: Derived values (unregistered)."""

    def test_derived_when_no_env_no_registry(self, mock_registry_path: Path) -> None:
        """Falls back to derived when no env var and no registry match."""
        # Empty registry (file doesn't exist - ProxyRegistryStore handles this)
        # mock_registry_path creates the parent dir but the file is empty by default
        # Remove the file so ProxyRegistryStore returns empty
        mock_registry_path.unlink(missing_ok=True)

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_host="myhost",
            request_port=9000,
        )

        assert result.proxy_id is None
        assert result.source == "derived"
        assert result.status == "unregistered"
        assert result.template == "litellm-openai"
        assert result.port == 9000
        assert result.base_url == "http://myhost:9000"

    def test_derived_uses_default_port(self, mock_registry_path: Path) -> None:
        """Falls back to default port when no port info available."""
        mock_registry_path.unlink(missing_ok=True)

        result = get_proxy_identity(
            active_template="litellm-openai",
            # No request_port, no env_port
        )

        assert result.port == DEFAULT_PROXY_PORT
        assert result.base_url == f"http://localhost:{DEFAULT_PROXY_PORT}"


class TestProxyIdentityErrorHandling:
    """Tests for error handling (corrupted registry, etc.)."""

    def test_corrupted_registry_returns_derived(self, mock_registry_path: Path) -> None:
        """Corrupted registry should not crash - returns derived."""
        mock_registry_path.write_text("{not-valid-json")

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_port=8085,
        )

        assert result.source == "derived"
        assert result.status == "unregistered"
        assert result.proxy_id is None

    def test_wrong_version_registry_returns_derived(self, mock_registry_path: Path) -> None:
        """Wrong version registry should not crash - returns derived."""
        mock_registry_path.write_text(json.dumps({"version": 999, "proxies": {}}))

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_port=8085,
        )

        assert result.source == "derived"
        assert result.status == "unregistered"


class TestProxyIdentityPortPriority:
    """Tests for port determination priority."""

    def test_request_port_preferred_over_env_port(self, mock_registry_path: Path) -> None:
        """request_port should be preferred over env_port."""
        mock_registry_path.unlink(missing_ok=True)

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_port=8085,
            env_port=8086,  # Should be ignored
        )

        assert result.port == 8085

    def test_env_port_used_when_no_request_port(self, mock_registry_path: Path) -> None:
        """env_port should be used when request_port is None."""
        mock_registry_path.unlink(missing_ok=True)

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_port=None,
            env_port=8086,
        )

        assert result.port == 8086

    def test_default_port_when_no_port_info(self, mock_registry_path: Path) -> None:
        """Default port should be used when no port info available."""
        mock_registry_path.unlink(missing_ok=True)

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_port=None,
            env_port=None,
        )

        assert result.port == DEFAULT_PROXY_PORT


class TestProxyIdentityBaseUrlDerivation:
    """Tests for base_url derivation."""

    def test_base_url_from_request_not_registry(self, mock_registry_path: Path) -> None:
        """base_url should be derived from request, not registry."""
        mock_registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "proxy_1": {
                            "proxy_id": "proxy_1",
                            "template": "litellm-openai",
                            "base_url": "http://127.0.0.1:8085",  # Registry says 127.0.0.1
                            "port": 8085,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_host="myhost.local",  # Request says myhost.local
            request_port=8085,
        )

        # Should use request host, not registry
        assert result.base_url == "http://myhost.local:8085"
        assert result.source == "registry"  # Still found in registry

    def test_base_url_defaults_to_localhost(self, mock_registry_path: Path) -> None:
        """base_url should default to localhost when no host provided."""
        mock_registry_path.unlink(missing_ok=True)

        result = get_proxy_identity(
            active_template="litellm-openai",
            request_host=None,
            request_port=8085,
        )

        assert result.base_url == "http://localhost:8085"
