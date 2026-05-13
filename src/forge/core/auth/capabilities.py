"""Credential registry and capability metadata.

Single source of truth for Forge credential definitions. Each credential
maps to one or more env vars and describes what features it unlocks.

Dependency direction: this module imports TEMPLATE_SECRETS from
template_secrets.py (one-way). template_secrets.py must NOT import
from this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVar:
    """Metadata for one environment variable within a credential."""

    name: str
    required: bool = True
    secret: bool = True
    connection_value: bool = False
    default_value: str | None = None


@dataclass(frozen=True)
class Credential:
    """A Forge credential with its env vars and capability metadata."""

    name: str
    env_vars: tuple[EnvVar, ...] = ()
    unlocks_features: tuple[str, ...] = ()
    signup_url: str | None = None
    note: str | None = None
    not_needed_for: tuple[str, ...] | None = None


CREDENTIALS: dict[str, Credential] = {
    "openrouter": Credential(
        name="openrouter",
        env_vars=(
            EnvVar("OPENROUTER_API_KEY"),
            EnvVar(
                "OPENROUTER_BASE_URL",
                required=False,
                secret=False,
                connection_value=True,
                default_value="https://openrouter.ai/api/v1",
            ),
        ),
        unlocks_features=("OpenRouter proxy templates", "OSS workflow model workers"),
        signup_url="https://openrouter.ai/keys",
        note="Routes to Claude, GPT, Gemini, DeepSeek, etc. via OpenRouter",
    ),
    "anthropic-api": Credential(
        name="anthropic-api",
        env_vars=(EnvVar("ANTHROPIC_API_KEY"),),
        unlocks_features=(
            "Forge subprocesses (supervisor, handoff agent)",
            "direct Anthropic panel/debate workers",
            "litellm-anthropic-local proxy",
        ),
        signup_url="https://console.anthropic.com/",
        note="Pay-per-token API key. Not Claude Code login.",
        not_needed_for=(
            "forge session start (uses Claude Code's own auth)",
            "Claude via openrouter-anthropic (uses OPENROUTER_API_KEY)",
            "Claude via litellm-anthropic (uses LITELLM_API_KEY)",
        ),
    ),
    "openai-api": Credential(
        name="openai-api",
        env_vars=(EnvVar("OPENAI_API_KEY"),),
        unlocks_features=("litellm-openai-local proxy",),
        signup_url="https://platform.openai.com/api-keys",
        note="OpenAI API key for local LiteLLM proxy routing",
    ),
    "gemini-api": Credential(
        name="gemini-api",
        env_vars=(EnvVar("GEMINI_API_KEY"),),
        unlocks_features=("litellm-gemini-local proxy",),
        signup_url="https://aistudio.google.com/apikey",
        note="Gemini API key for local LiteLLM proxy routing",
    ),
    "litellm-remote": Credential(
        name="litellm-remote",
        env_vars=(
            EnvVar("LITELLM_API_KEY"),
            EnvVar("LITELLM_BASE_URL", secret=False, connection_value=True),
        ),
        unlocks_features=("Remote LiteLLM proxy templates",),
        note="Shared/internal LiteLLM server (team setups)",
    ),
}

RETIRED_NAMES: dict[str, str] = {
    "anthropic": (
        "Unknown credential 'anthropic'. Did you mean 'anthropic-api'?\n"
        "\n"
        "  'anthropic-api' is for Forge subprocess auth (pay-per-token API key).\n"
        "  It is NOT your Claude Code login.\n"
        "\n"
        "  Run: forge auth login -c anthropic-api"
    ),
    "litellm-local": (
        "'litellm-local' is not a credential. It's a setup that uses upstream API keys.\n"
        "\n"
        "  Configure the providers you need:\n"
        "    forge auth login -c gemini-api       # for litellm-gemini-local\n"
        "    forge auth login -c openai-api       # for litellm-openai-local\n"
        "    forge auth login -c anthropic-api    # for litellm-anthropic-local"
    ),
}


def credential_for_env_var(var_name: str) -> Credential | None:
    """Find the credential that owns a given env var name."""
    for cred in CREDENTIALS.values():
        if any(ev.name == var_name for ev in cred.env_vars):
            return cred
    return None


def credentials_for_template(template: str) -> list[Credential]:
    """Which credentials does a template need?

    Bridges TEMPLATE_SECRETS (template -> env var names) to CREDENTIALS
    (credential -> env var metadata) via reverse lookup.
    """
    from forge.core.auth.template_secrets import TEMPLATE_SECRETS

    required_vars = TEMPLATE_SECRETS.get(template, [])
    if not required_vars:
        return []

    seen: set[str] = set()
    result: list[Credential] = []
    for var_name in required_vars:
        cred = credential_for_env_var(var_name)
        if cred and cred.name not in seen:
            seen.add(cred.name)
            result.append(cred)
    return result


def format_missing_credential_error(
    credential: Credential,
    *,
    missing_vars: list[str],
    template: str | None = None,
    context: str | None = None,
    extra_hint: str | None = None,
    profile: str | None = None,
    env_ignored: bool = False,
) -> str:
    """Build an actionable error message for missing credentials.

    Includes what failed, which key(s), signup URL, and the exact
    ``forge auth login`` command. Renders ``not_needed_for`` only for
    anthropic-api (where false urgency is common).
    """
    key_word = "key" if len(missing_vars) == 1 else "keys"
    var_list = ", ".join(missing_vars)

    if context and template:
        header = f"{context} requires {var_list} (template '{template}')."
    elif context:
        header = f"{context} requires {var_list}."
    elif template:
        header = f"Template '{template}' requires {key_word}: {var_list}."
    else:
        header = f"Missing {key_word}: {var_list}."

    lines = [f"Error: {header}"]

    if credential.note:
        lines.append(f"\n  {credential.note}")

    if credential.not_needed_for:
        lines.append("")
        lines.append("  NOT needed for:")
        for item in credential.not_needed_for:
            lines.append(f"    - {item}")

    unlocks = credential.unlocks_features
    if unlocks:
        lines.append(f"\n  Unlocks: {', '.join(unlocks)}")

    if credential.signup_url:
        lines.append(f"  Get one at {credential.signup_url}")

    login_cmd = f"forge auth login -c {credential.name}"
    if profile:
        login_cmd += f" --profile {profile}"
    lines.append(f"  Tip: Run '{login_cmd}' to configure.")

    if extra_hint:
        lines.append(f"       {extra_hint}")

    if env_ignored:
        present_in_env = [v for v in missing_vars if _env_has(v)]
        if present_in_env:
            env_list = ", ".join(present_in_env)
            verb = "is" if len(present_in_env) == 1 else "are"
            pronoun = "it" if len(present_in_env) == 1 else "them"
            lines.append(
                f"\n  Note: {env_list} {verb} set in env but auth_ignore_env is active."
                f"\n  Run 'forge config set auth_ignore_env=false' to use {pronoun}."
            )

    return "\n".join(lines)


def _env_has(var_name: str) -> bool:
    """Check if an env var is set (for env_ignored diagnostic only)."""
    import os

    return bool(os.environ.get(var_name))
