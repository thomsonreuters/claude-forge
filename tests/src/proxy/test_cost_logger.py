"""Tests for PID-sharded JSONL cost logger."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forge.proxy.cost_logger import (
    log_request_cost,
    read_cost_logs,
)


@pytest.fixture
def cost_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point cost logger to a temp directory."""
    costs_dir = tmp_path / "costs" / "requests"
    monkeypatch.setattr("forge.proxy.cost_logger._costs_dir", lambda: costs_dir)
    return costs_dir


class TestLogRequestCost:
    def test_creates_dir_and_writes_record(self, cost_log_dir: Path):
        log_request_cost(
            proxy_id="openrouter",
            model="anthropic/claude-sonnet-4.6",
            tier="sonnet",
            input_tokens=1500,
            output_tokens=800,
            cached_tokens=500,
            cost_micros=16500,
            latency_ms=1200.5,
            failed=False,
            request_id="req_abc123",
        )

        assert cost_log_dir.is_dir()
        files = list(cost_log_dir.glob("*.jsonl"))
        assert len(files) == 1

        with open(files[0]) as f:
            lines = f.readlines()
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["proxy_id"] == "openrouter"
        assert record["model"] == "anthropic/claude-sonnet-4.6"
        assert record["tier"] == "sonnet"
        assert record["input_tokens"] == 1500
        assert record["output_tokens"] == 800
        assert record["cached_tokens"] == 500
        assert record["cost_micros"] == 16500
        assert record["estimated"] is True
        assert record["pricing_source"] == "catalog"
        assert record["failed"] is False
        assert record["request_id"] == "req_abc123"
        assert record["ts"].endswith("Z")

    def test_appends_multiple_records(self, cost_log_dir: Path):
        for i in range(3):
            log_request_cost(
                proxy_id="test",
                model="test-model",
                tier="sonnet",
                input_tokens=100 * (i + 1),
                output_tokens=50,
                cached_tokens=0,
                cost_micros=1000 * (i + 1),
                latency_ms=100.0,
                failed=False,
                request_id=f"req_{i}",
            )

        files = list(cost_log_dir.glob("*.jsonl"))
        assert len(files) == 1
        with open(files[0]) as f:
            lines = f.readlines()
        assert len(lines) == 3

    def test_failed_request_logged(self, cost_log_dir: Path):
        log_request_cost(
            proxy_id="test",
            model="test-model",
            tier="opus",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cost_micros=0,
            latency_ms=50.0,
            failed=True,
            request_id="req_fail",
        )

        records = read_cost_logs()
        assert len(records) == 1
        assert records[0]["failed"] is True


class TestRoundtrip:
    """Write with log_request_cost, read back with period filtering."""

    def test_write_then_read_with_period(self, cost_log_dir: Path):
        """Records written by the logger are findable by period-filtered reads."""
        now = datetime.now(timezone.utc)

        log_request_cost(
            proxy_id="test",
            model="claude-sonnet-4-6",
            tier="sonnet",
            input_tokens=1000,
            output_tokens=500,
            cached_tokens=200,
            cost_micros=5000,
            latency_ms=100.0,
            failed=False,
            request_id="req_roundtrip",
        )

        one_minute_ago = now - timedelta(minutes=1)
        one_minute_later = now + timedelta(minutes=1)
        records = read_cost_logs(period_start=one_minute_ago, period_end=one_minute_later)
        assert len(records) == 1
        assert records[0]["request_id"] == "req_roundtrip"
        assert records[0]["cost_micros"] == 5000

    def test_timestamp_format_is_valid_utc(self, cost_log_dir: Path):
        """Written timestamps parse correctly and don't have double suffixes."""
        log_request_cost(
            proxy_id="test", model="m", tier="t",
            input_tokens=0, output_tokens=0, cached_tokens=0,
            cost_micros=0, latency_ms=0, failed=False, request_id="req_ts",
        )

        records = read_cost_logs()
        assert len(records) == 1
        ts_str = records[0]["ts"]
        assert ts_str.endswith("Z")
        assert "+00:00" not in ts_str
        parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None


class TestReadCostLogs:
    def test_empty_dir_returns_empty(self, cost_log_dir: Path):
        assert read_cost_logs() == []

    def test_reads_all_shards(self, cost_log_dir: Path):
        """Simulate multiple PID shards."""
        cost_log_dir.mkdir(parents=True, exist_ok=True)

        for pid in [1234, 5678]:
            path = cost_log_dir / f"2026-05_{pid}.jsonl"
            with open(path, "w") as f:
                record = {"ts": "2026-05-07T10:00:00Z", "cost_micros": pid, "model": "m"}
                f.write(json.dumps(record) + "\n")

        records = read_cost_logs()
        assert len(records) == 2

    def test_filters_by_period(self, cost_log_dir: Path):
        cost_log_dir.mkdir(parents=True, exist_ok=True)
        path = cost_log_dir / "2026-05_9999.jsonl"
        with open(path, "w") as f:
            for hour in [8, 12, 16]:
                record = {"ts": f"2026-05-07T{hour:02d}:00:00Z", "cost_micros": 100}
                f.write(json.dumps(record) + "\n")

        start = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 7, 14, 0, 0, tzinfo=timezone.utc)
        records = read_cost_logs(period_start=start, period_end=end)
        assert len(records) == 1
        assert "T12:00:00" in records[0]["ts"]

    def test_skips_malformed_lines(self, cost_log_dir: Path):
        cost_log_dir.mkdir(parents=True, exist_ok=True)
        path = cost_log_dir / "2026-05_9999.jsonl"
        with open(path, "w") as f:
            f.write("not json\n")
            f.write(json.dumps({"ts": "2026-05-07T10:00:00Z", "ok": True}) + "\n")
            f.write("\n")

        records = read_cost_logs()
        assert len(records) == 1
        assert records[0]["ok"] is True
