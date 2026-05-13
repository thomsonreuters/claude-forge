"""Authentication CLI commands.

Provides ``forge authentication login`` for storing credentials in
``~/.forge/credentials.yaml``, ``forge authentication status`` to check
credential status, ``forge authentication logout`` to remove stored
credentials, and ``forge authentication profiles`` to list saved profiles.

Usage:
    forge authentication login                            # Credential selection menu
    forge authentication login -c anthropic-api           # Single credential
    forge authentication login -c anthropic-api --profile work
    forge authentication status                           # Dual-view status
    forge authentication logout --profile default         # Remove stored credentials
    forge authentication profiles                         # List saved profiles
"""

from __future__ import annotations

import logging
import os

import click

from forge.core.auth.capabilities import (
    CREDENTIALS,
    RETIRED_NAMES,
    Credential,
    EnvVar,
)
from forge.core.auth.credentials_file import (
    CredentialVersionError,
    delete_profile,
    list_profiles,
    load_profile,
    resolve_profile,
    save_profile,
)

_log = logging.getLogger(__name__)


def _mask_value(value: str) -> str:
    """Mask all but first/last 4 chars of a secret value."""
    if len(value) <= 8:
        return "****"
    return value[:4] + "…" + value[-4:]


def _resolve_var_source(
    ev: EnvVar,
    file_secrets: dict[str, str],
    ignore_env: bool,
) -> tuple[str | None, str]:
    """Resolve a single env var's value and source label.

    Returns (value_or_None, source_label).
    """
    env_val = os.environ.get(ev.name)
    file_val = file_secrets.get(ev.name)

    if ignore_env:
        if file_val:
            return file_val, "file"
        if env_val:
            return None, "not configured (env ignored)"
        return None, "not configured"

    if env_val:
        return env_val, "env"
    if file_val:
        return file_val, "file"
    return None, "not configured"


def _credential_state(
    cred: Credential,
    file_secrets: dict[str, str],
    ignore_env: bool,
    profile_name: str,
) -> str:
    """Compute aggregate configuration state for a credential.

    Returns one of: "configured (env)", "configured (file)", "configured (env+file)",
    "partially configured", "not configured", "not configured (env ignored)".
    """
    sources: set[str] = set()
    any_missing = False
    env_ignored_present = False

    for ev in cred.env_vars:
        if not ev.required:
            continue
        _, source = _resolve_var_source(ev, file_secrets, ignore_env)
        if source == "not configured":
            any_missing = True
        elif source == "not configured (env ignored)":
            any_missing = True
            env_ignored_present = True
        else:
            sources.add(source)

    if not sources:
        if env_ignored_present:
            return "not configured (env ignored)"
        return "not configured"
    if any_missing:
        return "partially configured"

    if sources == {"env"}:
        return "configured (env)"
    if sources == {"file"}:
        return f"configured (file:{profile_name})"
    if sources == {"env", "file"}:
        return "configured (env+file)"
    return "configured"


def _capability_summary(cred: Credential) -> str:
    """One-line capability description for the credential menu."""
    features = ", ".join(cred.unlocks_features)
    if features and cred.note:
        return f"{features} ({cred.note})"
    if features:
        return features
    if cred.note:
        return cred.note
    return cred.name


# ── Click commands ────────────────────────────────────────────────


@click.group()
def auth() -> None:
    """Manage authentication and credentials.

    \b
    Examples:
        forge authentication login               # Store credentials
        forge authentication status              # Check credential sources
        forge authentication profiles            # List saved profiles
    """
    pass


@auth.command("login")
@click.option(
    "--credential",
    "-c",
    "--provider",
    "-p",
    "credential",
    type=str,
    default=None,
    help="Credential to configure (e.g. openrouter, anthropic-api, gemini-api)",
)
@click.option(
    "--profile",
    default=None,
    help="Profile name to store credentials in (default: 'default' or FORGE_PROFILE)",
)
def login(credential: str | None, profile: str | None) -> None:
    """Store credentials for Forge proxy routing and subprocesses.

    These are for Forge, NOT your Claude Code login (OAuth/Max plan).
    Press Enter to keep the existing value or skip env-provided keys.

    \b
    Examples:
        forge auth login                            # Credential selection menu
        forge auth login -c anthropic-api           # Single credential
        forge auth login -c openrouter --profile work
    """
    profile_name = resolve_profile(profile)

    # Validate credential name
    if credential is not None:
        if credential in RETIRED_NAMES:
            click.secho(RETIRED_NAMES[credential], fg="yellow", err=True)
            raise SystemExit(1)
        if credential not in CREDENTIALS:
            click.secho(f"Unknown credential '{credential}'.", fg="red", err=True)
            click.echo(f"Available: {', '.join(CREDENTIALS)}", err=True)
            raise SystemExit(1)

    ignore_env = _get_auth_ignore_env()

    try:
        existing = load_profile(profile_name)
    except CredentialVersionError as e:
        click.secho(f"✗ {e}", fg="red")
        raise SystemExit(1)
    except ValueError:
        existing = {}
        click.secho("⚠︎ Existing credentials file is corrupt -- starting fresh.", fg="yellow")

    # Select which credentials to configure
    if credential is not None:
        to_configure = [CREDENTIALS[credential]]
    else:
        to_configure = _credential_menu(existing, ignore_env, profile_name)
        if not to_configure:
            return

    # Prompt for each credential's env vars
    collected: dict[str, str] = {}

    click.echo(f"\nConfiguring credentials for profile '{profile_name}'")
    click.echo("-" * 40)

    for cred in to_configure:
        _prompt_credential(cred, existing, collected, ignore_env)

    if collected:
        path = save_profile(profile_name, collected, merge=True)
        click.echo()
        click.secho(
            f"✓ Credentials saved to {path} (profile: {profile_name})",
            fg="green",
        )
        click.echo("Tip: Use 'forge auth status' to verify.")
    else:
        click.echo("\nNo credentials to save.")


def _credential_menu(
    file_secrets: dict[str, str],
    ignore_env: bool,
    profile_name: str,
) -> list[Credential]:
    """Show numbered credential selection menu. Returns selected credentials."""
    click.echo("\nForge credentials")
    click.echo("These are for Forge proxy routing and subprocesses, NOT your Claude Code login.")
    click.echo("Claude Code authenticates separately (OAuth, Max plan, etc.).\n")

    cred_list = list(CREDENTIALS.values())
    for i, cred in enumerate(cred_list, 1):
        state = _credential_state(cred, file_secrets, ignore_env, profile_name)
        marker = "*" if state.startswith("configured") else "-"
        summary = _capability_summary(cred)
        click.echo(f"  [{i}] {cred.name:<18} {marker} {state:<28} {summary}")

    click.echo()
    raw = click.prompt(
        f"Select credentials [1-{len(cred_list)}, comma-separated, or 'all']",
        default="all",
        show_default=True,
    )

    if raw.strip().lower() == "all":
        return cred_list

    selected: list[Credential] = []
    for part in raw.split(","):
        part = part.strip()
        try:
            idx = int(part) - 1
            if 0 <= idx < len(cred_list):
                selected.append(cred_list[idx])
        except ValueError:
            click.secho(f"Ignoring invalid selection: {part}", fg="yellow")

    return selected


def _prompt_credential(
    cred: Credential,
    existing: dict[str, str],
    collected: dict[str, str],
    ignore_env: bool,
) -> None:
    """Prompt for a single credential's env vars."""
    header = f"\n{cred.name}"
    if cred.note:
        header += f": {cred.note}"
    click.echo(header)

    if cred.not_needed_for:
        click.echo()
        for item in cred.not_needed_for:
            click.echo(f"  NOT needed for: {item}")
        click.echo()

    for ev in cred.env_vars:
        _prompt_env_var(ev, existing, collected, ignore_env)


def _prompt_env_var(
    ev: EnvVar,
    existing: dict[str, str],
    collected: dict[str, str],
    ignore_env: bool,
) -> None:
    """Prompt for a single env var with env-aware skip behavior."""
    current = existing.get(ev.name, "")
    raw_env_value = os.environ.get(ev.name)
    env_value = None if ignore_env else raw_env_value

    if ignore_env and raw_env_value:
        display = _mask_value(raw_env_value) if ev.secret else raw_env_value
        click.echo(f"  {ev.name}: set in environment ({display}) but auth_ignore_env is active.")
        click.echo("  Enter a value for the credential file, or press Enter to skip.")
        prompt_text = f"  {ev.name} [skip]"
    elif env_value:
        display = _mask_value(env_value) if ev.secret else env_value
        click.echo(f"  {ev.name}: already set via environment variable ({display})")
        click.echo("  Storing in credential file is optional (env var takes precedence).")
        prompt_text = f"  {ev.name} [skip]"
    elif current:
        default_display = _mask_value(current) if ev.secret else current
        prompt_text = f"  {ev.name} [{default_display}]"
    elif ev.default_value:
        click.echo(f"  {ev.name}: default is {ev.default_value}")
        prompt_text = f"  {ev.name} [skip]"
    else:
        prompt_text = f"  {ev.name}"

    value = click.prompt(
        prompt_text,
        default="",
        show_default=False,
        hide_input=ev.secret,
    )

    if value:
        collected[ev.name] = value
    elif current:
        collected[ev.name] = current


@auth.command("status")
@click.option(
    "--profile",
    default=None,
    help="Profile to check (default: 'default' or FORGE_PROFILE)",
)
def status(profile: str | None) -> None:
    """Show credential status with capability summary and source details.

    \b
    Examples:
        forge authentication status
        forge authentication status --profile work
    """
    profile_name = resolve_profile(profile)

    ignore_env = _get_auth_ignore_env()

    try:
        file_secrets = load_profile(profile_name)
    except CredentialVersionError as e:
        click.secho(f"✗ {e}", fg="red")
        raise SystemExit(1)
    except ValueError:
        file_secrets = {}
        click.secho("⚠︎ Credentials file is corrupt -- file-based values unavailable.", fg="yellow")
        click.echo("Tip: Run 'forge auth login' to recreate the file.")

    click.echo(f"\nCredential status (profile: {profile_name})")
    click.echo("=" * 50)

    # Section 1: Capability summary
    configured: list[str] = []
    not_configured: list[str] = []

    for cred in CREDENTIALS.values():
        state = _credential_state(cred, file_secrets, ignore_env, profile_name)
        summary = _capability_summary(cred)
        if state.startswith("configured"):
            # Find primary source for display
            primary_source = state.split("(", 1)[1].rstrip(")") if "(" in state else ""
            configured.append(f"  * {cred.name:<18} {summary}  ({primary_source})")
        else:
            not_configured.append(f"  - {cred.name:<18} {summary}  ({state})")

    if configured:
        click.echo("\nConfigured capabilities:")
        for line in configured:
            click.secho(line, fg="green")

    if not_configured:
        click.echo("\nNot configured (set up if needed):")
        for line in not_configured:
            click.echo(line)

    # Section 2: Credential details
    click.echo("\nCredential details:")

    for cred in CREDENTIALS.values():
        click.echo(f"\n  {cred.name}")

        for ev in cred.env_vars:
            value, source = _resolve_var_source(ev, file_secrets, ignore_env)
            if value:
                display = _mask_value(value) if ev.secret else value
                source_label = f"file:{profile_name}" if source == "file" else source
                click.secho(f"    * {ev.name} = {display}  ({source_label})", fg="green")
            elif ev.default_value and source == "not configured":
                click.echo(f"    - {ev.name} = {ev.default_value}  (default)")
            else:
                click.echo(f"    - {ev.name}  {source}")

    click.echo()


@auth.command("logout")
@click.option(
    "--profile",
    default=None,
    help="Profile to remove credentials from (default: 'default' or FORGE_PROFILE)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--force", "-f", is_flag=True, hidden=True, help="Deprecated alias for --yes")
def logout(profile: str | None, yes: bool, force: bool) -> None:
    """Remove stored credentials for a profile.

    Deletes the profile from ~/.forge/credentials.yaml.
    Environment variables are not affected.

    \b
    Examples:
        forge authentication logout
        forge authentication logout --profile work
        forge authentication logout -y  # Skip confirmation
    """
    yes = yes or force
    profile_name = resolve_profile(profile)

    if not yes:
        if not click.confirm(f"Remove stored credentials for profile '{profile_name}'?"):
            click.echo("Aborted.")
            return

    if delete_profile(profile_name):
        click.secho(f"✓ Removed profile '{profile_name}'", fg="green")
    else:
        click.echo(f"Profile '{profile_name}' not found (nothing to remove).")


@auth.command("profiles")
def profiles_cmd() -> None:
    """List saved credential profiles.

    \b
    Examples:
        forge authentication profiles
    """
    try:
        profile_names = list_profiles()
    except CredentialVersionError as e:
        click.secho(f"✗ {e}", fg="red")
        raise SystemExit(1)
    except ValueError as e:
        click.secho(f"Error reading credentials file: {e}", fg="red")
        click.echo("\nTip: Run 'forge auth login' to recreate the file.")
        raise SystemExit(1)

    if not profile_names:
        click.echo("No profiles found.")
        click.echo("\nTip: Run 'forge auth login' to create one.")
        return

    active = resolve_profile()

    click.echo(f"\nSaved profiles ({len(profile_names)}):")
    click.echo("-" * 30)

    for name in profile_names:
        secrets = load_profile(name)
        key_count = len(secrets)
        marker = " ← active" if name == active else ""
        click.echo(f"  {name} ({key_count} keys){marker}")

    click.echo()


def _get_auth_ignore_env() -> bool:
    """Read auth_ignore_env from runtime config."""
    try:
        from forge.runtime_config import get_runtime_config

        return get_runtime_config().auth_ignore_env
    except Exception as e:
        _log.debug("Could not read auth_ignore_env; using environment credentials: %s", e)
        return False
