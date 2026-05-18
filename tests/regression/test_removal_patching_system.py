"""Regression: patching system removed for OSS release.

Forge now uses the native Claude Code env var CLAUDE_CODE_AUTO_COMPACT_WINDOW
in proxy mode only. This test documents the new contract.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from forge.cli.main import main

pytestmark = pytest.mark.regression

# Build the legacy env var name dynamically so it doesn't appear as a
# literal in grep-based removal verification.
_LEGACY_ENV_VAR = "FORGE" + "_" + "CONTEXT" + "_" + "LIMIT"


class TestPatchingRemoval:
    """Verify the patching system is fully removed."""

    def test_no_patch_subcommand(self):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "patch"])
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_no_update_subcommand(self):
        runner = CliRunner()
        result = runner.invoke(main, ["claude", "update"])
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_no_patch_flag_on_enable(self):
        runner = CliRunner()
        result = runner.invoke(main, ["extension", "enable", "--patch"])
        assert result.exit_code != 0
        assert "No such option" in result.output


class TestAutoCompactWindowContract:
    """Verify CLAUDE_CODE_AUTO_COMPACT_WINDOW is set correctly."""

    def test_proxy_launch_sets_auto_compact_window(self):
        """Proxy mode sets CLAUDE_CODE_AUTO_COMPACT_WINDOW to model's context window."""
        from forge.cli.session import _build_session_env

        env_vars, unset = _build_session_env(
            session_name="test",
            base_url="http://localhost:8085",
            template="litellm-openai",
            context_limit=400000,
        )
        assert env_vars["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
        assert _LEGACY_ENV_VAR not in env_vars

    def test_direct_launch_does_not_touch_auto_compact_window(self):
        """Direct mode neither sets nor unsets CLAUDE_CODE_AUTO_COMPACT_WINDOW."""
        from forge.cli.session import _build_session_env

        env_vars, unset = _build_session_env(
            session_name="test",
            base_url=None,
            template=None,
            context_limit=200000,
        )
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env_vars
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in unset
        assert _LEGACY_ENV_VAR not in env_vars
        assert _LEGACY_ENV_VAR not in unset

    def test_resolver_derives_context_from_proxy_model(self, tmp_path, monkeypatch):
        """_resolve_context_limit returns catalog-derived window, not the fallback."""
        from forge.cli.session import _resolve_context_limit
        from forge.config.loader import write_proxy_instance_config
        from forge.config.schema import ProxyInstanceConfig, TierModels
        from forge.core.models import get_context_window_tokens
        from forge.proxy.proxies import ProxyEntry, ProxyRegistry, ProxyRegistryStore
        from forge.runtime_config import reset_runtime_config

        monkeypatch.setenv("FORGE_HOME", str(tmp_path))
        reset_runtime_config()

        proxy_config = ProxyInstanceConfig(
            proxy_format=1,
            template="litellm-openai",
            template_digest="sha256:test",
            provider="litellm",
            proxy_endpoint="http://localhost:9999",
            port=9999,
            upstream_base_url="http://litellm.example.com",
            tiers=TierModels(sonnet="gpt-4o"),
            default_tier="sonnet",
        )
        write_proxy_instance_config("test-proxy", proxy_config)

        registry = ProxyRegistry(
            version=1,
            proxies={
                "test-proxy": ProxyEntry(
                    proxy_id="test-proxy",
                    base_url="http://localhost:9999",
                    port=9999,
                    template="litellm-openai",
                )
            },
        )
        store = ProxyRegistryStore()
        store.write(registry)

        expected = get_context_window_tokens("gpt-4o")
        result = _resolve_context_limit("test-proxy")
        assert result == expected
        assert result != 200000


class TestStaleManifestGuard:
    """Verify old manifests produce clean CLI errors, not tracebacks."""

    @pytest.fixture()
    def stale_manifest(self, tmp_path, monkeypatch):
        """Write a pre-OSS manifest with patched_files."""
        monkeypatch.setenv("FORGE_HOME", str(tmp_path))
        tracking_path = tmp_path / "installed.json"
        tracking_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "installations": {
                        "user": {
                            "scope": "user",
                            "mode": "copy",
                            "profile": "standard",
                            "modules_enabled": [],
                            "files": [],
                            "settings_entries": [],
                            "patched_files": [],
                        }
                    },
                }
            )
        )
        return tracking_path

    def test_forge_info_no_traceback(self, stale_manifest):
        """forge info handles stale manifest without traceback."""
        runner = CliRunner()
        result = runner.invoke(main, ["info"])
        assert "Traceback" not in result.output
        assert "pre-OSS patching build" in result.output

    def test_extension_status_no_traceback(self, stale_manifest):
        """forge extension status handles stale manifest without traceback."""
        runner = CliRunner()
        result = runner.invoke(main, ["extension", "status", "--scope", "user"])
        assert "Traceback" not in result.output
        assert result.exit_code != 0
        assert "pre-OSS" in result.output
