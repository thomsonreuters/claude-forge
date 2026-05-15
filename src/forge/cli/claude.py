"""Claude Code management commands.

Commands:
- forge claude start --proxy <id> - Start Claude with specific proxy
- forge claude start --no-proxy   - Start Claude without proxy
- forge claude preset         - Manage settings preset
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import click
import httpx
from rich.console import Console

from forge.core.paths import display_path
from forge.proxy.proxies import (
    ProxyRegistryCorruptedError,
    ProxyRegistryStore,
    ProxyResolutionError,
    resolve_proxy,
)
from forge.session.direct_model import apply_direct_model_env

logger = logging.getLogger(__name__)
console = Console()


def _default_context_limit() -> int:
    from forge.runtime_config import get_runtime_config

    return get_runtime_config().context_limit


def _get_context_limit_for_proxy(proxy_id: str) -> int:
    """Compute context limit from proxy's default tier model.

    Deterministic: uses the specific proxy_id to look up the exact model config,
    unlike the heuristic _get_context_limit_for_template() in session.py which
    picks the first matching template.
    """
    try:
        from forge.config.loader import load_proxy_instance_config
        from forge.core.models import get_context_window_tokens

        proxy_config = load_proxy_instance_config(proxy_id)
        if proxy_config is None:
            logger.debug(f"No proxy config found for {proxy_id}, using default context limit")
            return _default_context_limit()

        tier = proxy_config.default_tier or "sonnet"

        model = proxy_config.tiers.get(tier)
        if not model:
            logger.debug(f"No model found for tier {tier}, using default context limit")
            return _default_context_limit()

        context_limit = get_context_window_tokens(model)
        logger.debug(f"Computed context limit {context_limit} for model {model} (tier {tier})")
        return context_limit

    except Exception as e:
        logger.debug(f"Failed to compute context limit: {e}, using default")
        return _default_context_limit()


def _healthcheck_proxy(*, base_url: str, expected_template: str, expected_proxy_id: str) -> None:
    """Validate proxy is reachable and matches proxy identity."""

    url = base_url.rstrip("/") + "/"

    try:
        response = httpx.get(url, timeout=2.0)
    except httpx.ConnectError:
        raise ValueError(f"proxy is not running (connection refused at {url})")
    except httpx.RequestError as e:
        raise ValueError(f"proxy healthcheck failed at {url}: {e}")

    if response.status_code != 200:
        raise ValueError(f"proxy healthcheck failed at {url}: status {response.status_code}")

    try:
        data = response.json()
    except ValueError as e:
        raise ValueError(f"proxy healthcheck failed at {url}: invalid JSON: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"proxy healthcheck failed at {url}: expected JSON object")

    if data.get("is_proxy") is not True:
        raise ValueError(f"proxy healthcheck failed at {url}: is_proxy is not true")

    template = data.get("template")
    if template != expected_template:
        raise ValueError(
            f"proxy healthcheck failed at {url}: template mismatch (expected '{expected_template}', got '{template}')"
        )

    proxy_block = data.get("proxy")
    if not isinstance(proxy_block, dict):
        raise ValueError(f"proxy healthcheck failed at {url}: missing proxy block")

    actual_proxy_id = proxy_block.get("proxy_id")
    if actual_proxy_id != expected_proxy_id:
        raise ValueError(
            f"proxy healthcheck failed at {url}: proxy_id mismatch (expected '{expected_proxy_id}', got '{actual_proxy_id}')"
        )


# --- Group and Commands ---


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
)
def claude() -> None:
    """Start and manage Claude Code.

    \b
    Examples:
        forge claude start --proxy my-proxy    # Start with specific proxy
        forge claude start --no-proxy           # Start without proxy (direct to Anthropic)
    """
    pass


def _build_bare_launch_env(
    *,
    base_url: str | None,
    template: str | None,
    context_limit: int | None,
) -> tuple[dict[str, str], list[str]]:
    """Build environment for bare Claude launch (no session state).

    Returns (env_vars_to_set, env_vars_to_unset).  Always scrubs session
    identity vars so a nested ``forge claude start`` never inherits a
    parent session.
    """
    env_vars: dict[str, str] = {}
    unset_vars: list[str] = ["FORGE_SESSION", "FORGE_FORK_NAME", "FORGE_PARENT_SESSION"]

    if base_url is None:
        # Direct mode: don't touch CLAUDE_CODE_AUTO_COMPACT_WINDOW — it's a
        # native CC env var the user may have set. Only scrub Forge-managed vars.
        unset_vars.extend(["ANTHROPIC_BASE_URL", "ACTIVE_TEMPLATE"])
    else:
        env_vars["ANTHROPIC_BASE_URL"] = base_url
        if context_limit is not None:
            env_vars["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context_limit)
        if template:
            env_vars["ACTIVE_TEMPLATE"] = template
        else:
            unset_vars.append("ACTIVE_TEMPLATE")

    return env_vars, unset_vars


@claude.command("start")
@click.option(
    "--proxy",
    "proxy_id",
    type=str,
    default=None,
    help="Proxy to use (proxy_id or template name)",
)
@click.option(
    "--no-proxy",
    "direct",
    is_flag=True,
    default=False,
    help="Bypass the proxy and talk to Anthropic directly",
)
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def start_cmd(
    proxy_id: str | None,
    direct: bool,
    claude_args: tuple[str, ...],
) -> None:
    """Start Claude Code with proxy routing or direct to Anthropic.

    Bare launcher: no session state, no FORGE_SESSION.  Use
    ``forge session start`` for managed sessions with lifecycle tracking.

    \b
    Examples:
        forge claude start --proxy my-proxy
        forge claude start --no-proxy
        forge claude start --proxy my-proxy -- --debug
    """
    if direct and proxy_id:
        click.echo("Error: --no-proxy and --proxy are mutually exclusive")
        sys.exit(1)
    if not direct and not proxy_id:
        click.echo("Error: one of --proxy or --no-proxy is required")
        sys.exit(1)

    from forge.session.claude.invoke import invoke_claude

    # Resolve proxy to template + base_url
    template: str | None = None
    base_url: str | None = None
    context_limit: int | None = None
    proxy_display: str | None = None

    if proxy_id:
        proxy_store = ProxyRegistryStore()

        try:
            registry = proxy_store.read()
        except ProxyRegistryCorruptedError as e:
            click.echo(f"Error: {e}")
            sys.exit(1)

        try:
            entry = resolve_proxy(registry, proxy_id)
        except ProxyResolutionError as e:
            click.echo(f"Error: {e}")
            sys.exit(1)

        try:
            _healthcheck_proxy(
                base_url=entry.base_url,
                expected_template=entry.template,
                expected_proxy_id=entry.proxy_id,
            )
        except ValueError as e:
            click.echo(f"Error: {e}")
            if "not running" in str(e):
                click.echo(f"Tip: Run 'forge proxy start {entry.proxy_id}' to start it.")
            sys.exit(1)

        template = entry.template
        base_url = entry.base_url
        context_limit = _get_context_limit_for_proxy(entry.proxy_id)
        proxy_display = entry.proxy_id

    env_vars, unset_vars = _build_bare_launch_env(
        base_url=base_url,
        template=template,
        context_limit=context_limit,
    )

    if direct:
        from forge.runtime_config import get_default_direct_model

        direct_model = get_default_direct_model()
        error = apply_direct_model_env(env_vars, direct_model)
        if error:
            click.echo(f"Error: {error}")
            sys.exit(1)

    if proxy_display:
        console.print(f"Starting Claude with proxy [green]{proxy_display}[/green] ({template})")
    else:
        console.print("Starting Claude [green]direct[/green] (no proxy)")

    from forge.cli.session_addendum import (
        resolve_addendum_content_for_proxy,
        write_bare_addendum,
    )

    addendum_content = resolve_addendum_content_for_proxy(proxy_display)
    addendum_path: Path | None = None
    if addendum_content:
        addendum_path = write_bare_addendum(addendum_content)

    try:
        sys.exit(
            invoke_claude(
                model=None,
                system_prompt_file=str(addendum_path) if addendum_path else None,
                env_vars=env_vars,
                unset_env_vars=unset_vars,
                extra_args=list(claude_args) if claude_args else None,
            )
        )
    finally:
        if addendum_path:
            addendum_path.unlink(missing_ok=True)


# --- Preset subgroup ---


@claude.group("preset")
def preset() -> None:
    """Manage Claude Code settings preset.

    \b
    The preset (~/.forge/claude.preset.json) controls what settings
    Forge merges into Claude Code's settings.json on enable/sync.

    \b
    Examples:
        forge claude preset show         # Show current preset
        forge claude preset edit         # Open in $EDITOR
        forge claude preset reset        # Reset to built-in defaults
    """


@preset.command("show")
@click.option("--raw", is_flag=True, help="Output raw JSON without syntax highlighting")
def preset_show(raw: bool = False) -> None:
    """Show current Claude Code settings preset."""
    from rich.syntax import Syntax

    from forge.install.preset import ensure_preset, get_preset_path

    preset_path = get_preset_path()
    ensure_preset()

    content = preset_path.read_text(encoding="utf-8")

    if raw:
        console.print(content, end="")
    else:
        console.print("[bold]Claude Code Settings Preset[/bold]")
        console.print(f"[bold]Path:[/bold] {display_path(preset_path)}")
        console.print()
        syntax = Syntax(content, "json", theme="monokai")
        console.print(syntax)


@preset.command("edit")
def preset_edit() -> None:
    """Open settings preset in $EDITOR.

    Creates the file with built-in defaults if it doesn't exist.
    Validates JSON before saving.
    """
    import json
    import shutil
    import tempfile

    from forge.install.preset import ensure_preset, get_preset_path

    preset_path = get_preset_path()
    ensure_preset()

    editor = os.environ.get("EDITOR", "vim")
    if not shutil.which(editor):
        console.print(f"[red]Error:[/red] Editor '{editor}' not found. Set $EDITOR to an available editor.")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(preset_path.read_text(encoding="utf-8"))
        tmp_path = Path(tmp.name)

    success = False
    try:
        result = subprocess.run([editor, str(tmp_path)])
        if result.returncode != 0:
            console.print(f"[red]Error:[/red] Editor exited with code {result.returncode}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        try:
            with open(tmp_path, encoding="utf-8") as f:
                edited_data = json.load(f)
        except json.JSONDecodeError as e:
            console.print(f"[red]Error:[/red] Invalid JSON: {e}")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        if not isinstance(edited_data, dict):
            console.print("[red]Error:[/red] Preset must be a JSON object")
            console.print(f"Your changes are saved at: {display_path(tmp_path)}")
            sys.exit(1)

        from forge.core.state import atomic_write_text

        content = json.dumps(edited_data, indent=2) + "\n"
        atomic_write_text(preset_path, content)
        os.chmod(str(preset_path), 0o600)

        success = True
        console.print("[green]Updated[/green] settings preset")

    finally:
        if success and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


@preset.command("reset")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--force", "-f", is_flag=True, hidden=True, help="Deprecated alias for --yes")
def preset_reset(yes: bool, force: bool) -> None:
    """Reset settings preset to built-in defaults."""
    from forge.core.state import atomic_write_text
    from forge.install.preset import get_builtin_preset_json, get_preset_path

    yes = yes or force
    preset_path = get_preset_path()

    if not yes:
        if not click.confirm("Reset preset to built-in defaults?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    preset_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(preset_path, get_builtin_preset_json())
    os.chmod(str(preset_path), 0o600)
    console.print("[green]Reset[/green] preset to built-in defaults")
