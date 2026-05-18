"""Regression: cross-proxy cap bootstrap must isolate spend per proxy.

Bug: CostTracker.bootstrap_from_logs() read all JSONL records regardless of
proxy_id, causing proxy A to reject requests based on proxy B's spend.
Root cause: _parse_record() extracted (ts, cost_micros, month_key) but never
checked the proxy_id field.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forge.proxy.cost_tracker import CostTracker

pytestmark = pytest.mark.regression


def test_bug_cross_proxy_bootstrap_isolates_spend(tmp_path: Path) -> None:
    log_dir = tmp_path / "costs" / "requests"
    log_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    path = log_dir / f"{month}_9999.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"ts": ts, "cost_micros": 8_000_000, "proxy_id": "expensive-proxy"}) + "\n")
        f.write(json.dumps({"ts": ts, "cost_micros": 500_000, "proxy_id": "cheap-proxy"}) + "\n")

    tracker_cheap = CostTracker(daily_cap_usd=5.00)
    tracker_cheap.bootstrap_from_logs(log_dir, proxy_id="cheap-proxy")
    assert not tracker_cheap.check_cap().exceeded

    tracker_expensive = CostTracker(daily_cap_usd=5.00)
    tracker_expensive.bootstrap_from_logs(log_dir, proxy_id="expensive-proxy")
    assert tracker_expensive.check_cap().exceeded
