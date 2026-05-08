"""Tests for spend cap enforcement (CostTracker)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forge.proxy.cost_tracker import CostTracker


class TestCostTrackerBasic:
    def test_no_caps_never_exceeded(self):
        t = CostTracker()
        for _ in range(100):
            t.record(1_000_000)
        assert not t.check_cap().exceeded

    def test_daily_cap_post_mode(self):
        t = CostTracker(daily_cap_usd=1.00, cap_mode="post")
        t.record(500_000)
        assert not t.check_cap().exceeded

        t.record(600_000)
        result = t.check_cap()
        assert result.exceeded
        assert result.cap_type == "daily"
        assert not result.projected

    def test_monthly_cap_post_mode(self):
        t = CostTracker(monthly_cap_usd=5.00, cap_mode="post")
        t.record(4_000_000)
        assert not t.check_cap().exceeded

        t.record(1_500_000)
        result = t.check_cap()
        assert result.exceeded
        assert result.cap_type == "monthly"

    def test_strict_mode_blocks_projected(self):
        t = CostTracker(daily_cap_usd=1.00, cap_mode="strict")
        t.record(800_000)
        assert not t.check_cap().exceeded

        result = t.check_cap(projected_cost_micros=300_000)
        assert result.exceeded
        assert result.projected

    def test_strict_mode_allows_under_projection(self):
        t = CostTracker(daily_cap_usd=1.00, cap_mode="strict")
        t.record(800_000)
        result = t.check_cap(projected_cost_micros=100_000)
        assert not result.exceeded

    def test_post_mode_ignores_projected(self):
        t = CostTracker(daily_cap_usd=1.00, cap_mode="post")
        t.record(800_000)
        result = t.check_cap(projected_cost_micros=500_000)
        assert not result.exceeded

    def test_daily_checked_before_monthly(self):
        t = CostTracker(daily_cap_usd=1.00, monthly_cap_usd=100.00, cap_mode="post")
        t.record(1_500_000)
        result = t.check_cap()
        assert result.exceeded
        assert result.cap_type == "daily"

    def test_has_caps(self):
        assert not CostTracker().has_caps
        assert CostTracker(daily_cap_usd=1.0).has_caps
        assert CostTracker(monthly_cap_usd=5.0).has_caps


class TestCapSummary:
    def test_summary_with_both_caps(self):
        t = CostTracker(daily_cap_usd=10.00, monthly_cap_usd=100.00)
        t.record(2_000_000)
        summary = t.cap_summary()
        assert "daily" in summary
        assert "monthly" in summary
        assert summary["daily"]["current_usd"] == pytest.approx(2.0)
        assert summary["daily"]["limit_usd"] == pytest.approx(10.0)
        assert summary["monthly"]["current_usd"] == pytest.approx(2.0)

    def test_summary_no_caps(self):
        t = CostTracker()
        assert t.cap_summary() == {}


class TestBootstrap:
    def test_bootstrap_from_empty_dir(self, tmp_path: Path):
        t = CostTracker(daily_cap_usd=10.0)
        t.bootstrap_from_logs(tmp_path / "nonexistent")
        assert t.daily_spend_micros() == 0

    def test_bootstrap_reads_current_month(self, tmp_path: Path):
        log_dir = tmp_path / "costs" / "requests"
        log_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        path = log_dir / f"{month}_9999.jsonl"

        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "w") as f:
            f.write(json.dumps({"ts": ts, "cost_micros": 500_000}) + "\n")
            f.write(json.dumps({"ts": ts, "cost_micros": 300_000}) + "\n")

        t = CostTracker(daily_cap_usd=10.0, monthly_cap_usd=100.0)
        t.bootstrap_from_logs(log_dir)

        assert t.daily_spend_micros() == 800_000
        assert t.monthly_spend_micros() == 800_000

    def test_bootstrap_skips_zero_cost(self, tmp_path: Path):
        log_dir = tmp_path / "costs" / "requests"
        log_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        path = log_dir / f"{month}_9999.jsonl"

        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "w") as f:
            f.write(json.dumps({"ts": ts, "cost_micros": 0}) + "\n")
            f.write(json.dumps({"ts": ts, "cost_micros": 100_000}) + "\n")

        t = CostTracker(daily_cap_usd=10.0)
        t.bootstrap_from_logs(log_dir)
        assert t.daily_spend_micros() == 100_000

    def test_bootstrap_caps_survive_init(self, tmp_path: Path):
        """After bootstrap, check_cap uses the bootstrapped data."""
        log_dir = tmp_path / "costs" / "requests"
        log_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        path = log_dir / f"{month}_9999.jsonl"

        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "w") as f:
            f.write(json.dumps({"ts": ts, "cost_micros": 5_500_000}) + "\n")

        t = CostTracker(daily_cap_usd=5.00)
        t.bootstrap_from_logs(log_dir)

        result = t.check_cap()
        assert result.exceeded
        assert result.cap_type == "daily"

    def test_bootstrap_reads_multiple_shards(self, tmp_path: Path):
        log_dir = tmp_path / "costs" / "requests"
        log_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        for pid in [1111, 2222]:
            path = log_dir / f"{month}_{pid}.jsonl"
            with open(path, "w") as f:
                f.write(json.dumps({"ts": ts, "cost_micros": 100_000}) + "\n")

        t = CostTracker(monthly_cap_usd=10.0)
        t.bootstrap_from_logs(log_dir)
        assert t.monthly_spend_micros() == 200_000


class TestDailyWindowRolling:
    def test_old_entries_pruned(self):
        t = CostTracker(daily_cap_usd=10.0)

        old_ts = time.time() - 90000  # 25 hours ago
        t._daily_window.append((old_ts, 5_000_000))
        t.record(100_000)

        assert t.daily_spend_micros() == 100_000


class TestMonthlyWindow:
    def test_month_rolls_during_preflight_check(self):
        """A long-running proxy should not reject new-month requests on stale spend."""
        t = CostTracker(monthly_cap_usd=1.00)
        t._monthly_key = "1999-01"
        t._monthly_total = 2_000_000

        result = t.check_cap()

        assert not result.exceeded
        assert t.monthly_spend_micros() == 0
