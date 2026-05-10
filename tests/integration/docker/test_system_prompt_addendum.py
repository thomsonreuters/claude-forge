"""Docker E2E coverage for model-family system prompt addendums."""

from __future__ import annotations

import json

import pytest

from tests.fixtures.docker import ContainerLike

pytestmark = [pytest.mark.integration, pytest.mark.docker_in]


def _allocate_container_port(workspace: ContainerLike) -> int:
    result = workspace.exec(
        'python3 -c \'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); '
        "print(s.getsockname()[1]); s.close()'"
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _start_proxy_health_stub(
    workspace: ContainerLike, *, proxy_id: str, template: str, port: int
) -> None:
    """Start a minimal proxy truth endpoint for CLI health checks."""
    payload = json.dumps(
        {"is_proxy": True, "template": template, "proxy": {"proxy_id": proxy_id}}
    )
    workspace.write_file(
        "/tmp/addendum-health-stub.py",
        f"""import http.server

PAYLOAD = {payload!r}.encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
""",
    )
    result = workspace.exec(
        "nohup python3 /tmp/addendum-health-stub.py > /tmp/addendum-health-stub.log 2>&1 & "
        "echo $! > /tmp/addendum-health-stub.pid && "
        f"for i in $(seq 1 20); do curl -sf http://127.0.0.1:{port}/ >/dev/null && exit 0; sleep 0.1; done; "
        "cat /tmp/addendum-health-stub.log; exit 1",
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _write_openai_proxy(workspace: ContainerLike, *, proxy_id: str, port: int) -> None:
    template = "litellm-openai"
    base_url = f"http://127.0.0.1:{port}"

    workspace.mkdir(f"$HOME/.forge/proxies/{proxy_id}", parents=True)
    workspace.write_json(
        "$HOME/.forge/proxies/index.json",
        {
            "version": 1,
            "proxies": {
                proxy_id: {
                    "proxy_id": proxy_id,
                    "template": template,
                    "base_url": base_url,
                    "port": port,
                    "pid": None,
                    "status": "healthy",
                }
            },
        },
    )
    workspace.write_file(
        f"$HOME/.forge/proxies/{proxy_id}/proxy.yaml",
        f"""proxy_format: 1
template: {template}
template_digest: sha256:test
provider: litellm
proxy_endpoint: {base_url}
port: {port}
upstream_base_url: https://litellm.test.example.com
tiers:
  haiku: gpt-5.4-mini
  sonnet: gpt-5.5
  opus: gpt-5.5
default_tier: sonnet
""",
    )
    _start_proxy_health_stub(workspace, proxy_id=proxy_id, template=template, port=port)


def test_proxy_session_start_injects_openai_addendum(
    forge_workspace: ContainerLike,
) -> None:
    """Containerized launch path should pass the resolved addendum to Claude."""
    proxy_id = "addendum-openai-e2e"
    port = _allocate_container_port(forge_workspace)
    _write_openai_proxy(forge_workspace, proxy_id=proxy_id, port=port)

    result = forge_workspace.exec(
        f"cd /workspace && forge session start addendum-e2e --proxy {proxy_id}"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Proxy: {proxy_id}" in result.stdout

    invocations = forge_workspace.read_file("/tmp/claude_invocations.log")
    addendum_path = "/workspace/.forge/launch-context/addendum-e2e.addendum.md"
    assert f"--append-system-prompt-file {addendum_path}" in invocations

    addendum = forge_workspace.read_file(addendum_path)
    assert "# Tool Parameter Guidance" in addendum
    assert "Read" in addendum
    assert "pages" in addendum
