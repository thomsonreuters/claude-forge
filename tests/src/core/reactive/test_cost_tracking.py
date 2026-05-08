"""Tests for verb-level cost tracking via proxy metric snapshot deltas."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.core.reactive.cost_tracking import (
    ProxyCostDelta,
    VerbCostResult,
    _compute_delta,
    _log_verb_cost,
    read_verb_logs,
    resolve_proxy_urls,
    track_verb_cost,
)


@pytest.fixture
def verb_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point verb logger to a temp directory."""
    log_dir = tmp_path / "costs" / "verbs"
    monkeypatch.setattr("forge.core.reactive.cost_tracking._verb_log_dir", lambda: log_dir)
    return log_dir


class TestComputeDelta:
    def test_basic_delta(self):
        before = {
            "total_requests": 10,
            "tokens": {"input": 5000, "output": 2000, "cached": 1000},
            "costs": {"total_micros": 100_000},
        }
        after = {
            "total_requests": 13,
            "tokens": {"input": 8000, "output": 3500, "cached": 1200},
            "costs": {"total_micros": 250_000},
        }
        delta = _compute_delta(before, after, "http://localhost:8084")
        assert delta.request_count == 3
        assert delta.input_tokens == 3000
        assert delta.output_tokens == 1500
        assert delta.cached_tokens == 200
        assert delta.cost_micros == 150_000
        assert delta.base_url == "http://localhost:8084"

    def test_zero_delta(self):
        snap = {
            "total_requests": 5,
            "tokens": {"input": 1000, "output": 500, "cached": 100},
            "costs": {"total_micros": 50_000},
        }
        delta = _compute_delta(snap, snap, "http://localhost:8084")
        assert delta.cost_micros == 0
        assert delta.request_count == 0


class TestLogVerbCost:
    def test_writes_jsonl_record(self, verb_log_dir: Path):
        result = VerbCostResult(
            verb="panel",
            total_cost_micros=120_000,
            input_tokens=5000,
            output_tokens=2000,
            cached_tokens=500,
            request_count=4,
            duration_ms=30000.0,
            per_proxy=[
                ProxyCostDelta(
                    base_url="http://localhost:8084",
                    cost_micros=80_000,
                    input_tokens=3000,
                    output_tokens=1200,
                    request_count=2,
                ),
            ],
        )
        _log_verb_cost(result)

        files = list(verb_log_dir.glob("*.jsonl"))
        assert len(files) == 1

        with open(files[0]) as f:
            record = json.loads(f.readline())

        assert record["verb"] == "panel"
        assert record["total_cost_micros"] == 120_000
        assert record["estimated"] is True
        assert record["request_count"] == 4
        assert len(record["per_proxy"]) == 1
        assert record["per_proxy"][0]["base_url"] == "http://localhost:8084"


class TestReadVerbLogs:
    def test_empty_returns_empty(self, verb_log_dir: Path):
        assert read_verb_logs() == []

    def test_reads_multiple_shards(self, verb_log_dir: Path):
        verb_log_dir.mkdir(parents=True, exist_ok=True)
        for pid in [111, 222]:
            path = verb_log_dir / f"2026-05_{pid}.jsonl"
            with open(path, "w") as f:
                f.write(json.dumps({"ts": "2026-05-07T10:00:00Z", "verb": f"v{pid}"}) + "\n")

        records = read_verb_logs()
        assert len(records) == 2

    def test_period_filtering(self, verb_log_dir: Path):
        verb_log_dir.mkdir(parents=True, exist_ok=True)
        path = verb_log_dir / "2026-05_999.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps({"ts": "2026-05-07T08:00:00Z", "verb": "early"}) + "\n")
            f.write(json.dumps({"ts": "2026-05-07T14:00:00Z", "verb": "mid"}) + "\n")
            f.write(json.dumps({"ts": "2026-05-07T20:00:00Z", "verb": "late"}) + "\n")

        start = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 7, 18, 0, 0, tzinfo=timezone.utc)
        records = read_verb_logs(period_start=start, period_end=end)
        assert len(records) == 1
        assert records[0]["verb"] == "mid"


class TestTrackVerbCost:
    def test_no_urls_is_noop(self, verb_log_dir: Path):
        """Empty proxy URL list should not crash or write logs."""
        with track_verb_cost("test", []):
            pass
        assert not verb_log_dir.exists() or not list(verb_log_dir.glob("*.jsonl"))

    def test_unreachable_proxy_is_noop(self, verb_log_dir: Path):
        """Unreachable proxy URL should not crash."""
        with track_verb_cost("test", ["http://localhost:99999"]):
            pass

    def test_snapshot_delta_logged(self, verb_log_dir: Path):
        """With mocked snapshots, verify the delta is computed and logged."""
        before_snap = {
            "is_proxy": True,
            "metrics": {
                "total_requests": 5,
                "tokens": {"input": 1000, "output": 500, "cached": 100},
                "costs": {"total_micros": 50_000},
            },
        }
        after_snap = {
            "is_proxy": True,
            "metrics": {
                "total_requests": 8,
                "tokens": {"input": 4000, "output": 2000, "cached": 300},
                "costs": {"total_micros": 200_000},
            },
        }
        call_count = 0

        def mock_urlopen(url, timeout=None):
            nonlocal call_count
            call_count += 1
            data = before_snap if call_count == 1 else after_snap

            class MockResp:
                def read(self):
                    return json.dumps(data).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return MockResp()

        with patch("forge.core.reactive.cost_tracking.urllib.request.urlopen", mock_urlopen):
            with track_verb_cost("panel", ["http://localhost:8084"]):
                pass

        records = read_verb_logs()
        assert len(records) == 1
        assert records[0]["verb"] == "panel"
        assert records[0]["total_cost_micros"] == 150_000
        assert records[0]["request_count"] == 3
        assert records[0]["input_tokens"] == 3000

    def test_snapshot_delta_logged_when_wrapped_code_raises(self, verb_log_dir: Path):
        """Failed verb invocations should still attribute completed proxy work."""
        before_snap = {
            "is_proxy": True,
            "metrics": {
                "total_requests": 5,
                "tokens": {"input": 1000, "output": 500, "cached": 100},
                "costs": {"total_micros": 50_000},
            },
        }
        after_snap = {
            "is_proxy": True,
            "metrics": {
                "total_requests": 7,
                "tokens": {"input": 2500, "output": 1300, "cached": 150},
                "costs": {"total_micros": 125_000},
            },
        }
        call_count = 0

        def mock_urlopen(url, timeout=None):
            nonlocal call_count
            call_count += 1
            data = before_snap if call_count == 1 else after_snap

            class MockResp:
                def read(self):
                    return json.dumps(data).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return MockResp()

        with patch("forge.core.reactive.cost_tracking.urllib.request.urlopen", mock_urlopen):
            with pytest.raises(RuntimeError, match="panel failed"):
                with track_verb_cost("panel", ["http://localhost:8084"]):
                    raise RuntimeError("panel failed")

        records = read_verb_logs()
        assert len(records) == 1
        assert records[0]["verb"] == "panel"
        assert records[0]["total_cost_micros"] == 75_000
        assert records[0]["request_count"] == 2
        assert records[0]["input_tokens"] == 1500


class TestResolveProxyUrls:
    def test_skips_none_proxies(self):
        class FakeSpec:
            def __init__(self, proxy):
                self.proxy = proxy

        port = iter([8084, 8085])

        with patch("forge.core.reactive.proxy.lookup_proxy_base_url") as mock_lookup:
            mock_lookup.side_effect = lambda p: f"http://localhost:{next(port)}"
            urls = resolve_proxy_urls(
                [
                    FakeSpec(proxy="litellm-openai"),
                    FakeSpec(proxy=None),
                    FakeSpec(proxy="litellm-gemini"),
                ]
            )
            assert len(urls) == 2
            assert mock_lookup.call_count == 2

    def test_deduplicates_urls(self):
        class FakeSpec:
            def __init__(self, proxy):
                self.proxy = proxy

        with patch("forge.core.reactive.proxy.lookup_proxy_base_url") as mock_lookup:
            mock_lookup.return_value = "http://localhost:8084"
            urls = resolve_proxy_urls(
                [
                    FakeSpec(proxy="litellm-openai"),
                    FakeSpec(proxy="litellm-openai"),
                ]
            )
            assert len(urls) == 1
