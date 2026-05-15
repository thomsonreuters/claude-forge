"""Regression: template tier_overrides must not set reasoning_effort for models with null support.

Bug: openrouter-minimax.yaml set reasoning_effort: high for sonnet/opus tiers,
but model_catalog.yaml had litellm_reasoning_efforts: null for minimax-m2.7.
The schema validator skips the check when the catalog field is None, so the
mismatch passed silently. At runtime the proxy would send an unsupported
parameter.

This test prevents the drift from recurring by cross-checking all templates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.regression

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "forge" / "config" / "defaults" / "templates"


def test_bug_no_reasoning_effort_when_catalog_unsupported() -> None:
    """Every template tier_override with reasoning_effort must map to a catalog model that supports it."""
    from forge.core.models.catalog import (
        ModelCatalogError,
        get_model_spec,
        resolve_model_id,
    )

    violations: list[str] = []

    for template_path in sorted(TEMPLATES_DIR.glob("*.yaml")):
        with open(template_path) as f:
            tmpl = yaml.safe_load(f)

        proxy = tmpl.get("proxy", {})

        for provider_name in ("openrouter", "litellm", "gemini", "openai"):
            provider = proxy.get(provider_name, {})
            tiers_cfg = provider.get("tiers", {})
            overrides = provider.get("tier_overrides", {})

            if not isinstance(overrides, dict):
                continue

            for tier, override in overrides.items():
                if not isinstance(override, dict):
                    continue
                if "reasoning_effort" not in override:
                    continue

                model_ref = tiers_cfg.get(tier, "")
                if not model_ref:
                    continue

                lookup = model_ref.removesuffix("[1m]")
                try:
                    canonical = resolve_model_id(lookup)
                    spec = get_model_spec(canonical)
                except ModelCatalogError:
                    violations.append(
                        f"{template_path.name}: {tier}.reasoning_effort={override['reasoning_effort']!r} "
                        f"but model {lookup!r} is not in the catalog"
                    )
                    continue

                if spec.litellm_reasoning_efforts is None:
                    violations.append(
                        f"{template_path.name}: {tier}.reasoning_effort={override['reasoning_effort']!r} "
                        f"but {canonical} has litellm_reasoning_efforts=null"
                    )

    assert not violations, "Template/catalog reasoning_effort drift:\n" + "\n".join(violations)
