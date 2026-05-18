"""Regression: cap error hint must reference the actual config key names.

Bug: _cap_result_message() used f"per_{cap_type}" which produced "per_daily"
and "per_monthly" -- but the actual config keys are "per_day" and "per_month".
Users copy-pasting the hint would get an invalid config key.
"""

from __future__ import annotations

import pytest

from forge.proxy.cost_tracker import CapResult

pytestmark = pytest.mark.regression


def test_bug_daily_hint_uses_per_day() -> None:
    from forge.proxy.server import _cap_result_message

    result = CapResult(exceeded=True, cap_type="daily", current_micros=5_000_000, limit_micros=5_000_000)
    msg = _cap_result_message(result)
    assert "per_day" in msg
    assert "per_daily" not in msg


def test_bug_monthly_hint_uses_per_month() -> None:
    from forge.proxy.server import _cap_result_message

    result = CapResult(exceeded=True, cap_type="monthly", current_micros=50_000_000, limit_micros=50_000_000)
    msg = _cap_result_message(result)
    assert "per_month" in msg
    assert "per_monthly" not in msg
