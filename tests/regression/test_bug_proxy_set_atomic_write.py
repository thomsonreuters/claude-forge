"""Regression: forge proxy set must use atomic writes and warn on cost config changes.

Bug 1 (M-new1): proxy set used a fixed temp path (proxy.yaml.tmp) which could collide
between concurrent calls and had a brief world-readable window before chmod.

Bug 2 (H3): changing costs.* config had no warning that the proxy must be restarted
for the change to take effect (cost config is read at startup, not hot-reloaded).

Fix: Use atomic_write_text() (tempfile.mkstemp + os.replace). Print restart tip for costs.* keys.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from forge.cli.main import main

pytestmark = pytest.mark.regression

_PROXY_YAML = """\
proxy_format: 1
template: litellm-openai
template_digest: abc123
provider: litellm
proxy_endpoint: http://localhost:8085
port: 8085
upstream_base_url: https://litellm.test.example.com
default_tier: sonnet
tiers:
  haiku: gpt-4o-mini
  sonnet: gpt-4o
  opus: gpt-5
"""


def _create_proxy(proxy_id: str) -> Path:
    forge_home = Path(os.environ["FORGE_HOME"])
    proxy_dir = forge_home / "proxies" / proxy_id
    proxy_dir.mkdir(parents=True, exist_ok=True)
    proxy_file = proxy_dir / "proxy.yaml"
    proxy_file.write_text(_PROXY_YAML)
    return proxy_file


def test_bug_proxy_set_no_fixed_tmp_file() -> None:
    """After proxy set, no proxy.yaml.tmp file should remain."""
    runner = CliRunner()
    proxy_file = _create_proxy("atomic-test")

    result = runner.invoke(main, ["proxy", "set", "atomic-test", "default_tier=opus"])
    assert result.exit_code == 0

    tmp_file = proxy_file.with_suffix(".yaml.tmp")
    assert not tmp_file.exists(), f"Fixed tmp file should not exist: {tmp_file}"


def test_bug_proxy_set_file_permissions() -> None:
    """Proxy file should have restrictive permissions after set."""
    runner = CliRunner()
    proxy_file = _create_proxy("perms-test")

    result = runner.invoke(main, ["proxy", "set", "perms-test", "default_tier=opus"])
    assert result.exit_code == 0

    mode = proxy_file.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_bug_set_cost_field_shows_restart_tip() -> None:
    """Setting a costs.* field should show a restart tip."""
    runner = CliRunner()
    _create_proxy("cost-tip-test")

    result = runner.invoke(main, ["proxy", "set", "cost-tip-test", "costs.caps.per_day=10"])
    assert result.exit_code == 0
    assert "Restart" in result.output or "restart" in result.output


def test_bug_set_non_cost_field_no_restart_tip() -> None:
    """Setting a non-costs field should not mention restart."""
    runner = CliRunner()
    _create_proxy("no-tip-test")

    result = runner.invoke(main, ["proxy", "set", "no-tip-test", "default_tier=opus"])
    assert result.exit_code == 0
    assert "restart" not in result.output.lower()
