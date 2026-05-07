"""Authentication CLI commands.

Provides `forge authentication login` for storing credentials in ~/.forge/credentials.yaml,
`forge authentication status` to check credential source per provider, `forge authentication logout`
to remove stored credentials, and `forge authentication profiles` to list saved profiles.

Usage:
    forge authentication login                                  # Prompt for all providers
    forge authentication login --provider anthropic              # Prompt for single provider
    forge authentication login --provider anthropic --profile work  # Store in named profile
    forge authentication status                                 # Show credential sources
    forge authentication logout --profile default               # Remove stored credentials
    forge authentication profiles                               # List saved profiles
"""

from __future__ import annotations

import click

from forge.core.auth import EnvSecretsProvider
from forge.core.auth.credentials_file import (
    CredentialVersionError,
    delete_profile,
    list_profiles,
    load_profile,
    resolve_profile,
    save_profile,
)

# Provider specifications: required and optional credentials/connection values.
#
# Keys cover: Forge client credentials (e.g., LITELLM_API_KEY), sidecar upstream keys
# (e.g., GEMINI_API_KEY), and connection values (e.g., LITELLM_BASE_URL) stored as a
# convenience fallback for proxy bootstrapping.
PROVIDERS = {
    "litellm-remote": {
        "required": ["LITELLM_API_KEY", "LITELLM_BASE_URL"],
        "optional": [],
        "description": "Remote LiteLLM gateway",
    },
    "litellm-local": {
        "required": [],
        "optional": ["GEMINI_API_KEY", "OPENAI_API_KEY", "LITELLM_LOCAL_API_KEY"],
        "description": "Local LiteLLM instance (upstream provider keys + client auth)",
    },
    "anthropic": {
        "required": ["ANTHROPIC_API_KEY"],
        "optional": [],
        "description": "Direct Anthropic API",
    },
    "openrouter": {
        "required": ["OPENROUTER_API_KEY"],
        "optional": ["OPENROUTER_BASE_URL"],
        "description": "OpenRouter multi-provider gateway",
    },
}

# Keys that should be hidden during prompt input
_SENSITIVE_PATTERNS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD")


def _is_sensitive(key: str) -> bool:
    return any(p in key.upper() for p in _SENSITIVE_PATTERNS)


def _mask_value(value: str) -> str:
    """Mask all but last 4 chars of a secret value."""
    if len(value) <= 8:
        return "****"
    return value[:4] + "…" + value[-4:]


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
    "--provider",
    "-p",
    type=click.Choice(list(PROVIDERS.keys())),
    help="Provider to configure. If not specified, prompts for all providers",
)
@click.option(
    "--profile",
    default=None,
    help="Profile name to store credentials in (default: 'default' or FORGE_PROFILE)",
)
def login(provider: str | None, profile: str | None) -> None:
    """Store credentials for LLM providers.

    Prompts for API keys and stores them in ~/.forge/credentials.yaml.
    Press Enter to keep the existing value (shown as masked default).

    \b
    Examples:
        forge authentication login                              # All providers
        forge authentication login --provider anthropic          # Single provider
        forge authentication login -p anthropic --profile work   # Named profile
    """
    profile_name = resolve_profile(profile)

    if provider is not None:
        providers_to_configure = [provider]
    else:
        providers_to_configure = list(PROVIDERS.keys())

    try:
        existing = load_profile(profile_name)
    except CredentialVersionError as e:
        click.secho(f"✗ {e}", fg="red")
        raise SystemExit(1)
    except ValueError:
        existing = {}
        click.secho("⚠︎ Existing credentials file is corrupt — starting fresh.", fg="yellow")
    collected: dict[str, str] = {}

    click.echo(f"\nConfiguring credentials for profile '{profile_name}'")
    click.echo("-" * 40)

    for prov_name in providers_to_configure:
        spec = PROVIDERS[prov_name]
        all_keys = list(spec["required"]) + list(spec["optional"])
        if not all_keys:
            continue

        click.echo(f"\n{prov_name}: {spec['description']}")

        for key in all_keys:
            current = existing.get(key, "")
            env_value = EnvSecretsProvider().get(key)

            # Build prompt with context
            if current:
                default_display = _mask_value(current)
                prompt_text = f"  {key} [{default_display}]"
            else:
                prompt_text = f"  {key}"

            # Show env override hint
            if env_value:
                click.secho("    (env override active)", fg="cyan")

            value = click.prompt(
                prompt_text,
                default="",
                show_default=False,
                hide_input=_is_sensitive(key),
            )

            if value:
                collected[key] = value
            elif current:
                collected[key] = current

    if collected:
        path = save_profile(profile_name, collected, merge=True)
        click.echo()
        click.secho(
            f"✓ Credentials saved to {path} (profile: {profile_name})",
            fg="green",
        )
        click.echo("Tip: Use 'forge authentication status' to verify.")
    else:
        click.echo("\nNo credentials to save.")


@auth.command("status")
@click.option(
    "--profile",
    default=None,
    help="Profile to check (default: 'default' or FORGE_PROFILE)",
)
def status(profile: str | None) -> None:
    """Show credential status per provider with source attribution.

    Displays where each credential comes from: environment variable,
    credential file, or missing.

    \b
    Examples:
        forge authentication status
        forge authentication status --profile work
    """
    profile_name = resolve_profile(profile)
    env = EnvSecretsProvider()
    try:
        file_secrets = load_profile(profile_name)
    except CredentialVersionError as e:
        click.secho(f"✗ {e}", fg="red")
        raise SystemExit(1)
    except ValueError:
        file_secrets = {}
        click.secho("⚠︎ Credentials file is corrupt — file-based values unavailable.", fg="yellow")
        click.echo("Tip: Run 'forge authentication login' to recreate the file.")

    click.echo(f"\nCredential status (profile: {profile_name})")
    click.echo("=" * 50)

    any_missing = False
    for prov_name, spec in PROVIDERS.items():
        all_keys = list(spec["required"]) + list(spec["optional"])
        if not all_keys:
            continue

        click.echo(f"\n{prov_name}: {spec['description']}")
        click.echo("-" * 40)

        for key in all_keys:
            is_required = key in spec["required"]
            env_val = env.get(key)
            file_val = file_secrets.get(key)

            if env_val:
                display = _mask_value(env_val) if _is_sensitive(key) else env_val
                click.secho(f"  ✓ {key} = {display}  (env)", fg="green")
            elif file_val:
                display = _mask_value(file_val) if _is_sensitive(key) else file_val
                click.secho(f"  ✓ {key} = {display}  (file:{profile_name})", fg="green")
            elif is_required:
                click.secho(f"  ✗ {key}  MISSING", fg="red")
                any_missing = True
            else:
                click.secho(f"  ○ {key}  not set (optional)", fg="yellow")

    click.echo()
    if any_missing:
        click.echo("Tip: Run 'forge auth login' to add missing credentials.")


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
        click.echo("\nTip: Run 'forge authentication login' to recreate the file.")
        raise SystemExit(1)

    if not profile_names:
        click.echo("No profiles found.")
        click.echo("\nTip: Run 'forge authentication login' to create one.")
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
