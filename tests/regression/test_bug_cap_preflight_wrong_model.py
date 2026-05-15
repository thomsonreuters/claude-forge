"""Regression: strict cap preflight must price the resolved model, not raw input.

Bug: The strict cap preflight used `request_data.model or "claude-sonnet-4-6"` for
cost estimation, but model resolution (tier mapping, alternatives) happens later.
A tier=opus request through an OpenAI proxy would price against "claude-sonnet-4-6"
instead of the actual resolved model (e.g., "o3"), underestimating the cap.

Root cause: Cap check block was positioned before model resolution in create_message().
Fix: Moved cap check to after actual_model_id is known.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from forge.proxy.cost_tracker import CostTracker

pytestmark = pytest.mark.regression


@pytest.mark.asyncio
async def test_bug_strict_cap_uses_resolved_model() -> None:
    """Strict cap preflight must receive the tier-resolved model, not the raw input."""
    from forge.proxy import server
    from forge.proxy.data_models import MessagesRequest

    captured_models: list[str] = []

    def spy_calc(model: str, *args: Any, **kwargs: Any) -> int:
        captured_models.append(model)
        return 999_999

    tracker = CostTracker(daily_cap_usd=0.0005, cap_mode="strict", on_cap_hit="reject")

    class State:
        request_id = "req-cap-model"

    class FakeRequest:
        state = State()
        headers: dict[str, str] = {}

    raw_input_model = "claude-sonnet-4-6"

    with (
        patch.object(server, "cost_tracker", tracker),
        patch.object(server, "reload", lambda **kw: None),
        patch("forge.core.models.pricing.calculate_cost", side_effect=spy_calc),
    ):
        request_data = MessagesRequest(
            model=raw_input_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "x" * 500}],  # type: ignore[list-item]
        )

        resp = await server.create_message(request_data, FakeRequest())  # type: ignore[arg-type]

    assert resp.status_code == 429, "Should reject when cap is exceeded"
    assert len(captured_models) >= 1, "calculate_cost should have been called for preflight"

    preflight_model = captured_models[0]
    assert preflight_model != raw_input_model, (
        f"Preflight used raw input model '{raw_input_model}' instead of the tier-resolved model. "
        "Strict cap should price the resolved backend model, not the Anthropic model name."
    )
