"""Regression: LiteLLMClient Responses-API routing must follow the model catalog.

Bug: the LiteLLM client maintained a hardcoded set of model names alongside
the model catalog to decide which models route to OpenAI's Responses API.
The two registries drifted -- a model marked `use_responses_api: true` in
`model_catalog.yaml` could be missing from the hardcoded set, silently
routing requests to Chat Completions instead of Responses API.

Root cause: two parallel sources of truth. `ModelSpec.use_responses_api`
(loaded from the catalog) and a hardcoded list in the client code each
described "which models use the Responses API." Every new OpenAI model
required edits in both places; missing the second edit produced silent
mis-routing.

Affected files:
- src/forge/core/llm/clients/litellm.py (`_should_use_responses_api`)
- src/forge/core/data/model_catalog.yaml (per-model `use_responses_api` flag)

Fix: drive routing from the catalog (`get_model_spec(model).use_responses_api`)
and delete the parallel registry. This test iterates every catalog entry
with `use_responses_api: true` and verifies the client routes it through
the Responses API -- catching any future drift the moment a parallel
registry reappears.
"""

from __future__ import annotations

import pytest

from forge.core.llm.clients.litellm import LiteLLMClient
from forge.core.llm.types import ModelHyperparameters
from forge.core.models.catalog import load_model_catalog

pytestmark = pytest.mark.regression


def _make_client(model: str) -> LiteLLMClient:
    return LiteLLMClient(model=model, provider="litellm_remote")


def test_bug_gpt55_pro_routes_to_responses_api() -> None:
    """The exact model that triggered the bug: openai/gpt-5.5-pro must route to Responses API."""
    client = _make_client("openai/gpt-5.5-pro")
    assert client._should_use_responses_api(None, ModelHyperparameters()), (
        "gpt-5.5-pro is marked use_responses_api: true in model_catalog.yaml "
        "but the client failed to route it -- requests would fall back to Chat Completions"
    )


def test_catalog_responses_api_entries_all_routed_correctly() -> None:
    """Every catalog model with use_responses_api: true must be routed to Responses API.

    This is the drift sentinel: if anyone reintroduces a parallel registry
    (e.g., a new hardcoded list) that disagrees with the catalog, the next
    catalog addition will make this test fail. The catalog is the single
    source of truth for Responses-API routing.
    """
    catalog = load_model_catalog()
    responses_api_models = [name for name, spec in catalog.models.items() if spec.use_responses_api]

    assert responses_api_models, "Sanity: catalog must contain at least one use_responses_api: true entry"

    undetected: list[str] = []
    for model_name in responses_api_models:
        client = _make_client(f"openai/{model_name}")
        if not client._should_use_responses_api(None, ModelHyperparameters()):
            undetected.append(model_name)

    assert not undetected, (
        f"Catalog/router drift: {len(undetected)} catalog models declare "
        f"use_responses_api: true but the client missed them: {undetected}"
    )


def test_unknown_model_falls_back_to_chat_completions() -> None:
    """Models not in the catalog must default to Chat Completions (OpenRouter open model space)."""
    client = _make_client("openrouter/some-future-model-not-in-catalog")
    assert not client._should_use_responses_api(
        None, ModelHyperparameters()
    ), "Unknown models must default to non-Responses-API (graceful fallback)"
