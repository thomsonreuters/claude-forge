"""Regression: proxy server must default to localhost, not 0.0.0.0.

Bug: python -m forge.proxy.server --template ... exposed API-credit access
on the LAN by defaulting --host to 0.0.0.0.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.regression


def test_bug_default_host_is_localhost() -> None:
    from forge.config.schema import ProxyConfig

    assert ProxyConfig().host == "127.0.0.1"


def test_bug_server_command_default_host_is_localhost() -> None:
    from forge.proxy.server import main

    host_param = next(param for param in main.params if param.name == "host")
    assert host_param.default == "127.0.0.1"
