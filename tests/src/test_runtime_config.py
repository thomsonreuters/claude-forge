"""Tests for forge.runtime_config module.

Covers: RuntimeConfig dataclass, load_runtime_config(), get_runtime_config()
singleton, write_runtime_config(), and get_default_config_content().

Note: the autouse `isolate_forge_home` fixture (tests/conftest.py) already
sets FORGE_HOME to tmp_path/forge_home for every test. Tests that need the
path use `get_forge_home()` directly rather than the `forge_home` fixture
(which would clash with the autouse fixture's mkdir).
"""

from __future__ import annotations

import logging
from dataclasses import fields
from pathlib import Path

import pytest

from forge.core.paths import get_forge_home
from forge.runtime_config import (
    RuntimeConfig,
    get_default_config_content,
    get_runtime_config,
    load_runtime_config,
    reset_runtime_config,
    write_runtime_config,
)

# ---------------------------------------------------------------------------
# RuntimeConfig dataclass
# ---------------------------------------------------------------------------


class TestRuntimeConfigDefaults:
    def test_default_proxy_mode_is_host(self):
        rc = RuntimeConfig()
        assert rc.proxy_mode == "host"

    def test_default_sidecar_image(self):
        rc = RuntimeConfig()
        assert rc.sidecar_image == "forge-sidecar:latest"

    def test_context_limit(self):
        rc = RuntimeConfig()
        assert rc.context_limit == 200000

    def test_default_direct_model_is_opt_in(self):
        rc = RuntimeConfig()
        assert rc.default_direct_model == ""

    def test_default_status_timeout(self):
        rc = RuntimeConfig()
        assert rc.status_timeout == 2.0

    def test_default_handoff_timeout(self):
        rc = RuntimeConfig()
        assert rc.handoff_timeout == 300

    def test_default_user_agent_version_empty(self):
        rc = RuntimeConfig()
        assert rc.user_agent_claude_code_version == ""

    def test_tool_failure_logging_is_opt_in(self):
        rc = RuntimeConfig()
        assert rc.log_tool_failures is False


class TestRuntimeConfigValidation:
    def test_invalid_proxy_mode_rejected(self):
        with pytest.raises(ValueError, match="Invalid proxy_mode"):
            RuntimeConfig(proxy_mode="invalid")

    def test_sidecar_proxy_mode_accepted(self):
        rc = RuntimeConfig(proxy_mode="sidecar")
        assert rc.proxy_mode == "sidecar"

    def test_host_proxy_mode_accepted(self):
        rc = RuntimeConfig(proxy_mode="host")
        assert rc.proxy_mode == "host"

    def test_zero_context_limit_rejected(self):
        with pytest.raises(ValueError, match="context_limit must be >= 1"):
            RuntimeConfig(context_limit=0)

    def test_negative_context_limit_rejected(self):
        with pytest.raises(ValueError, match="context_limit must be >= 1"):
            RuntimeConfig(context_limit=-100)

    def test_zero_status_timeout_rejected(self):
        with pytest.raises(ValueError, match="status_timeout must be > 0"):
            RuntimeConfig(status_timeout=0)

    def test_negative_status_timeout_rejected(self):
        with pytest.raises(ValueError, match="status_timeout must be > 0"):
            RuntimeConfig(status_timeout=-1.0)

    def test_zero_handoff_timeout_rejected(self):
        with pytest.raises(ValueError, match="handoff_timeout must be >= 1"):
            RuntimeConfig(handoff_timeout=0)

    def test_negative_log_retention_days_rejected(self):
        with pytest.raises(ValueError, match="log_retention_days must be >= 0"):
            RuntimeConfig(log_retention_days=-1)

    def test_zero_log_retention_days_accepted(self):
        rc = RuntimeConfig(log_retention_days=0)
        assert rc.log_retention_days == 0

    def test_positive_log_retention_days_accepted(self):
        rc = RuntimeConfig(log_retention_days=30)
        assert rc.log_retention_days == 30

    def test_negative_session_retention_days_rejected(self):
        with pytest.raises(ValueError, match="session_retention_days must be >= 0"):
            RuntimeConfig(session_retention_days=-1)

    def test_zero_session_retention_days_accepted(self):
        rc = RuntimeConfig(session_retention_days=0)
        assert rc.session_retention_days == 0

    def test_positive_session_retention_days_accepted(self):
        rc = RuntimeConfig(session_retention_days=90)
        assert rc.session_retention_days == 90

    def test_custom_values_accepted(self):
        rc = RuntimeConfig(
            proxy_mode="sidecar",
            sidecar_image="custom:v2",
            context_limit=1000000,
            status_timeout=0.5,
            handoff_timeout=60,
        )
        assert rc.proxy_mode == "sidecar"
        assert rc.sidecar_image == "custom:v2"
        assert rc.context_limit == 1000000
        assert rc.status_timeout == 0.5
        assert rc.handoff_timeout == 60


# ---------------------------------------------------------------------------
# load_runtime_config()
# ---------------------------------------------------------------------------


class TestLoadRuntimeConfig:
    def test_missing_file_returns_defaults(self, tmp_path: Path):
        rc = load_runtime_config(tmp_path / "nonexistent.yaml")
        assert rc.proxy_mode == "host"
        assert rc.context_limit == 200000

    def test_empty_file_returns_defaults(self, tmp_path: Path):
        """Empty YAML file (yaml.safe_load returns None)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "host"

    def test_non_mapping_yaml_returns_defaults(self, tmp_path: Path):
        """YAML that parses to a list, not a dict."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- item1\n- item2\n")
        rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "host"

    def test_valid_yaml_parsed(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("proxy_mode: sidecar\nstatus_timeout: 0.5\n")
        rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "sidecar"
        assert rc.status_timeout == 0.5

    def test_log_tool_failures_yaml_parsed(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_tool_failures: true\n")
        rc = load_runtime_config(config_file)
        assert rc.log_tool_failures is True

    def test_partial_yaml_uses_defaults_for_missing(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("proxy_mode: sidecar\n")
        rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "sidecar"
        assert rc.context_limit == 200000  # Default preserved
        assert rc.status_timeout == 2.0  # Default preserved

    def test_default_direct_model_yaml_roundtrip(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text('default_direct_model: "claude-sonnet-4-6"\n')
        rc = load_runtime_config(config_file)
        assert rc.default_direct_model == "claude-sonnet-4-6"

    def test_unknown_keys_warned_and_ignored(self, tmp_path: Path, caplog):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "proxy_mode: host\nfuture_setting: true\nanother_key: 42\n"
        )
        with caplog.at_level(logging.WARNING):
            rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "host"
        assert "Unknown keys" in caplog.text
        assert "another_key" in caplog.text
        assert "future_setting" in caplog.text

    def test_invalid_value_falls_back_to_defaults(self, tmp_path: Path, caplog):
        """Invalid proxy_mode triggers validation error → fall back to defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("proxy_mode: invalid_mode\n")
        with caplog.at_level(logging.WARNING):
            rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "host"  # Fell back to default
        assert "Invalid config" in caplog.text

    def test_invalid_yaml_syntax_returns_defaults(self, tmp_path: Path, caplog):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("proxy_mode: [\n")  # Broken YAML
        with caplog.at_level(logging.WARNING):
            rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "host"
        assert "Failed to read" in caplog.text

    def test_unreadable_file_returns_defaults(self, tmp_path: Path, caplog):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("proxy_mode: sidecar\n")
        config_file.chmod(0o000)
        with caplog.at_level(logging.WARNING):
            rc = load_runtime_config(config_file)
        assert rc.proxy_mode == "host"
        # Restore permissions for cleanup
        config_file.chmod(0o644)

    def test_integer_and_float_types_preserved(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "context_limit: 1000000\nstatus_timeout: 0.25\nhandoff_timeout: 60\n"
        )
        rc = load_runtime_config(config_file)
        assert rc.context_limit == 1000000
        assert rc.status_timeout == 0.25
        assert rc.handoff_timeout == 60


# ---------------------------------------------------------------------------
# get_runtime_config() singleton
# ---------------------------------------------------------------------------


class TestGetRuntimeConfig:
    def setup_method(self):
        reset_runtime_config()

    def teardown_method(self):
        reset_runtime_config()

    def test_returns_runtime_config(self):
        rc = get_runtime_config()
        assert isinstance(rc, RuntimeConfig)

    def test_singleton_is_cached(self):
        rc1 = get_runtime_config()
        rc2 = get_runtime_config()
        assert rc1 is rc2

    def test_reset_clears_cache(self):
        rc1 = get_runtime_config()
        reset_runtime_config()
        rc2 = get_runtime_config()
        assert rc1 is not rc2

    def test_loads_from_forge_home(self):
        """Singleton reads from FORGE_HOME/config.yaml."""
        home = get_forge_home()
        config_file = home / "config.yaml"
        config_file.write_text("proxy_mode: sidecar\n")

        reset_runtime_config()
        rc = get_runtime_config()
        assert rc.proxy_mode == "sidecar"


# ---------------------------------------------------------------------------
# write_runtime_config()
# ---------------------------------------------------------------------------


class TestWriteRuntimeConfig:
    def test_writes_yaml_file(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        write_runtime_config({"proxy_mode": "sidecar"}, path=config_path)
        assert config_path.exists()
        content = config_path.read_text()
        assert "proxy_mode: sidecar" in content

    def test_creates_parent_directories(self, tmp_path: Path):
        config_path = tmp_path / "subdir" / "config.yaml"
        write_runtime_config({"proxy_mode": "host"}, path=config_path)
        assert config_path.exists()

    def test_atomic_write_no_partial_on_error(self, tmp_path: Path):
        """If write fails after temp file created, original file is untouched."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("original content")

        # Make os.replace fail so the atomic swap doesn't complete
        from unittest.mock import patch

        with patch(
            "forge.runtime_config.os.replace", side_effect=OSError("mock replace")
        ):
            with pytest.raises(OSError, match="mock replace"):
                write_runtime_config({"proxy_mode": "sidecar"}, path=config_path)

        # Original file untouched
        assert config_path.read_text() == "original content"
        # No temp file left behind
        temps = list(tmp_path.glob(".*config*.tmp"))
        assert temps == []

    def test_invalidates_singleton_cache(self):
        home = get_forge_home()
        reset_runtime_config()

        rc1 = get_runtime_config()
        assert rc1.proxy_mode == "host"

        write_runtime_config(
            {"proxy_mode": "sidecar"},
            path=home / "config.yaml",
        )

        # Cache was invalidated by write
        rc2 = get_runtime_config()
        assert rc2.proxy_mode == "sidecar"
        assert rc1 is not rc2

    def test_roundtrip_preserves_values(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        data = {
            "proxy_mode": "sidecar",
            "sidecar_image": "custom:v3",
            "context_limit": 500000,
            "status_timeout": 1.5,
        }
        write_runtime_config(data, path=config_path)
        rc = load_runtime_config(config_path)
        assert rc.proxy_mode == "sidecar"
        assert rc.sidecar_image == "custom:v3"
        assert rc.context_limit == 500000
        assert rc.status_timeout == 1.5


# ---------------------------------------------------------------------------
# get_default_config_content()
# ---------------------------------------------------------------------------


class TestGetDefaultConfigContent:
    def test_returns_string(self):
        content = get_default_config_content()
        assert isinstance(content, str)

    def test_contains_proxy_mode(self):
        content = get_default_config_content()
        assert "proxy_mode: host" in content

    def test_parseable_as_yaml(self):
        import yaml

        content = get_default_config_content()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert data["proxy_mode"] == "host"

    def test_contains_all_documented_keys(self):
        content = get_default_config_content()
        for key in [
            "proxy_mode",
            "sidecar_image",
            "user_agent_claude_code_version",
            "default_direct_model",
            "context_limit",
            "status_timeout",
            "handoff_timeout",
            "log_tool_failures",
        ]:
            assert key in content, f"Missing key in default content: {key}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestEnvVarOverrides:
    """Test three-layer resolution: defaults -> YAML -> env vars."""

    def setup_method(self):
        reset_runtime_config()

    def teardown_method(self):
        reset_runtime_config()

    def test_forge_debug_1_sets_log_level_debug(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("FORGE_DEBUG", "1")
        rc = load_runtime_config(tmp_path / "nonexistent.yaml")
        assert rc.log_level == "debug"

    def test_forge_debug_0_sets_log_level_off(self, monkeypatch, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text('log_level: "debug"\n')
        monkeypatch.setenv("FORGE_DEBUG", "0")
        rc = load_runtime_config(config_file)
        assert rc.log_level == "off"

    def test_forge_debug_overrides_yaml(self, monkeypatch, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text('log_level: "info"\n')
        monkeypatch.setenv("FORGE_DEBUG", "1")
        rc = load_runtime_config(config_file)
        assert rc.log_level == "debug"

    def test_env_sources_tracked(self, monkeypatch, tmp_path: Path):
        """Verify _env_sources dict is attached for %config annotations."""
        monkeypatch.setenv("FORGE_DEBUG", "1")
        rc = load_runtime_config(tmp_path / "nonexistent.yaml")
        env_sources = getattr(rc, "_env_sources", {})
        assert env_sources == {
            "log_level": "FORGE_DEBUG",
        }

    def test_env_sources_empty_when_no_overrides(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("FORGE_DEBUG", raising=False)
        rc = load_runtime_config(tmp_path / "nonexistent.yaml")
        env_sources = getattr(rc, "_env_sources", {})
        assert env_sources == {}

    def test_forge_debug_passthrough_info(self, monkeypatch, tmp_path: Path):
        """FORGE_DEBUG=info passes through as log_level=info."""
        monkeypatch.setenv("FORGE_DEBUG", "info")
        rc = load_runtime_config(tmp_path / "nonexistent.yaml")
        assert rc.log_level == "info"

    def test_forge_debug_passthrough_warning(self, monkeypatch, tmp_path: Path):
        """FORGE_DEBUG=warning passes through as log_level=warning."""
        monkeypatch.setenv("FORGE_DEBUG", "warning")
        rc = load_runtime_config(tmp_path / "nonexistent.yaml")
        assert rc.log_level == "warning"

    def test_env_overrides_mapping_targets_valid_fields(self):
        """Invariant: every _ENV_OVERRIDES target must be a real RuntimeConfig field."""
        from forge.runtime_config import _ENV_OVERRIDES

        valid_fields = {f.name for f in fields(RuntimeConfig)}
        for env_var, field_name in _ENV_OVERRIDES.items():
            assert field_name in valid_fields, (
                f"_ENV_OVERRIDES[{env_var!r}] targets {field_name!r} "
                f"which is not a RuntimeConfig field"
            )
