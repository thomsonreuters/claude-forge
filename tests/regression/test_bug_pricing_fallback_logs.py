"""Regression: pricing fallbacks must log warnings.

Bug: get_pricing() silently fell back to default or hardcoded rates for
unknown models. No log output indicated the model lacked catalog pricing,
making $0-ish cost estimates invisible.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from forge.core.models.pricing import get_pricing, reset_pricing_cache

pytestmark = pytest.mark.regression


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    reset_pricing_cache()
    yield
    reset_pricing_cache()


def test_bug_default_pricing_fallback_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Requesting pricing for an unknown model should warn when using default rates."""
    with caplog.at_level(logging.WARNING, logger="forge.core.models.pricing"):
        pricing = get_pricing("totally-unknown-model-xyz")

    assert pricing.source == "default"
    assert any("totally-unknown-model-xyz" in r.message and "default rates" in r.message for r in caplog.records)


def test_bug_hardcoded_pricing_fallback_logs_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the default section is missing, hardcoded fallback should warn."""
    import forge.core.models.pricing as pricing_mod

    original_loader = pricing_mod._load_pricing_yaml

    def _loader_without_default():
        data = original_loader()
        data.pop("default", None)
        return data

    monkeypatch.setattr(pricing_mod, "_load_pricing_yaml", _loader_without_default)

    with caplog.at_level(logging.WARNING, logger="forge.core.models.pricing"):
        pricing = get_pricing("totally-unknown-model-xyz")

    assert pricing.source == "hardcoded"
    assert any("totally-unknown-model-xyz" in r.message and "hardcoded fallback" in r.message for r in caplog.records)
