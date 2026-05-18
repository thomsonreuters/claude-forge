"""Regression test: walkthrough sandbox must not adopt a foreign proxy on 8085.

Bug: `forge proxy create litellm-openai` would adopt any healthy proxy on the
template's default port, even if that proxy already belonged to another
Forge home and reported a different `proxy_id`. The walkthrough sandbox then
stored a fake local alias and `forge claude launch --proxy <alias>` failed
later with a proxy_id mismatch.

Fix: only adopt healthy proxies that are currently unregistered. If the
default port is occupied by another Forge-managed proxy, skip adoption and
spawn on the next free port instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.proxy.proxy_orchestrator import start_proxy

pytestmark = pytest.mark.regression


class _Proc:
    """Fake process object for spawn-path regression coverage."""

    returncode = None

    def __init__(self, pid: int = 4242):
        self.pid = pid

    def poll(self):
        return None


def test_create_skips_foreign_registered_proxy_and_spawns_next_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign registered proxy on the default port must not be adopted."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge"))
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.test.example.com")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")

    import forge.proxy.proxy_orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "_validate_template_exists", lambda _: None)
    monkeypatch.setattr(orchestrator, "_get_template_default_port", lambda _: 8085)
    monkeypatch.setattr(orchestrator, "_is_port_in_use", lambda port: port == 8085)
    monkeypatch.setattr(orchestrator, "_find_available_port", lambda **_: 8086)
    monkeypatch.setattr(orchestrator, "_new_proxy_id", lambda existing=None: "walkthrough-local")
    monkeypatch.setattr(orchestrator, "now_iso", lambda: "2026-03-20T12:00:00+00:00")
    monkeypatch.setattr(
        orchestrator,
        "_spawn_proxy_process",
        lambda **_: (_Proc(pid=9999), tmp_path / "stderr.log"),
    )

    def _health(
        *,
        base_url: str,
        expected_template: str,
        timeout_s: float,
        expected_proxy_id: str | None = None,
        require_unregistered: bool = False,
    ) -> bool:
        assert expected_template == "litellm-openai"

        if base_url == "http://localhost:8085":
            # Simulate a healthy proxy already owned by another Forge home.
            return not require_unregistered

        if base_url == "http://localhost:8086":
            return expected_proxy_id == "walkthrough-local"

        return False

    monkeypatch.setattr(orchestrator, "check_proxy_health", _health)

    result = start_proxy(template="litellm-openai")

    assert result.source == "spawn"
    assert result.proxy.proxy_id == "walkthrough-local"
    assert result.proxy.port == 8086
    assert result.proxy.base_url == "http://localhost:8086"
