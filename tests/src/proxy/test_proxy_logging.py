"""Tests for proxy logging gating.

Structured JSONL logs (requests + tool events) must only be written when the
Forge effective log level is "debug". Tool failure logs are opt-in via
RuntimeConfig.log_tool_failures.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forge.proxy.utils import (
    _truncate_for_log,
    _truncate_recursive,
    log_request_response,
    log_tool_event,
    log_tool_failure,
)
from forge.runtime_config import reset_runtime_config


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """Ensure each test gets a fresh RuntimeConfig singleton."""
    reset_runtime_config()
    yield
    reset_runtime_config()


@pytest.mark.asyncio
async def test_structured_proxy_logs_disabled_by_default(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default log_level=off should produce no JSONL files."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge_home"))
    monkeypatch.delenv("FORGE_DEBUG", raising=False)

    await log_request_response(
        request_id="req_1",
        original_model="claude-opus",
        mapped_model="gpt-5.2",
        request_body={"messages": []},
        response_body={"id": "resp_1"},
        status_code=200,
        duration_ms=12.3,
    )

    await log_tool_event(
        request_id="req_1",
        tool_name="bash",
        status="attempt",
        stage="openai_request",
        details={"x": 1},
    )

    assert not (tmp_path / "forge_home" / "logs" / "requests").exists()
    assert not (tmp_path / "forge_home" / "logs" / "tool_events").exists()


@pytest.mark.asyncio
async def test_structured_proxy_logs_write_in_debug(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge_home"))
    monkeypatch.setenv("FORGE_DEBUG", "1")

    await log_request_response(
        request_id="req_1",
        original_model="claude-opus",
        mapped_model="gpt-5.2",
        request_body={"messages": []},
        response_body={"id": "resp_1"},
        status_code=200,
        duration_ms=12.3,
    )

    await log_tool_event(
        request_id="req_1",
        tool_name="bash",
        status="attempt",
        stage="openai_request",
        details={"x": 1},
    )

    requests_dir = tmp_path / "forge_home" / "logs" / "requests"
    tool_events_dir = tmp_path / "forge_home" / "logs" / "tool_events"

    assert requests_dir.is_dir()
    assert tool_events_dir.is_dir()

    request_files = list(requests_dir.glob("*_requests.*.jsonl"))
    tool_files = list(tool_events_dir.glob("*_proxy.*.jsonl"))

    assert len(request_files) == 1
    assert len(tool_files) == 1

    assert '"request_id": "req_1"' in request_files[0].read_text(encoding="utf-8")
    assert '"tool_name": "bash"' in tool_files[0].read_text(encoding="utf-8")


# --- Tool failure logging (opt-in) ---


def _make_failure_kwargs() -> dict:
    return dict(
        request_id="req_fail_1",
        mapped_model="openai/gpt-5.5",
        tool_name="Read",
        tool_use_id="tu_abc123",
        tool_input={"file_path": "/workspace/foo.py", "pages": ""},
        error_content="Error: pages parameter is only valid for PDF files",
    )


def _enable_tool_failure_logging(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge_home"))
    monkeypatch.delenv("FORGE_DEBUG", raising=False)

    config_dir = tmp_path / "forge_home"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("log_tool_failures: true\n")


@pytest.mark.asyncio
async def test_tool_failure_log_disabled_by_default(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool failure log is opt-in even though it is independent of debug logging."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge_home"))
    monkeypatch.delenv("FORGE_DEBUG", raising=False)

    await log_tool_failure(**_make_failure_kwargs())

    assert not (tmp_path / "forge_home" / "logs" / "tool_failures").exists()


@pytest.mark.asyncio
async def test_tool_failure_log_writes_when_enabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool failure log writes when log_tool_failures=true."""
    _enable_tool_failure_logging(tmp_path, monkeypatch)

    await log_tool_failure(**_make_failure_kwargs())

    failures_dir = tmp_path / "forge_home" / "logs" / "tool_failures"
    assert failures_dir.is_dir()
    files = list(failures_dir.glob("*_failures.*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert '"tool": "Read"' in content
    assert '"pages": ""' in content


@pytest.mark.asyncio
async def test_tool_failure_log_disabled_by_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool failure log respects log_tool_failures=false."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge_home"))
    monkeypatch.delenv("FORGE_DEBUG", raising=False)

    config_dir = tmp_path / "forge_home"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("log_tool_failures: false\n")

    await log_tool_failure(**_make_failure_kwargs())

    assert not (tmp_path / "forge_home" / "logs" / "tool_failures").exists()


@pytest.mark.asyncio
async def test_tool_failure_log_record_fields(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify all expected fields are present in the JSONL record."""
    _enable_tool_failure_logging(tmp_path, monkeypatch)

    await log_tool_failure(**_make_failure_kwargs())

    files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").strip())

    assert set(record.keys()) == {
        "schema_version",
        "ts",
        "request_id",
        "tool_use_id",
        "model",
        "tool",
        "tool_input",
        "error",
    }
    assert record["schema_version"] == 1
    assert record["request_id"] == "req_fail_1"
    assert record["tool_use_id"] == "tu_abc123"
    assert record["model"] == "openai/gpt-5.5"
    assert record["tool"] == "Read"
    assert record["tool_input"] == {"file_path": "/workspace/foo.py", "pages": ""}
    assert "pages parameter" in record["error"]


@pytest.mark.asyncio
async def test_tool_failure_log_truncates_long_errors(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_tool_failure_logging(tmp_path, monkeypatch)

    long_error = "x" * 5000
    await log_tool_failure(
        request_id="req_trunc",
        mapped_model="openai/gpt-5.5",
        tool_name="Bash",
        tool_use_id="tu_trunc",
        tool_input={"command": "fail"},
        error_content=long_error,
    )

    files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert len(record["error"]) < 2100
    assert "5000 chars" in record["error"]


@pytest.mark.asyncio
async def test_tool_failure_log_truncates_list_error_content(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropic tool_result content can be a list of blocks; keep it bounded."""
    _enable_tool_failure_logging(tmp_path, monkeypatch)

    await log_tool_failure(
        request_id="req_list_error",
        mapped_model="openai/gpt-5.5",
        tool_name="Read",
        tool_use_id="tu_list",
        tool_input={"file_path": "/foo.py"},
        error_content=[{"type": "text", "text": "E" * 10_000}],
    )

    files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert len(json.dumps(record)) < 3000
    assert "10000 chars" in record["error"][0]["text"]


@pytest.mark.asyncio
async def test_tool_failure_log_survives_write_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write errors are swallowed (best-effort)."""
    monkeypatch.setenv("FORGE_HOME", "/nonexistent/readonly/path")
    monkeypatch.delenv("FORGE_DEBUG", raising=False)
    monkeypatch.setattr("forge.proxy.utils._should_log_tool_failures", lambda: True)

    await log_tool_failure(**_make_failure_kwargs())


def test_truncate_for_log_string() -> None:
    assert _truncate_for_log("short", 100) == "short"
    assert _truncate_for_log("x" * 200, 100) is not None
    result = _truncate_for_log("x" * 200, 100)
    assert isinstance(result, str)
    assert len(result) < 200
    assert "200 chars" in result


def test_truncate_for_log_non_string_passthrough() -> None:
    assert _truncate_for_log({"key": "val"}, 100) == {"key": "val"}
    assert _truncate_for_log(None, 100) is None
    assert _truncate_for_log(["a", "b"], 100) == ["a", "b"]


def test_truncate_recursive_caps_string_inside_dict() -> None:
    """Edit/Write tool inputs may carry KB-scale `content` fields."""
    big_content = "x" * 50_000
    result = _truncate_recursive({"file_path": "/foo.py", "content": big_content}, max_str_len=1024)
    assert isinstance(result, dict)
    assert result["file_path"] == "/foo.py"
    assert isinstance(result["content"], str)
    assert len(result["content"]) < 1100
    assert "50000 chars" in result["content"]


def test_truncate_recursive_walks_lists() -> None:
    big = "y" * 5000
    result = _truncate_recursive([{"x": big}], max_str_len=100)
    assert isinstance(result, list)
    assert len(result[0]["x"]) < 200


def test_truncate_recursive_max_depth_guard() -> None:
    """Pathological deep nesting bottoms out cleanly."""
    nested: dict = {}
    cursor = nested
    for _ in range(20):
        cursor["k"] = {}
        cursor = cursor["k"]
    cursor["k"] = "leaf"

    result = _truncate_recursive(nested, max_depth=3)
    # Walk down 3 levels — should hit the truncation marker
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_tool_failure_log_truncates_tool_input(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tool_input` with KB-scale content gets recursively truncated."""
    _enable_tool_failure_logging(tmp_path, monkeypatch)

    big_content = "X" * 10_000
    await log_tool_failure(
        request_id="req_big",
        mapped_model="openai/gpt-5.5",
        tool_name="Write",
        tool_use_id="tu_big",
        tool_input={"file_path": "/foo.py", "content": big_content},
        error_content="failed",
    )

    files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").strip())
    # The whole record line must stay reasonable, not 10KB+
    assert len(json.dumps(record)) < 3000
    assert "10000 chars" in record["tool_input"]["content"]


@pytest.mark.asyncio
async def test_tool_failure_log_file_perms_0600(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry log files are owner-only readable."""
    import stat

    _enable_tool_failure_logging(tmp_path, monkeypatch)

    await log_tool_failure(**_make_failure_kwargs())

    files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))
    mode = stat.S_IMODE(files[0].stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# --- _find_tool_use_info ---


class _FakeBlock:
    content: str
    id: str
    name: str
    input: dict
    tool_use_id: str
    is_error: bool

    def __init__(self, type: str, **kwargs):
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeMsg:
    def __init__(self, role: str, content):
        self.role = role
        self.content = content


def test_find_tool_use_info_returns_name_and_input() -> None:
    from forge.proxy.server import _find_tool_use_info

    tool_use_block = _FakeBlock("tool_use", id="tu_1", name="Read", input={"file_path": "/foo.py", "pages": ""})
    tool_result_block = _FakeBlock("tool_result", tool_use_id="tu_1", content="Error: bad pages", is_error=True)
    assistant_msg = _FakeMsg("assistant", [tool_use_block])
    user_msg = _FakeMsg("user", [tool_result_block])
    messages = [assistant_msg, user_msg]

    name, tool_input = _find_tool_use_info(messages, user_msg, "tu_1")
    assert name == "Read"
    assert tool_input == {"file_path": "/foo.py", "pages": ""}


def test_find_tool_use_info_missing_returns_none_tuple() -> None:
    from forge.proxy.server import _find_tool_use_info

    user_msg = _FakeMsg("user", [_FakeBlock("tool_result", tool_use_id="tu_99")])
    messages = [user_msg]

    name, tool_input = _find_tool_use_info(messages, user_msg, "tu_99")
    assert name is None
    assert tool_input is None


# --- _check_client_tool_failures: only-latest-message scan ---


@pytest.mark.asyncio
async def test_check_client_tool_failures_scans_only_latest_user_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old failed tool_results in earlier messages must not be re-logged."""
    from unittest.mock import MagicMock

    from forge.proxy.server import _check_client_tool_failures

    _enable_tool_failure_logging(tmp_path, monkeypatch)

    # Old assistant turn (issued tool_use_old) + old user reply (failure).
    old_use = _FakeBlock("tool_use", id="tu_old", name="Read", input={"file_path": "/a"})
    old_result = _FakeBlock("tool_result", tool_use_id="tu_old", content="Error: gone", is_error=True)
    # Newer assistant turn + new user reply.
    new_use = _FakeBlock("tool_use", id="tu_new", name="Read", input={"file_path": "/b", "pages": ""})
    new_result = _FakeBlock(
        "tool_result",
        tool_use_id="tu_new",
        content="Error: pages PDF only",
        is_error=True,
    )

    messages = [
        _FakeMsg("assistant", [old_use]),
        _FakeMsg("user", [old_result]),
        _FakeMsg("assistant", [new_use]),
        _FakeMsg("user", [new_result]),
    ]
    request_data = MagicMock(messages=messages)

    await _check_client_tool_failures(request_data, "req_test", "openai/gpt-5.5")

    # Allow the asyncio.create_task background write to complete
    import asyncio as _aio

    await _aio.sleep(0.05)

    files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))
    if not files:
        # Background tasks may not have flushed yet under heavy CI; retry once
        await _aio.sleep(0.1)
        files = list((tmp_path / "forge_home" / "logs" / "tool_failures").glob("*.jsonl"))

    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    # Only the newest tool_result should have been logged
    assert len(lines) == 1
    assert lines[0]["tool_use_id"] == "tu_new"
    assert lines[0]["tool_input"]["file_path"] == "/b"


@pytest.mark.asyncio
async def test_check_client_tool_failures_logs_raw_error_before_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry keeps the original tool error separate from forwarded hints."""
    import asyncio as _aio
    from unittest.mock import MagicMock

    from forge.proxy import server

    captured: dict = {}

    async def capture_failure(**kwargs):
        captured.update(kwargs)

    async def noop_tool_event(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "log_tool_failure", capture_failure)
    monkeypatch.setattr(server, "log_tool_event", noop_tool_event)
    monkeypatch.setattr(
        server.config,
        "proxy",
        SimpleNamespace(get_provider=lambda: SimpleNamespace(error_hints=True)),
        raising=False,
    )

    raw_error = "Error: File does not exist: /missing.py"
    tool_use = _FakeBlock("tool_use", id="tu_raw", name="Read", input={"file_path": "/missing.py"})
    tool_result = _FakeBlock("tool_result", tool_use_id="tu_raw", content=raw_error, is_error=True)
    messages = [_FakeMsg("assistant", [tool_use]), _FakeMsg("user", [tool_result])]

    await server._check_client_tool_failures(MagicMock(messages=messages), "req_raw", "openai/gpt-5.5")
    await _aio.sleep(0.05)

    assert captured["error_content"] == raw_error
    assert "HINT:" in tool_result.content
