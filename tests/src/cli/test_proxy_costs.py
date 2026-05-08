"""Tests for proxy cost reporting."""

from __future__ import annotations

import json

from click.testing import CliRunner

from forge.cli.proxy_costs import _scope_verb_records_to_proxy, costs_cmd


def test_scope_verb_records_to_proxy_slices_multi_proxy_record() -> None:
    records = [
        {
            "verb": "panel",
            "total_cost_micros": 125_000,
            "input_tokens": 12_000,
            "output_tokens": 4_500,
            "cached_tokens": 2_000,
            "request_count": 3,
            "per_proxy": [
                {
                    "base_url": "http://localhost:8084",
                    "cost_micros": 80_000,
                    "input_tokens": 8_000,
                    "output_tokens": 3_000,
                    "cached_tokens": 1_200,
                    "request_count": 2,
                },
                {
                    "base_url": "http://localhost:8085",
                    "cost_micros": 45_000,
                    "input_tokens": 4_000,
                    "output_tokens": 1_500,
                    "cached_tokens": 800,
                    "request_count": 1,
                },
            ],
        },
        {
            "verb": "supervisor",
            "total_cost_micros": 10_000,
            "request_count": 1,
            "per_proxy": [
                {
                    "base_url": "http://localhost:8085",
                    "cost_micros": 10_000,
                    "request_count": 1,
                }
            ],
        },
    ]

    scoped = _scope_verb_records_to_proxy(records, "http://localhost:8084/")

    assert len(scoped) == 1
    assert scoped[0]["verb"] == "panel"
    assert scoped[0]["total_cost_micros"] == 80_000
    assert scoped[0]["input_tokens"] == 8_000
    assert scoped[0]["output_tokens"] == 3_000
    assert scoped[0]["cached_tokens"] == 1_200
    assert scoped[0]["request_count"] == 2
    assert len(scoped[0]["per_proxy"]) == 1
    assert scoped[0]["per_proxy"][0]["base_url"] == "http://localhost:8084"


def test_costs_json_filters_verb_records_by_proxy(monkeypatch) -> None:
    request_records = [
        {
            "proxy_id": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "cost_micros": 100_000,
            "input_tokens": 1_000,
            "output_tokens": 500,
        },
        {
            "proxy_id": "litellm-gemini",
            "model": "gemini/gemini-2.5-pro",
            "cost_micros": 40_000,
            "input_tokens": 800,
            "output_tokens": 200,
        },
    ]
    verb_records = [
        {
            "verb": "panel",
            "total_cost_micros": 120_000,
            "request_count": 3,
            "per_proxy": [
                {"base_url": "http://localhost:8084", "cost_micros": 80_000, "request_count": 2},
                {"base_url": "http://localhost:8085", "cost_micros": 40_000, "request_count": 1},
            ],
        },
        {
            "verb": "supervisor",
            "total_cost_micros": 40_000,
            "request_count": 1,
            "per_proxy": [
                {"base_url": "http://localhost:8085", "cost_micros": 40_000, "request_count": 1},
            ],
        },
    ]

    monkeypatch.setattr("forge.proxy.cost_logger.read_cost_logs", lambda *args, **kwargs: request_records)
    monkeypatch.setattr("forge.core.reactive.cost_tracking.read_verb_logs", lambda *args, **kwargs: verb_records)
    monkeypatch.setattr("forge.core.reactive.proxy.lookup_proxy_base_url", lambda proxy_id: "http://localhost:8084")

    result = CliRunner().invoke(costs_cmd, ["openrouter", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total_cost_micros"] == 100_000
    assert data["interactive_cost_micros"] == 20_000
    assert set(data["by_verb"]) == {"panel"}
    assert data["by_verb"]["panel"]["cost_micros"] == 80_000
    assert data["by_verb"]["panel"]["request_count"] == 2
    assert set(data["by_model"]) == {"anthropic/claude-sonnet-4.6"}
