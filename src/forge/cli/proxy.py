"""CLI commands for proxy management.

Proxies are model routing configurations that define model routing and hyperparameters.

Commands:
- forge proxy list              # List proxies
- forge proxy show <name>       # Show proxy contents
- forge proxy create <template> # Create proxy (starts unless --no-start)
- forge proxy edit <id>         # Edit proxy in $EDITOR
- forge proxy delete <id> [...]  # Delete proxy(ies) and stop server(s)
- forge proxy start <id>        # Start server for existing proxy
- forge proxy stop <id>         # Stop server for proxy
- forge proxy set <id> k=v      # Set single value
- forge proxy clean             # Clean up stale proxies
- forge proxy validate <id>     # Validate proxy config
- forge proxy metrics [id]      # Show runtime metrics for a running proxy
- forge proxy template list     # List available templates
- forge proxy template show <n> # Show template YAML
- forge proxy template edit <n> # Customize a template (copy-on-first-edit)
- forge proxy template reset <n># Reset to built-in default
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import click
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from forge.cli.proxy_costs import costs_cmd
from forge.config.loader import (
    get_proxy_file_path,
    get_template_path,
    get_user_template_path,
    is_user_template,
    list_template_names,
    load_config,
    load_proxy_instance_config,
    read_shipped_template,
    read_template,
    shipped_template_exists,
    template_exists,
    validate_template_name,
)
from forge.core.paths import display_path
from forge.core.process import find_pid_by_port
from forge.proxy.proxies import (
    CLI_LOCK_TIMEOUT_S,
    ProxyEntry,
    ProxyRegistry,
    ProxyRegistryCorruptedError,
    ProxyRegistryStore,
    is_pid_alive,
)
from forge.proxy.proxy_orchestrator import (
    ProxyStartError,
    TierOverrideOptions,
    check_proxy_health,
    create_proxy_file,
    prune_stale_proxies,
    start_proxy,
)


def _infer_proxy_source(entry: ProxyEntry) -> str:
    """Derive display source from pid + status (no schema change needed)."""
    if entry.pid is not None:
        return "managed" if is_pid_alive(entry.pid) else "stale"
    if entry.status == "healthy":
        return "adopted"
    return entry.status or "-"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def proxy() -> None:
    """Manage proxies (model routing configurations).

    \b
    Examples:
        forge proxy create litellm-gemini      # Create proxy from template
        forge proxy list                       # List all proxies
        forge proxy show my-proxy              # Show proxy details
    """


proxy.add_command(costs_cmd)


# --- List ---


@proxy.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_cmd(as_json: bool) -> None:
    """List proxies."""
    from forge.core.ops.context import ExecutionContext
    from forge.core.ops.proxy import list_proxies as list_proxies_op
    from forge.core.ops.session import ForgeOpError

    console = Console(width=200)

    # Prune stale proxies before listing (CLI-only side effect)
    try:
        prune_stale_proxies()
    except Exception:
        pass  # Best-effort pruning

    try:
        ctx = ExecutionContext.from_cwd()
        result = list_proxies_op(ctx=ctx)
    except ForgeOpError as e:
        if as_json:
            import json

            click.echo(json.dumps({"error": str(e)}, indent=2), err=True)
        else:
            console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if as_json:
        import json

        data = []
        for item in result.proxies:
            source = _infer_proxy_source(item.entry)
            data.append(
                {
                    "proxy_id": item.proxy_id,
                    "template": item.entry.template,
                    "base_url": item.entry.base_url,
                    "port": item.entry.port,
                    "pid": item.entry.pid,
                    "status": item.entry.status,
                    "source": source,
                }
            )
        click.echo(json.dumps(data, indent=2, default=str))
        return

    if not result.proxies:
        console.print("No proxies found.")
        console.print("\n[dim]Tip: Run 'forge proxy template list' to see available templates.[/dim]")
        return

    table = Table(title="Forge Proxies")
    table.add_column("PROXY ID", style="cyan")
    table.add_column("TEMPLATE")
    table.add_column("BASE URL")
    table.add_column("PORT", justify="right")
    table.add_column("PID", justify="right")
    table.add_column("STATUS")
    table.add_column("SOURCE", style="dim")

    for item in result.proxies:
        source = _infer_proxy_source(item.entry)
        table.add_row(
            item.proxy_id,
            item.entry.template,
            item.entry.base_url,
            str(item.entry.port),
            str(item.entry.pid) if item.entry.pid is not None else "-",
            item.entry.status or "-",
            source,
        )

    console.print(table)

    # Show backend status (best-effort, prunes dead PIDs)
    try:
        from forge.backend.registry import BackendRegistryStore

        backend_store = BackendRegistryStore()
        backends = backend_store.list_backends()  # Prunes dead PIDs
        if backends:
            console.print("\n[bold]Backends:[/bold]")
            for backend in backends:
                status_color = "green" if backend.status == "healthy" else "yellow"
                console.print(
                    f"  [{status_color}]{backend.backend_id}[/{status_color}] "
                    f"(port {backend.port}, pid {backend.pid or '-'})"
                )
    except Exception:
        # Best-effort - don't fail proxy list if backend registry has issues
        pass

    console.print("\n[dim]Tip: To use a proxy:[/dim]")
    console.print("  forge claude start --proxy <proxy_id>")
    console.print("  forge session start <name> --proxy <proxy_id>")


# --- Show ---


@proxy.command("show")
@click.argument("proxy_id")
@click.option("--raw", is_flag=True, help="Output raw YAML without syntax highlighting")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def show_cmd(proxy_id: str, raw: bool, as_json: bool) -> None:
    """Show proxy configuration.

    \b
    Examples:
        forge proxy show my-proxy
    """
    console = Console(width=200)

    from forge.core.ops.context import ExecutionContext
    from forge.core.ops.proxy import show_proxy as show_proxy_op
    from forge.core.ops.session import ForgeOpError

    try:
        ctx = ExecutionContext.from_cwd()
        result = show_proxy_op(ctx=ctx, proxy_id=proxy_id)
    except ForgeOpError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("\n[dim]Tip: Use 'forge proxy template show <name>' to show a template.[/dim]")
        sys.exit(1)

    content = result.config_yaml or ""
    path = get_proxy_file_path(proxy_id)
    entry = result.entry

    if as_json:
        import json

        data = {
            "proxy_id": proxy_id,
            "config_yaml": content,
            "entry": (
                {
                    "template": entry.template,
                    "base_url": entry.base_url,
                    "port": entry.port,
                    "pid": entry.pid,
                    "status": entry.status,
                }
                if entry
                else None
            ),
        }
        click.echo(json.dumps(data, indent=2, default=str))
        return

    if raw:
        console.print(content)
    else:
        syntax = Syntax(content, "yaml", theme="monokai", line_numbers=True)
        console.print(f"[bold]Proxy:[/bold] {proxy_id}")
        console.print(f"[bold]Path:[/bold] {display_path(path)}")

        if entry is not None:
            status_color = "green" if entry.status == "healthy" else "dim"
            console.print(f"[bold]Status:[/bold] [{status_color}]{entry.status or 'unknown'}[/{status_color}]")
            if entry.pid:
                console.print(f"[bold]PID:[/bold] {entry.pid}")

            from forge.core.logging import find_latest_log

            latest_log = find_latest_log("proxy", "proxy.*.log")
            if latest_log:
                console.print(f"[bold]Log:[/bold] {display_path(latest_log)}")

        console.print()
        console.print(syntax)


# --- Create (replaces acquire + clone) ---


@proxy.command("create")
@click.argument("template")
@click.option("--name", "-n", help="Name for the proxy (defaults to template name)")
@click.option("--port", "-p", type=int, help="Port number (defaults to template's default)")
@click.option("--no-start", is_flag=True, help="Create config only, don't start the server")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--host", type=str, default="localhost", help="Host to bind server to")
@click.option("--base-url", "upstream_url", type=str, help="Upstream LiteLLM base URL (overrides env var)")
# Per-tier reasoning effort overrides
@click.option("--haiku-reasoning", type=str, help="Override reasoning_effort for haiku tier")
@click.option("--sonnet-reasoning", type=str, help="Override reasoning_effort for sonnet tier")
@click.option("--opus-reasoning", type=str, help="Override reasoning_effort for opus tier")
# Per-tier temperature overrides
@click.option("--haiku-temperature", type=float, help="Override temperature for haiku tier")
@click.option("--sonnet-temperature", type=float, help="Override temperature for sonnet tier")
@click.option("--opus-temperature", type=float, help="Override temperature for opus tier")
@click.option("--smoke-test", is_flag=True, help="Send a test LLM request after start to verify upstream")
def create_cmd(
    template: str,
    name: str | None,
    port: int | None,
    no_start: bool,
    json_output: bool,
    host: str,
    upstream_url: str | None,
    haiku_reasoning: str | None,
    sonnet_reasoning: str | None,
    opus_reasoning: str | None,
    haiku_temperature: float | None,
    sonnet_temperature: float | None,
    opus_temperature: float | None,
    smoke_test: bool,
) -> None:
    """Create a proxy from a template and start it.

    \b
    Examples:
        forge proxy create litellm-gemini              # Create and start server
        forge proxy create litellm-gemini --no-start   # Create config only
        forge proxy create litellm-gemini -n my-proxy  # Custom name
        forge proxy create litellm-gemini --opus-reasoning=high  # With overrides
        forge proxy create litellm-openai --base-url https://litellm.corp.com  # Explicit upstream
        forge proxy create litellm-openai --smoke-test # Verify upstream after start
    """
    console = Console(width=200)

    try:
        exists = template_exists(template)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    if not exists:
        console.print(f"[red]Error:[/red] Template '{template}' not found")
        console.print("\n[dim]Tip: Run 'forge proxy template list' to see available templates.[/dim]")
        sys.exit(1)

    proxy_name = name or template

    # Preserve the raw user-provided port before default resolution.
    # When calling start_proxy(), we only pass port if the user explicitly
    # provided --port (so the orchestrator can still do template-scoped
    # port scanning for the default create path).
    user_port = port

    # Get default port from template if not specified
    if port is None:
        cfg = load_config(template=template)
        port = cfg.proxy.default_port
        if not port:
            console.print("[red]Error:[/red] Template has no default_port, use --port")
            sys.exit(1)

    base_url = f"http://{host}:{port}"

    tier_overrides = TierOverrideOptions(
        haiku_reasoning_effort=haiku_reasoning,
        sonnet_reasoning_effort=sonnet_reasoning,
        opus_reasoning_effort=opus_reasoning,
        haiku_temperature=haiku_temperature,
        sonnet_temperature=sonnet_temperature,
        opus_temperature=opus_temperature,
    )
    has_overrides = any(
        [
            haiku_reasoning,
            sonnet_reasoning,
            opus_reasoning,
            haiku_temperature,
            sonnet_temperature,
            opus_temperature,
        ]
    )

    if not no_start:
        # Check if proxy already exists when user provided --name
        if name is not None:
            proxy_path = get_proxy_file_path(proxy_name)
            if proxy_path.exists():
                console.print(f"[red]Error:[/red] Proxy '{proxy_name}' already exists")
                console.print("[dim]Tip: Use 'forge proxy start' to start it, or 'forge proxy delete' first.[/dim]")
                sys.exit(1)

        if not json_output:
            console.print(f"Creating proxy [cyan]{proxy_name}[/cyan] from '{template}'...")

        # Only pass proxy_id/port when the user explicitly provided --name/--port.
        # Otherwise let the orchestrator use its template-scoped defaults (reuse any
        # healthy proxy for the template, scan for available ports).
        explicit_proxy_id = proxy_name if name is not None else None
        explicit_port = user_port

        try:
            prune_stale_proxies()
            result = start_proxy(
                template=template,
                host=host,
                proxy_id=explicit_proxy_id,
                port=explicit_port,
                tier_overrides=tier_overrides if has_overrides else None,
                upstream_base_url=upstream_url,
            )
        except ProxyRegistryCorruptedError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)
        except ProxyStartError as e:
            console.print(f"[red]Failed to start server:[/red] {e}")
            err_str = str(e)
            if "dependency backend" not in err_str and "upstream URL" not in err_str:
                console.print("\n[dim]Tip: Use --no-start to create the config without starting the server:[/dim]")
                console.print(f"  forge proxy create {template} --name {proxy_name} --no-start")
            sys.exit(1)

        proxy_entry = result.proxy

        if json_output:
            import json

            print(
                json.dumps(
                    {
                        "proxy_id": proxy_entry.proxy_id,
                        "template": proxy_entry.template,
                        "base_url": proxy_entry.base_url,
                        "port": proxy_entry.port,
                        "pid": proxy_entry.pid,
                        "status": proxy_entry.status,
                        "source": result.source,
                    }
                )
            )
        else:
            if result.source == "reuse":
                prefix = "Reusing existing"
            elif result.source == "adopt":
                prefix = f"Found existing process on port {proxy_entry.port}, registered as"
            else:
                prefix = "Started"

            console.print(f"[green]{prefix}[/green] proxy [cyan]{proxy_entry.proxy_id}[/cyan]")
            console.print(f"  URL: {proxy_entry.base_url}")
            console.print(f"  PID: {proxy_entry.pid or '-'}")

            # Show log location (skip for adopted — no Forge-owned log exists)
            if result.source != "adopt":
                from forge.core.logging import find_latest_log

                latest_log = find_latest_log("proxy", "proxy.*.log")
                if latest_log:
                    console.print(f"  Log: {display_path(latest_log)}")

            if result.source == "adopt":
                console.print(
                    f"\n[dim]Tip: This proxy was not started by Forge. "
                    f"Logs may be unavailable.\n"
                    f"  Delete and recreate for full Forge management: "
                    f"forge proxy delete {proxy_entry.proxy_id} && "
                    f"forge proxy create {proxy_entry.template}[/dim]"
                )

        if smoke_test:
            from forge.proxy.proxy_orchestrator import smoke_test_proxy

            if not json_output:
                console.print("\n[dim]Smoke testing upstream LLM...[/dim]")

            ok, detail = smoke_test_proxy(base_url=proxy_entry.base_url)

            if json_output:
                import json

                print(json.dumps({"smoke_test": {"passed": ok, "detail": detail}}))
            elif ok:
                console.print(f"[green]Smoke test passed[/green]: {detail[:80]}")
            else:
                console.print(f"[red]Smoke test failed[/red]: {detail}")
                sys.exit(1)
    else:
        proxy_path = get_proxy_file_path(proxy_name)
        if proxy_path.exists():
            console.print(f"[red]Error:[/red] Proxy '{proxy_name}' already exists")
            console.print("[dim]Tip: Use 'forge proxy edit' to modify it, or 'forge proxy delete' first.[/dim]")
            sys.exit(1)

        try:
            created_path = create_proxy_file(
                proxy_id=proxy_name,
                template=template,
                base_url=base_url,
                port=port,
                cli_overrides=tier_overrides if has_overrides else None,
                upstream_base_url=upstream_url,
            )
        except Exception as e:
            console.print(f"[red]Error:[/red] Failed to create proxy: {e}")
            sys.exit(1)

        # Register the proxy in index.json so it appears in `forge proxy list`
        from forge.core.state import now_iso

        store = ProxyRegistryStore()
        now = now_iso()
        proxy_entry = ProxyEntry(
            proxy_id=proxy_name,
            template=template,
            base_url=base_url,
            port=port,
            pid=None,
            created_at=now,
            last_seen_at=None,
            status="configured",
        )

        def _register_proxy(registry: ProxyRegistry) -> None:
            registry.proxies[proxy_name] = proxy_entry

        try:
            store.update(timeout_s=CLI_LOCK_TIMEOUT_S, mutate=_register_proxy)
        except Exception as e:
            try:
                shutil.rmtree(created_path.parent)
            except OSError as cleanup_error:
                console.print(
                    f"[yellow]Warning:[/yellow] Could not remove unregistered proxy directory: {cleanup_error}"
                )
            console.print(f"[red]Error:[/red] Could not register proxy: {e}")
            sys.exit(1)

        console.print(f"[green]Created[/green] proxy [cyan]{proxy_name}[/cyan] from '{template}'")
        console.print(f"  Path: {display_path(created_path)}")
        console.print(f"  Port: {port}")
        console.print("\n[dim]Next steps:[/dim]")
        console.print(f"  forge proxy edit {proxy_name}   # Customize config")
        console.print(f"  forge proxy start {proxy_name}  # Start server")


# --- Start / Stop ---


StopProxyOutcome = Literal["stopped", "already_stopped", "adopted_left_running", "error"]


def _stop_proxy_process(console: Console, entry: ProxyEntry, *, kill_adopted: bool = False) -> StopProxyOutcome:
    """Kill the proxy process if Forge owns it (known PID).

    Adopted proxies (pid=None) are NOT killed by default — Forge didn't start
    them and shouldn't stop them. Pass kill_adopted=True to override.

    Returns:
        "stopped": A process was killed.
        "already_stopped": No live process remained; registry state should be cleared.
        "error": Refused or failed to stop the process.
    """
    # Known PID — Forge started this process, safe to kill
    if entry.pid is not None and is_pid_alive(entry.pid):
        try:
            os.kill(entry.pid, signal.SIGTERM)
            console.print(f"Stopped server (pid {entry.pid})")
            return "stopped"
        except (ProcessLookupError, PermissionError) as e:
            console.print(f"[yellow]Warning:[/yellow] Could not stop server: {e}")
            return "error"

    # PID unknown (adopted) — not our process to kill
    if entry.pid is None:
        if not kill_adopted:
            console.print(
                f"[dim]Adopted proxy on port {entry.port} (not started by Forge, leaving process alone)[/dim]"
            )
            return "adopted_left_running"

        # Explicit kill_adopted: find by port with health guard
        discovered_pid = find_pid_by_port(entry.port)
        if discovered_pid is None:
            console.print(f"[dim]No process found on port {entry.port}[/dim]")
            return "already_stopped"

        if not check_proxy_health(
            base_url=entry.base_url,
            expected_template=entry.template,
            timeout_s=1.0,
            expected_proxy_id=entry.proxy_id,
        ):
            console.print(
                f"[yellow]Warning:[/yellow] Process on port {entry.port} doesn't match "
                f"proxy '{entry.proxy_id}' (template '{entry.template}'), skipping kill"
            )
            return "error"

        try:
            os.kill(discovered_pid, signal.SIGTERM)
            console.print(f"Stopped server on port {entry.port} (discovered pid {discovered_pid})")
            return "stopped"
        except (ProcessLookupError, PermissionError) as e:
            console.print(f"[yellow]Warning:[/yellow] Could not stop process on port {entry.port}: {e}")
            return "error"

    # PID known but process is dead
    console.print(f"[dim]Process pid {entry.pid} is not running[/dim]")
    return "already_stopped"


@proxy.command("start")
@click.argument("proxy_id")
@click.option("--smoke-test", is_flag=True, help="Send a test LLM request after start to verify upstream")
def start_cmd(proxy_id: str, smoke_test: bool) -> None:
    """Start server for an existing proxy.

    \b
    Example:
        forge proxy start my-proxy
        forge proxy start my-proxy --smoke-test
    """
    console = Console(width=200)

    proxy_path = get_proxy_file_path(proxy_id)
    if not proxy_path.exists():
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found at {display_path(proxy_path)}")
        console.print("\n[dim]Create one first:[/dim]")
        console.print(f"  forge proxy create <template> --name {proxy_id}")
        sys.exit(1)

    config = load_proxy_instance_config(proxy_id)
    if config is None:
        console.print(f"[red]Error:[/red] Failed to load proxy config for '{proxy_id}'")
        sys.exit(1)

    try:
        prune_stale_proxies()
        result = start_proxy(
            template=config.template,
            host="localhost",
            proxy_id=proxy_id,
            port=config.port,
            skip_proxy_file=True,
        )
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except ProxyStartError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    proxy_entry = result.proxy
    if result.source == "reuse":
        console.print(f"Server already running for [cyan]{proxy_entry.proxy_id}[/cyan]")
    else:
        console.print(f"[green]Started[/green] server for [cyan]{proxy_entry.proxy_id}[/cyan]")
    console.print(f"  URL: {proxy_entry.base_url}")
    console.print(f"  PID: {proxy_entry.pid}")

    if smoke_test:
        from forge.proxy.proxy_orchestrator import smoke_test_proxy

        console.print("\n[dim]Smoke testing upstream LLM...[/dim]")
        ok, detail = smoke_test_proxy(base_url=proxy_entry.base_url)
        if ok:
            console.print(f"[green]Smoke test passed[/green]: {detail[:80]}")
        else:
            console.print(f"[red]Smoke test failed[/red]: {detail}")
            sys.exit(1)


@proxy.command("stop")
@click.argument("proxy_id")
@click.option("--force", "-f", is_flag=True, help="Stop even if other proxies share the port")
@click.option("--kill-adopted", is_flag=True, help="Terminate adopted processes (not started by Forge)")
def stop_cmd(proxy_id: str, force: bool, kill_adopted: bool) -> None:
    """Stop server for a proxy (keeps the proxy config).

    \b
    Examples:
        forge proxy stop my-proxy
        forge proxy stop my-proxy --force          # Stop even if port is shared
        forge proxy stop my-proxy --kill-adopted   # Kill adopted process
    """
    console = Console(width=200)
    store = ProxyRegistryStore()

    try:
        registry = store.read()
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    entry = registry.proxies.get(proxy_id)
    if entry is None:
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found in registry")
        console.print("[dim]The proxy may not be running.[/dim]")
        console.print("[dim]Tip: Run 'forge proxy list' to see configured proxies.[/dim]")
        sys.exit(1)

    # Shared-port policy: refuse if other proxies share the same port
    if not force:
        sharing = _live_proxy_ids_on_port(registry, proxy_id, entry.port)
        if sharing:
            names = ", ".join(sharing[:5])
            console.print(f"[red]Error:[/red] Cannot stop: other proxies share port {entry.port}: {names}")
            console.print("[dim]Tip: Use --force to stop anyway, or delete individual proxies.[/dim]")
            sys.exit(1)

    outcome = _stop_proxy_process(console, entry, kill_adopted=kill_adopted)

    if outcome == "stopped":
        console.print(f"[green]Stopped[/green] server for [cyan]{proxy_id}[/cyan]")
    elif outcome == "already_stopped":
        console.print(f"[green]Cleared[/green] stale running state for [cyan]{proxy_id}[/cyan]")
    elif outcome == "adopted_left_running":
        # Process still alive (not ours to kill) — don't mark as "stopped"
        console.print(f"[green]Detached[/green] [cyan]{proxy_id}[/cyan] from registry (process still running)")

        def _detach(reg: ProxyRegistry) -> None:
            reg.proxies.pop(proxy_id, None)

        try:
            store.update(timeout_s=CLI_LOCK_TIMEOUT_S, mutate=_detach)
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Could not update registry: {e}")
        return
    else:
        # _stop_proxy_process already printed the reason
        return

    # Update registry: mark this proxy AND all siblings on the same port as stopped.
    # When --force bypasses the shared-port guard, siblings become stale.
    stopped_siblings: list[str] = []

    def clear_pid(reg: ProxyRegistry) -> None:
        nonlocal stopped_siblings
        if proxy_id in reg.proxies:
            reg.proxies[proxy_id].pid = None
            reg.proxies[proxy_id].status = "stopped"
        # Mark siblings on the same port as stopped too
        for eid, e in reg.proxies.items():
            if eid != proxy_id and e.port == entry.port and e.status != "stopped":
                e.pid = None
                e.status = "stopped"
                stopped_siblings.append(eid)

    try:
        store.update(timeout_s=CLI_LOCK_TIMEOUT_S, mutate=clear_pid)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not update registry: {e}")

    if stopped_siblings:
        console.print(
            f"[dim]Also marked as stopped (shared port {entry.port}): " f"{', '.join(stopped_siblings)}[/dim]"
        )


# --- Edit ---


@proxy.command("edit")
@click.argument("proxy_id")
def edit_cmd(proxy_id: str) -> None:
    """Open proxy configuration in $EDITOR.

    Uses a temp file for safety - changes are validated before applying.
    """
    console = Console(width=200)

    proxy_path = get_proxy_file_path(proxy_id)
    if not proxy_path.exists():
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found at {display_path(proxy_path)}")
        sys.exit(1)

    editor = os.environ.get("EDITOR", "vim")

    if not shutil.which(editor):
        console.print(f"[red]Error:[/red] Editor '{editor}' not found. Set $EDITOR to an available editor.")
        sys.exit(1)

    # Copy to temp file for safe editing
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(proxy_path.read_text())
        tmp_path = Path(tmp.name)

    success = False
    try:
        result = subprocess.run([editor, str(tmp_path)])
        if result.returncode != 0:
            console.print(f"[red]Error:[/red] Editor exited with code {result.returncode}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        from ruamel.yaml import YAML

        yaml = YAML()
        try:
            with open(tmp_path) as f:
                edited_data = yaml.load(f)
        except Exception as e:
            console.print(f"[red]Error:[/red] Invalid YAML: {e}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        # Validate through ProxyInstanceConfig (catches type errors, invalid values)
        try:
            from forge.config.loader import load_proxy_instance_config_from_dict

            load_proxy_instance_config_from_dict(edited_data)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            console.print(f"[red]Error:[/red] Invalid proxy configuration: {e}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        write_tmp = proxy_path.with_suffix(".yaml.tmp")
        shutil.copy(tmp_path, write_tmp)
        write_tmp.chmod(0o600)
        write_tmp.rename(proxy_path)

        success = True
        console.print(f"[green]Updated[/green] proxy '{proxy_id}'")

    finally:
        if success and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# --- Set ---


@proxy.command("set")
@click.argument("proxy_id")
@click.argument("key_value")
def set_cmd(proxy_id: str, key_value: str) -> None:
    """Set a single value in proxy configuration.

    Supports dot notation for nested keys.

    \b
    Examples:
        forge proxy set my-proxy default_tier=opus
        forge proxy set my-proxy tier_overrides.opus.reasoning_effort=high
        forge proxy set my-proxy port=8085
    """
    console = Console(width=200)

    if "=" not in key_value:
        console.print(f"[red]Error:[/red] Expected format: key=value (got: {key_value})")
        sys.exit(1)

    key, value = key_value.split("=", 1)

    proxy_path = get_proxy_file_path(proxy_id)
    if not proxy_path.exists():
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found at {display_path(proxy_path)}")
        sys.exit(1)

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    with open(proxy_path) as f:
        data = yaml.load(f)

    keys = key.split(".")
    current = data
    for k in keys[:-1]:
        if k not in current:
            current[k] = {}
        current = current[k]

    final_key = keys[-1]
    coerced_value: Any
    try:
        if value.lower() in ("none", "null"):
            coerced_value = None
        elif final_key in ("port", "proxy_format", "thinking_budget_tokens"):
            coerced_value = int(value)
        elif final_key in ("temperature",) or key in ("costs.caps.per_day", "costs.caps.per_month"):
            coerced_value = float(value)
        elif value.lower() == "true":
            coerced_value = True
        elif value.lower() == "false":
            coerced_value = False
        else:
            coerced_value = value
    except ValueError as e:
        console.print(f"[red]Error:[/red] Invalid value for '{final_key}': {e}")
        sys.exit(1)

    current[final_key] = coerced_value

    # Validate the full config before writing (CR-006)
    try:
        from forge.config.loader import load_proxy_instance_config_from_dict

        load_proxy_instance_config_from_dict(data)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        console.print(f"[red]Error:[/red] Invalid value — {e}")
        sys.exit(1)

    tmp_path = proxy_path.with_suffix(".yaml.tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(data, f)
    tmp_path.chmod(0o600)
    tmp_path.rename(proxy_path)

    console.print(f"[green]Set[/green] {key}={coerced_value} in proxy '{proxy_id}'")


# --- Delete ---


def _find_sessions_for_proxy(proxy_id: str, *, port: int | None = None) -> list[str]:
    """Best-effort scan for sessions affected by deleting a proxy. Returns [] on any error.

    When port is None (non-terminal delete — other entries share the port),
    matches only sessions whose confirmed.started_with_proxy.proxy_id equals the
    target. This avoids false positives from shared-port aliases.

    When port is provided (terminal delete — server will die), also matches
    sessions bound to ANY alias on that port (by extracting port from the
    session's started_with_proxy.base_url), since all sessions on the port
    will lose connectivity.
    """
    try:
        from urllib.parse import urlparse

        from forge.session.identity import session_name_from_key
        from forge.session.index import IndexStore
        from forge.session.store import SessionStore

        matching: list[str] = []
        for key, idx_entry in IndexStore().read().sessions.items():
            name = session_name_from_key(key)
            try:
                store = SessionStore(idx_entry.forge_root or idx_entry.worktree_path, name)
                if not store.exists():
                    continue
                state = store.read()
                swp = state.confirmed.started_with_proxy
                if not swp:
                    continue
                if swp.proxy_id == proxy_id:
                    matching.append(name)
                elif port is not None and swp.base_url:
                    # Extract port from session's base_url for host-spelling-agnostic match
                    parsed = urlparse(swp.base_url)
                    session_port = parsed.port
                    if session_port == port:
                        matching.append(name)
            except Exception:
                continue
        return matching
    except Exception:
        return []


_ALIVE_STATUSES = frozenset({"healthy", "starting"})


def _all_proxy_ids_on_port(registry: ProxyRegistry, proxy_id: str, port: int) -> list[str]:
    """Return ALL other proxy IDs on the same port (any status).

    Used for UX: confirmation messages should list every sibling for
    awareness, including configured and stopped entries.
    """
    return sorted(
        entry_id for entry_id, entry in registry.proxies.items() if entry_id != proxy_id and entry.port == port
    )


def _live_proxy_ids_on_port(registry: ProxyRegistry, proxy_id: str, port: int) -> list[str]:
    """Return OTHER proxy IDs that share the same port AND have a live listener.

    Only includes entries with status in {healthy, starting}. Configured
    (never started) and stopped (listener dead) entries are excluded so
    they don't block stop/delete of a healthy proxy.
    """
    return sorted(
        entry_id
        for entry_id, entry in registry.proxies.items()
        if entry_id != proxy_id and entry.port == port and entry.status in _ALIVE_STATUSES
    )


def _restore_proxy_registry_entry(store: ProxyRegistryStore, entry: ProxyEntry) -> None:
    """Best-effort restore for a registry entry removed before cleanup failed."""

    def _restore(registry: ProxyRegistry) -> None:
        registry.proxies.setdefault(entry.proxy_id, entry)

    store.update(timeout_s=CLI_LOCK_TIMEOUT_S, mutate=_restore)


@proxy.command("delete")
@click.argument("proxy_ids", nargs=-1)
@click.option("--all", "-a", "delete_all", is_flag=True, help="Delete all proxies")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--force", "-f", is_flag=True, hidden=True, help="Deprecated alias for --yes")
@click.option("--kill-adopted", is_flag=True, help="Terminate adopted processes during deletion")
@click.option("--no-kill", is_flag=True, help="Remove from registry without killing the process")
def delete_cmd(
    proxy_ids: tuple[str, ...], delete_all: bool, yes: bool, force: bool, kill_adopted: bool, no_kill: bool
) -> None:
    """Delete one or more proxies and stop their servers if running.

    \b
    Examples:
      forge proxy delete my-proxy
      forge proxy delete proxy-1 proxy-2
      forge proxy delete --all
      forge proxy delete --all --yes
    """
    # Deprecated --force alias: preserves both old behaviors (skip confirmation
    # + kill adopted) during the deprecation window.
    if force:
        yes = True
        kill_adopted = True
    console = Console(width=200)

    if delete_all and proxy_ids:
        console.print("[red]Error:[/red] Cannot combine --all with explicit proxy IDs")
        sys.exit(1)

    if not delete_all and not proxy_ids:
        console.print("[red]Error:[/red] Provide proxy ID(s) or use --all")
        sys.exit(1)

    store = ProxyRegistryStore()

    try:
        registry = store.read()
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if delete_all:
        if not registry.proxies:
            console.print("[dim]No proxies to delete.[/dim]")
            return
        targets = list(registry.proxies.keys())

        console.print(f"About to delete [bold]all {len(targets)} proxy(ies)[/bold]:")
        for t in targets:
            console.print(f"  - {t}")
        console.print()
        if not yes:
            if not click.confirm("Are you sure you want to delete all proxies?"):
                console.print("Cancelled.")
                return
    else:
        targets = list(dict.fromkeys(proxy_ids))

    deleted = 0
    failed = 0

    for proxy_id in targets:
        try:
            _delete_single_proxy(
                console=console,
                store=store,
                proxy_id=proxy_id,
                yes=yes or delete_all,
                kill_adopted=kill_adopted,
                no_kill=no_kill,
            )
            deleted += 1
        except SystemExit as e:
            if len(targets) == 1:
                raise
            if e.code not in (0, None):
                failed += 1
        except Exception as e:
            console.print(f"[red]Error:[/red] {proxy_id}: {e}")
            failed += 1

    if len(targets) > 1:
        parts = [f"{deleted} deleted"]
        if failed:
            parts.append(f"{failed} failed")
        console.print(f"\n[dim]Summary: {', '.join(parts)}[/dim]")

    if failed:
        sys.exit(1)


def _delete_single_proxy(
    *,
    console: Console,
    store: ProxyRegistryStore,
    proxy_id: str,
    yes: bool,
    kill_adopted: bool = False,
    no_kill: bool = False,
) -> None:
    """Delete a single proxy, handling confirmation and cleanup.

    Args:
        yes: Skip confirmation prompts (informational output stays visible).
        kill_adopted: Terminate adopted processes during deletion.

    Raises:
        SystemExit: If user cancels or proxy not found.
    """
    try:
        registry = store.read()
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)

    entry = registry.proxies.get(proxy_id)
    proxy_path = get_proxy_file_path(proxy_id)
    proxy_dir = proxy_path.parent

    if entry is None and not proxy_dir.exists():
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found")
        raise SystemExit(1)

    # Best-effort pre-check for UX (prompt message and session warnings).
    # The authoritative ref-count check happens under lock below.
    shared_proxy_ids: list[str] = []
    shared_url_hint = False
    if entry is not None:
        shared_proxy_ids = _all_proxy_ids_on_port(registry, proxy_id, entry.port)
        shared_url_hint = bool(shared_proxy_ids)

    # Informational output — always visible (--yes only skips prompts)
    if shared_url_hint:
        referencing_sessions = _find_sessions_for_proxy(proxy_id)
    else:
        referencing_sessions = _find_sessions_for_proxy(proxy_id, port=entry.port if entry else None)

    base_url_label = entry.base_url if entry else "unknown"
    if referencing_sessions:
        if shared_url_hint:
            console.print(f"[yellow]Warning:[/yellow] {len(referencing_sessions)} " "session(s) reference this proxy:")
        else:
            console.print(
                f"[yellow]Warning:[/yellow] Deleting the last proxy on "
                f"{base_url_label} affects "
                f"{len(referencing_sessions)} session(s):"
            )
            console.print(f"[dim]Related sessions on {base_url_label}:[/dim]")
        for s in referencing_sessions[:5]:
            console.print(f"  - {s}")
        if len(referencing_sessions) > 5:
            console.print(f"  ... and {len(referencing_sessions) - 5} more")
        console.print("\n[dim]Tip: Delete sessions first with " "'forge session delete <name>'[/dim]")

    elif not shared_url_hint and entry is not None:
        console.print(f"[dim]Related sessions on {base_url_label}:[/dim] none")

    if shared_proxy_ids:
        console.print(f"[dim]Related proxies on the same port " f"({base_url_label}):[/dim]")
        for related_proxy_id in shared_proxy_ids[:5]:
            console.print(f"  - {related_proxy_id}")
        if len(shared_proxy_ids) > 5:
            console.print(f"  ... and {len(shared_proxy_ids) - 5} more")

    # Confirmation prompt — gated by --yes only
    if not yes:
        has_process = (entry and entry.pid and is_pid_alive(entry.pid)) or (
            entry and entry.pid is None and entry.status == "healthy"
        )
        if has_process:
            if shared_url_hint:
                msg = f"Delete proxy '{proxy_id}' (server kept alive -- other proxies share this port)?"
            else:
                pid_info = f"pid {entry.pid}" if entry and entry.pid else f"port {entry.port}" if entry else ""
                msg = f"Delete proxy '{proxy_id}' and stop running server ({pid_info})?"
        else:
            if shared_url_hint:
                msg = f"Delete proxy '{proxy_id}' (other proxies share this port)?"
            else:
                msg = f"Delete proxy '{proxy_id}'?"
        if not click.confirm(msg):
            console.print("Cancelled.")
            raise SystemExit(0)

    # Remove from registry and determine PID fate under lock (TOCTOU-safe).
    should_kill_pid = False
    remaining_aliases: list[str] = []
    if entry:

        def remove_and_check(reg: ProxyRegistry) -> None:
            nonlocal should_kill_pid, remaining_aliases
            reg.proxies.pop(proxy_id, None)
            remaining_aliases = _live_proxy_ids_on_port(reg, proxy_id, entry.port)
            if not remaining_aliases:
                should_kill_pid = True

        try:
            store.update(timeout_s=CLI_LOCK_TIMEOUT_S, mutate=remove_and_check)
        except Exception as e:
            console.print(f"[red]Error:[/red] Could not update registry: {e}")
            raise SystemExit(1)

    # Post-lock summary: show authoritative remaining aliases
    if remaining_aliases:
        console.print(f"[dim]Keeping shared server references:[/dim] " f"{', '.join(remaining_aliases)}")

    # Delete proxy directory
    if proxy_dir.exists():
        try:
            shutil.rmtree(proxy_dir)
        except OSError as e:
            if entry is not None:
                try:
                    _restore_proxy_registry_entry(store, entry)
                except Exception as restore_error:
                    console.print(
                        f"[yellow]Warning:[/yellow] Could not restore registry entry after delete failure: "
                        f"{restore_error}"
                    )
            console.print(f"[red]Error:[/red] Could not delete proxy directory: {e}")
            raise SystemExit(1)

    # Kill server only if the locked check confirmed we're the last reference
    if entry and should_kill_pid and not no_kill:
        _stop_proxy_process(console, entry, kill_adopted=kill_adopted)
    elif entry and not should_kill_pid:
        console.print(f"[dim]Server kept alive (other proxies share port {entry.port})[/dim]")

    console.print(f"[green]Deleted[/green] proxy '{proxy_id}'")


# --- Prune ---


@proxy.command("clean")
def clean_cmd() -> None:
    """Clean up stale proxies (dead server processes)."""
    console = Console(width=200)

    try:
        result = prune_stale_proxies()
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not result.pruned_proxy_ids:
        console.print("No stale proxies to clean.")
        return

    console.print(f"Cleaned {len(result.pruned_proxy_ids)} stale proxy(ies):")
    for pid in result.pruned_proxy_ids:
        console.print(f"  - {pid}")


# --- Validate ---


@proxy.command("validate")
@click.argument("proxy_id")
def validate_cmd(proxy_id: str) -> None:
    """Validate a proxy configuration file."""
    console = Console(width=200)

    proxy_path = get_proxy_file_path(proxy_id)
    if not proxy_path.exists():
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found at {display_path(proxy_path)}")
        sys.exit(1)

    try:
        config = load_proxy_instance_config(proxy_id)
        if config is None:
            console.print(f"[red]Error:[/red] Failed to load proxy '{proxy_id}'")
            sys.exit(1)

        console.print(f"[green]✓[/green] Proxy '{proxy_id}' is valid")
        console.print(f"  Template: {config.template}")
        console.print(f"  Provider: {config.provider}")
        console.print(f"  Port: {config.port}")
        console.print(f"  Default tier: {config.default_tier}")

    except ValueError as e:
        console.print(f"[red]✗[/red] Validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]✗[/red] Error loading proxy: {e}")
        sys.exit(1)


# --- Metrics ---


def _format_tokens(n: int) -> str:
    """Format token count with K/M suffix for readability."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_duration(seconds: float) -> str:
    """Format duration as human-readable (3s, 5m, 2h 15m, 1d 3h)."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    return f"{days}d {hours}h"


def _format_latency(ms: float) -> str:
    """Format latency with comma separators."""
    return f"{ms:,.0f}ms"


def _format_relative_time(iso_str: str) -> str:
    """Format ISO timestamp as relative time ('12s ago', '5m ago')."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = delta.total_seconds()
        if secs < 0:
            return "just now"
        return f"{_format_duration(secs)} ago"
    except (ValueError, TypeError):
        return iso_str


@dataclass
class _ProxyInfo:
    """Fetched proxy info (metrics + identity)."""

    metrics: dict[str, Any]
    template: str | None = None


def _fetch_proxy_info(base_url: str) -> _ProxyInfo | None:
    """Fetch metrics + identity from a proxy's GET / endpoint."""
    import httpx

    try:
        with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
            resp = client.get(f"{base_url}/")
        if resp.status_code != 200:
            return None
        data = resp.json()
        metrics = data.get("metrics")
        if metrics is None:
            return None
        return _ProxyInfo(metrics=metrics, template=data.get("template"))
    except Exception:
        return None


def _display_metrics(
    console: Console,
    proxy_id: str,
    base_url: str,
    info: _ProxyInfo,
    *,
    show_separator: bool = False,
) -> None:
    """Render metrics to the console using Rich."""
    metrics = info.metrics
    uptime = _format_duration(metrics.get("uptime_seconds", 0))
    total = metrics.get("total_requests", 0)
    streaming = metrics.get("total_streaming", 0)
    failures = metrics.get("total_failures", 0)

    tokens = metrics.get("tokens", {})
    cache_rate = metrics.get("cache_hit_rate", 0)

    if show_separator:
        console.print("[dim]" + "-" * 60 + "[/dim]")
    console.print(f"\n[bold]Proxy Metrics:[/bold] {proxy_id}")
    identity_parts = []
    if info.template:
        identity_parts.append(info.template)
    identity_parts.append(base_url)
    identity_parts.append(f"uptime {uptime}")
    console.print(f"  [dim]{' | '.join(identity_parts)}[/dim]\n")

    streaming_note = f" ({streaming:,} streaming)" if streaming > 0 else ""
    console.print(f"  Requests     {total:>10,}{streaming_note}")
    if failures > 0:
        fail_pct = f" ({failures / total * 100:.1f}%)" if total > 0 else ""
        console.print(f"  Failures     {failures:>10,}{fail_pct}")
    else:
        console.print(f"  Failures     {failures:>10,}")

    console.print("\n  [bold]Tokens[/bold]")
    console.print(f"    Input      {_format_tokens(tokens.get('input', 0)):>10}")
    console.print(f"    Output     {_format_tokens(tokens.get('output', 0)):>10}")
    cached = tokens.get("cached", 0)
    cache_str = f"  ({cache_rate:.1f}% hit rate)" if cached > 0 else ""
    console.print(f"    Cached     {_format_tokens(cached):>10}{cache_str}")

    failed_in = tokens.get("failed_input", 0)
    failed_out = tokens.get("failed_output", 0)
    if failed_in > 0 or failed_out > 0:
        console.print("\n  [bold]Failed Tokens[/bold]")
        console.print(f"    Input      {_format_tokens(failed_in):>10}")
        console.print(f"    Output     {_format_tokens(failed_out):>10}")

    by_tier = metrics.get("by_tier", {})
    if by_tier:
        console.print("\n  [bold]By Tier[/bold]")
        tier_table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
        tier_table.add_column("TIER", style="bold")
        tier_table.add_column("REQUESTS", justify="right")
        tier_table.add_column("INPUT", justify="right")
        tier_table.add_column("OUTPUT", justify="right")
        tier_table.add_column("CACHED", justify="right")
        tier_table.add_column("LATENCY", justify="right")
        for tier, data in sorted(by_tier.items()):
            tier_table.add_row(
                tier,
                f"{data.get('requests', 0):,}",
                _format_tokens(data.get("input_tokens", 0)),
                _format_tokens(data.get("output_tokens", 0)),
                _format_tokens(data.get("cached_tokens", 0)),
                _format_latency(data.get("avg_latency_ms", 0)),
            )
        console.print(tier_table)

    by_model = metrics.get("by_model", {})
    if by_model:
        console.print("\n  [bold]By Model[/bold]")
        model_table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
        model_table.add_column("MODEL", style="bold")
        model_table.add_column("REQUESTS", justify="right")
        model_table.add_column("INPUT", justify="right")
        model_table.add_column("OUTPUT", justify="right")
        model_table.add_column("CACHED", justify="right")
        model_table.add_column("LATENCY", justify="right")
        for model, data in sorted(by_model.items()):
            model_table.add_row(
                model,
                f"{data.get('requests', 0):,}",
                _format_tokens(data.get("input_tokens", 0)),
                _format_tokens(data.get("output_tokens", 0)),
                _format_tokens(data.get("cached_tokens", 0)),
                _format_latency(data.get("avg_latency_ms", 0)),
            )
        console.print(model_table)

    failures_by_type = metrics.get("failures_by_type", {})
    if failures_by_type:
        console.print("\n  [bold]Failures by Type[/bold]")
        for err_type, count in sorted(failures_by_type.items(), key=lambda x: -x[1]):
            console.print(f"    {err_type:<25} {count:>5}")

    last = metrics.get("last_request_at")
    if last:
        console.print(f"\n  [dim]Last request: {_format_relative_time(last)}[/dim]")
    console.print()


@proxy.command("metrics")
@click.argument("proxy_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.option("--all", "show_all", is_flag=True, help="Show all active proxies")
def metrics_cmd(proxy_id: str | None, json_output: bool, show_all: bool) -> None:
    """Show runtime metrics for a running proxy."""
    import json

    console = Console(width=200)

    try:
        store = ProxyRegistryStore()
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] Proxy registry error: {e}")
        sys.exit(1)

    if show_all:
        try:
            proxies = store.list_proxies()
        except ProxyRegistryCorruptedError as e:
            console.print(f"[red]Error:[/red] Proxy registry error: {e}")
            sys.exit(1)
        if not proxies:
            console.print("[dim]No proxies registered.[/dim]")
            return
        if json_output:
            # Collect all results into a single valid JSON object
            results: dict[str, Any] = {}
            for entry in proxies:
                info = _fetch_proxy_info(entry.base_url)
                results[entry.proxy_id] = info.metrics if info else None
            console.print(json.dumps(results, indent=2))
        else:
            show_sep = len(proxies) > 1
            for i, entry in enumerate(proxies):
                info = _fetch_proxy_info(entry.base_url)
                if info is None:
                    if show_sep and i > 0:
                        console.print("[dim]" + "-" * 60 + "[/dim]")
                    console.print(f"\n[dim]{entry.proxy_id}: not reachable at {entry.base_url}[/dim]\n")
                else:
                    _display_metrics(
                        console,
                        entry.proxy_id,
                        entry.base_url,
                        info,
                        show_separator=show_sep and i > 0,
                    )
        return

    if not proxy_id:
        # Default: show the single proxy if exactly one exists
        try:
            proxies = store.list_proxies()
        except ProxyRegistryCorruptedError as e:
            console.print(f"[red]Error:[/red] Proxy registry error: {e}")
            sys.exit(1)
        if len(proxies) == 1:
            proxy_id = proxies[0].proxy_id
        elif len(proxies) == 0:
            console.print("[dim]No proxies registered.[/dim]")
            return
        else:
            console.print("[red]Error:[/red] Multiple proxies exist. Specify a proxy_id or use --all.")
            sys.exit(1)

    try:
        registry = store.read()
    except ProxyRegistryCorruptedError as e:
        console.print(f"[red]Error:[/red] Proxy registry error: {e}")
        sys.exit(1)
    maybe_entry = registry.proxies.get(proxy_id)
    if maybe_entry is None:
        console.print(f"[red]Error:[/red] Proxy '{proxy_id}' not found in registry.")
        sys.exit(1)
    entry = maybe_entry

    info = _fetch_proxy_info(entry.base_url)
    if info is None:
        console.print(f"[dim]Proxy '{proxy_id}' not reachable at {entry.base_url}[/dim]")
        sys.exit(1)

    if json_output:
        console.print(json.dumps(info.metrics, indent=2))
    else:
        _display_metrics(console, proxy_id, entry.base_url, info)


# --- Template subgroup ---


def _extract_template_description(content: str) -> str:
    """Extract description from template YAML comments.

    Shipped templates follow the convention:
        # Template: <name>          <- line 1: skip (repeats name)
        # <description>             <- line 2: use this

    Returns empty string if no suitable comment is found.
    """
    lines = content.splitlines()
    comment_lines = [line.lstrip("# ").strip() for line in lines if line.startswith("#")]
    # Skip the first comment (usually "Template: <name>") and any blank comment lines
    for line in comment_lines[1:]:
        if line:
            return line
    return ""


@proxy.group("template")
def template_group() -> None:
    """Manage proxy templates.

    \b
    Templates define model routing and are used to create proxies.
    User-customized templates are stored at ~/.forge/templates/.

    \b
    Examples:
        forge proxy template list         # List available templates
        forge proxy template show <name>  # Show template config
        forge proxy template edit <name>  # Customize a template
        forge proxy template reset <name> # Reset to built-in default
    """


@template_group.command("list")
def template_list_cmd() -> None:
    """List available proxy templates."""
    console = Console(width=200)

    templates = list_template_names()
    if not templates:
        console.print("[dim]No templates available.[/dim]")
        return

    table = Table(title="Proxy Templates")
    table.add_column("NAME", style="cyan")
    table.add_column("SOURCE")
    table.add_column("DESCRIPTION", style="dim")

    for name in templates:
        user = is_user_template(name)
        shipped = shipped_template_exists(name)
        if user and shipped:
            source = "customized"
        elif user:
            source = "user"
        else:
            source = "built-in"

        try:
            content = read_template(name)
            description = _extract_template_description(content)
        except Exception:
            description = ""

        table.add_row(name, source, description)

    console.print(table)
    console.print("\n[dim]Tip: Run 'forge proxy create <template>' to create a proxy.[/dim]")


@template_group.command("show")
@click.argument("name")
@click.option("--raw", is_flag=True, help="Output raw YAML without syntax highlighting")
def template_show_cmd(name: str, raw: bool) -> None:
    """Show template configuration.

    \b
    Examples:
        forge proxy template show litellm-gemini
        forge proxy template show litellm-gemini --raw
    """
    console = Console(width=200)

    try:
        exists = template_exists(name)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not exists:
        console.print(f"[red]Error:[/red] Template '{name}' not found")
        console.print("\n[dim]Tip: Run 'forge proxy template list' to see available templates.[/dim]")
        sys.exit(1)

    content = read_template(name)
    path = get_template_path(name)

    user = is_user_template(name)
    shipped = shipped_template_exists(name)
    if user and shipped:
        source_label = "customized (overrides built-in)"
    elif user:
        source_label = "user"
    else:
        source_label = "built-in"

    if raw:
        console.print(content)
    else:
        syntax = Syntax(content, "yaml", theme="monokai", line_numbers=True)
        console.print(f"[bold]Template:[/bold] {name}")
        console.print(f"[bold]Source:[/bold] {source_label}")
        console.print(f"[bold]Path:[/bold] {display_path(path)}")
        console.print()
        console.print(syntax)


@template_group.command("edit")
@click.argument("name")
def template_edit_cmd(name: str) -> None:
    """Customize a template (copy-on-first-edit).

    Creates a user copy at ~/.forge/templates/<name>.yaml on first edit.
    Subsequent edits modify the user copy directly.

    \b
    Examples:
        forge proxy template edit litellm-gemini
    """
    console = Console(width=200)

    try:
        validate_template_name(name)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # edit requires a shipped template to seed from
    if not shipped_template_exists(name):
        console.print(f"[red]Error:[/red] No built-in template '{name}' to customize")
        console.print("\n[dim]Tip: Run 'forge proxy template list' to see available templates.[/dim]")
        sys.exit(1)

    user_path = get_user_template_path(name)
    first_edit = not user_path.is_file()

    # Seed temp file from user copy (if exists) or shipped template.
    # The user file is only created/updated after successful validation.
    seed_content = user_path.read_text(encoding="utf-8") if not first_edit else read_shipped_template(name)

    editor = os.environ.get("EDITOR", "vim")
    if not shutil.which(editor):
        console.print(f"[red]Error:[/red] Editor '{editor}' not found. Set $EDITOR to an available editor.")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(seed_content)
        tmp_path = Path(tmp.name)

    success = False
    try:
        result = subprocess.run([editor, str(tmp_path)])
        if result.returncode != 0:
            console.print(f"[red]Error:[/red] Editor exited with code {result.returncode}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        import yaml as pyyaml

        try:
            with open(tmp_path, encoding="utf-8") as f:
                edited_data = pyyaml.safe_load(f)
        except Exception as e:
            console.print(f"[red]Error:[/red] Invalid YAML: {e}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        if not isinstance(edited_data, dict):
            console.print("[red]Error:[/red] Template must be a YAML mapping")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        # Validate template shape (ForgeConfig, not ProxyInstanceConfig)
        try:
            from forge.config.dataclass_utils import dict_to_dataclass
            from forge.config.schema import ForgeConfig

            dict_to_dataclass(ForgeConfig, edited_data, strict=True)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            console.print(f"[red]Error:[/red] Invalid template configuration: {e}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        # Write back atomically (create user dir on first edit)
        from forge.core.state import atomic_write_text

        user_path.parent.mkdir(parents=True, exist_ok=True)
        content = tmp_path.read_text(encoding="utf-8")
        atomic_write_text(user_path, content)

        success = True
        if first_edit:
            console.print(f"[dim]Created user copy at {display_path(user_path)}[/dim]")
        console.print(f"[green]Updated[/green] template '{name}'")

    finally:
        if success and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


@template_group.command("reset")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--force", "-f", is_flag=True, hidden=True, help="Deprecated alias for --yes")
def template_reset_cmd(name: str, yes: bool, force: bool) -> None:
    """Reset a template to built-in defaults.

    Removes the user-customized copy so the shipped template takes effect.

    \b
    Examples:
        forge proxy template reset litellm-gemini
        forge proxy template reset litellm-gemini --yes
    """
    yes = yes or force
    console = Console(width=200)

    try:
        user_path = get_user_template_path(name)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not user_path.is_file():
        console.print(f"[dim]Already using built-in defaults for '{name}'.[/dim]")
        return

    if not yes:
        if shipped_template_exists(name):
            msg = f"Reset template '{name}' to built-in defaults?"
        else:
            console.print(
                f"[yellow]Warning:[/yellow] No built-in template '{name}'. " "This will delete the template entirely."
            )
            msg = f"Delete user template '{name}'?"
        if not click.confirm(msg):
            console.print("[dim]Cancelled.[/dim]")
            return

    user_path.unlink()
    console.print(f"[green]Reset[/green] template '{name}' to built-in defaults")
