"""CLI command: forge proxy costs.

Shows cost breakdowns from persistent JSONL cost logs. Reads both
per-request logs (model/tier analysis) and per-verb logs (functional
attribution). "Interactive" cost is computed as the residual.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import click
from rich.console import Console
from rich.table import Table

console = Console(stderr=True)


def _local_period_bounds(period: str) -> tuple[datetime, datetime]:
    """Compute UTC start/end for a named period using local timezone."""
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)

    if period == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = local_midnight.astimezone(timezone.utc)
        return start, now_utc
    elif period == "week":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = local_midnight - timedelta(days=local_midnight.weekday())
        start = week_start.astimezone(timezone.utc)
        return start, now_utc
    elif period == "month":
        local_month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = local_month_start.astimezone(timezone.utc)
        return start, now_utc
    else:
        return datetime.min.replace(tzinfo=timezone.utc), now_utc


def _format_usd(micros: int) -> str:
    usd = micros / 1_000_000
    if usd >= 1.0:
        return f"${usd:,.2f}"
    if usd >= 0.01:
        return f"${usd:.2f}"
    return f"${usd:.4f}"


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


@click.command("costs")
@click.argument("proxy_id", required=False, default=None)
@click.option(
    "--period",
    type=click.Choice(["today", "week", "month", "all"]),
    default="today",
    help="Time period to show (default: today)",
)
@click.option("--by-model", is_flag=True, help="Breakdown by model")
@click.option("--by-verb", is_flag=True, help="Breakdown by verb (default view)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def costs_cmd(
    proxy_id: str | None,
    period: str,
    by_model: bool,
    by_verb: bool,
    as_json: bool,
) -> None:
    """Show cost summary from proxy cost logs.

    \b
    Examples:
        forge proxy costs                    # Today's costs, by verb
        forge proxy costs --by-model         # Today's costs, by model
        forge proxy costs --period week      # This week
        forge proxy costs --period all       # All time
        forge proxy costs openrouter         # Filter by proxy
    """
    from forge.core.reactive.cost_tracking import read_verb_logs
    from forge.proxy.cost_logger import read_cost_logs

    start, end = _local_period_bounds(period)
    if period == "all":
        request_records = read_cost_logs()
        verb_records = read_verb_logs()
    else:
        request_records = read_cost_logs(period_start=start, period_end=end)
        verb_records = read_verb_logs(period_start=start, period_end=end)

    if proxy_id:
        request_records = [r for r in request_records if r.get("proxy_id") == proxy_id]

    if as_json:
        _output_json(request_records, verb_records, period, proxy_id)
        return

    if by_model:
        _display_by_model(request_records, period, proxy_id)
    else:
        _display_by_verb(request_records, verb_records, period, proxy_id)


def _display_by_verb(
    request_records: list[dict],
    verb_records: list[dict],
    period: str,
    proxy_id: str | None,
) -> None:
    total_cost = sum(r.get("cost_micros", 0) for r in request_records)
    total_requests = len(request_records)

    verb_costs: dict[str, dict] = {}
    for v in verb_records:
        verb = v.get("verb", "unknown")
        if verb not in verb_costs:
            verb_costs[verb] = {"cost_micros": 0, "request_count": 0, "invocations": 0}
        verb_costs[verb]["cost_micros"] += v.get("total_cost_micros", 0)
        verb_costs[verb]["request_count"] += v.get("request_count", 0)
        verb_costs[verb]["invocations"] += 1

    verb_total = sum(v["cost_micros"] for v in verb_costs.values())
    interactive_cost = max(0, total_cost - verb_total)

    if total_cost == 0 and not verb_costs:
        scope = f" ({proxy_id})" if proxy_id else ""
        console.print(f"[dim]No cost data for {period}{scope}.[/dim]")
        return

    scope = f" ({proxy_id})" if proxy_id else ""
    console.print(f"\n[bold]Cost Summary ({period}{scope}):[/bold]")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Source", style="cyan")
    table.add_column("Cost", justify="right")
    table.add_column("Detail", style="dim")
    table.add_column("", style="dim")

    table.add_row("Total", _format_usd(total_cost), f"{total_requests} requests", "")
    table.add_row(
        "Interactive",
        _format_usd(interactive_cost),
        "unattributed",
        "~",
    )

    for verb in sorted(verb_costs):
        info = verb_costs[verb]
        detail = f"{info['invocations']} run{'s' if info['invocations'] != 1 else ''}"
        if info["request_count"]:
            detail += f", {info['request_count']} reqs"
        table.add_row(verb, _format_usd(info["cost_micros"]), detail, "~")

    console.print(table)
    console.print()


def _display_by_model(
    request_records: list[dict],
    period: str,
    proxy_id: str | None,
) -> None:
    model_costs: dict[str, dict] = {}
    for r in request_records:
        model = r.get("model", "unknown")
        if model not in model_costs:
            model_costs[model] = {
                "cost_micros": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "requests": 0,
            }
        model_costs[model]["cost_micros"] += r.get("cost_micros", 0)
        model_costs[model]["input_tokens"] += r.get("input_tokens", 0)
        model_costs[model]["output_tokens"] += r.get("output_tokens", 0)
        model_costs[model]["requests"] += 1

    if not model_costs:
        scope = f" ({proxy_id})" if proxy_id else ""
        console.print(f"[dim]No cost data for {period}{scope}.[/dim]")
        return

    scope = f" ({proxy_id})" if proxy_id else ""
    console.print(f"\n[bold]By Model ({period}{scope}):[/bold]")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Model", style="cyan")
    table.add_column("Cost", justify="right")
    table.add_column("Tokens", style="dim")

    for model in sorted(model_costs, key=lambda m: model_costs[m]["cost_micros"], reverse=True):
        info = model_costs[model]
        tokens = f"{_format_tokens(info['input_tokens'])} in, {_format_tokens(info['output_tokens'])} out"
        table.add_row(model, _format_usd(info["cost_micros"]), tokens)

    console.print(table)
    console.print()


def _output_json(
    request_records: list[dict],
    verb_records: list[dict],
    period: str,
    proxy_id: str | None,
) -> None:
    total_cost = sum(r.get("cost_micros", 0) for r in request_records)

    verb_summary: dict[str, dict] = {}
    for v in verb_records:
        verb = v.get("verb", "unknown")
        if verb not in verb_summary:
            verb_summary[verb] = {"cost_micros": 0, "request_count": 0, "invocations": 0}
        verb_summary[verb]["cost_micros"] += v.get("total_cost_micros", 0)
        verb_summary[verb]["request_count"] += v.get("request_count", 0)
        verb_summary[verb]["invocations"] += 1

    model_summary: dict[str, dict] = {}
    for r in request_records:
        model = r.get("model", "unknown")
        if model not in model_summary:
            model_summary[model] = {"cost_micros": 0, "input_tokens": 0, "output_tokens": 0, "requests": 0}
        model_summary[model]["cost_micros"] += r.get("cost_micros", 0)
        model_summary[model]["input_tokens"] += r.get("input_tokens", 0)
        model_summary[model]["output_tokens"] += r.get("output_tokens", 0)
        model_summary[model]["requests"] += 1

    verb_total = sum(v["cost_micros"] for v in verb_summary.values())

    output = {
        "period": period,
        "proxy_id": proxy_id,
        "total_cost_micros": total_cost,
        "total_cost_usd": round(total_cost / 1_000_000, 6),
        "total_requests": len(request_records),
        "interactive_cost_micros": max(0, total_cost - verb_total),
        "by_verb": verb_summary,
        "by_model": model_summary,
        "estimated": True,
    }
    click.echo(json.dumps(output, indent=2))
