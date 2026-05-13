# Auth UX Redesign — Credential-Oriented Setup

**Status**: Proposal. Iterating on design.

**Context**: Forge auth has outgrown "provider -> env vars." Users need to know "what can I do after setting this?" not
"which opaque key bucket am I filling?" The current `forge authentication login` walks through 4 providers sequentially
with no preview, no skip, and no indication of what each credential unlocks.

---

## Problem

1. **No upfront visibility**: User doesn't know what's coming before prompts start.
2. **No skip**: Can't opt out of credentials they don't need without Ctrl+C.
3. **"Direct mode" is overloaded**: Two unrelated things are both called "direct":
   - `forge session start` (no `--proxy`) — uses Claude Code's own auth (OAuth, Max plan). Forge needs nothing.
   - `claude -p --bare` (Forge subprocess) — uses `ANTHROPIC_API_KEY` env var. `--bare` skips OAuth, so the API key is
     mandatory. This is what supervisor, handoff agent, and direct panel workers use.
4. **Anthropic confusion**: Three different ways to use Claude models, each with different auth:
   - Claude Code's own auth (direct session) — not Forge's concern
   - `ANTHROPIC_API_KEY` (Forge subprocesses via `claude -p --bare`) — Forge auth
   - `OPENROUTER_API_KEY` or `LITELLM_API_KEY` (proxy routing to Claude via OpenRouter/LiteLLM) — different key
5. **`litellm-local` is not really a provider**: It's a capability group that consumes upstream API keys
   (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Presenting it as a peer of `openrouter` or `anthropic` is
   confusing — it doesn't have its own key, it borrows others.
6. **No capability context**: User sees env var names, not what features they unlock.
7. **Poor enforcement errors**: Missing credentials produce cryptic failures at use time, not actionable messages with
   signup URLs and `forge auth login` hints.

### Auth path matrix

Five ways to use "Anthropic/Claude" — each with different auth:

| Action                                | Auth needed                       | Who manages it | Forge credential? |
| ------------------------------------- | --------------------------------- | -------------- | ----------------- |
| `forge session start` (no `--proxy`)  | Claude Code login (OAuth/Max/key) | Claude Code    | No                |
| `claude -p --bare` (Forge subprocess) | `ANTHROPIC_API_KEY`               | Forge env      | Yes               |
| Proxy via `openrouter-anthropic`      | `OPENROUTER_API_KEY`              | Forge proxy    | Yes               |
| Proxy via `litellm-anthropic`         | `LITELLM_API_KEY`                 | Forge proxy    | Yes               |
| Proxy via `litellm-anthropic-local`   | `ANTHROPIC_API_KEY`               | Local LiteLLM  | Yes               |

**Key implication**: A user with no `ANTHROPIC_API_KEY` can still:

- Run `forge session start` (Claude Code uses its own auth)
- Run workflows via `openrouter-anthropic` (different key)
- Use any non-Anthropic proxy

But they **cannot** run supervisor, handoff agent, or direct Claude panel workers (these spawn `claude -p --bare`).

**Subprocess proxy escape hatch**: `--subprocess-proxy` routes child jobs through a proxy instead of `claude -p --bare`,
so users without `ANTHROPIC_API_KEY` can still get subprocess features if they have any proxy configured.

## Design

### Core model: credentials, not providers

The current model groups by **provider** (`litellm-local` → 3 keys). The new model treats each API key as a first-class
**credential** with its own capabilities:

| Credential       | Env var                                | What it unlocks                                                    |
| ---------------- | -------------------------------------- | ------------------------------------------------------------------ |
| `openrouter`     | `OPENROUTER_API_KEY`                   | All `openrouter-*` proxy templates, OSS workflow models            |
| `anthropic-api`  | `ANTHROPIC_API_KEY`                    | Forge subprocesses (`claude -p --bare`), `litellm-anthropic-local` |
| `openai-api`     | `OPENAI_API_KEY`                       | `litellm-openai-local` proxy                                       |
| `gemini-api`     | `GEMINI_API_KEY`                       | `litellm-gemini-local` proxy                                       |
| `litellm-remote` | `LITELLM_API_KEY` + `LITELLM_BASE_URL` | All remote `litellm-*` proxy templates                             |

Optional/rare:

| Credential             | Env var                 | What it unlocks                                   |
| ---------------------- | ----------------------- | ------------------------------------------------- |
| `litellm-local-client` | `LITELLM_LOCAL_API_KEY` | Client auth to local LiteLLM (usually not needed) |
| `openrouter-url`       | `OPENROUTER_BASE_URL`   | Custom OpenRouter endpoint (rare)                 |

`litellm-local` disappears as a top-level auth provider. It was never one — it was a setup path that consumed upstream
keys. Users who want local LiteLLM + Gemini configure the `gemini-api` credential. Users who want local LiteLLM + OpenAI
configure `openai-api`. The `litellm-*-local` templates resolve keys from these atomic credentials.

**No credential name aliases.** Old names (`anthropic`, `litellm-local`) are not silently accepted — they produce a
clear error with migration guidance (see "Retired names" below). The `--provider/-p` flag is kept as an alias for
`--credential/-c` (flag rename only), but the credential *values* must be the new canonical names.

### 1. Credential selection menu

When `forge auth login` is called without `--credential`/`--provider`, show a capability-oriented menu with three-state
source attribution:

```
Forge credentials
These are for Forge proxy routing and subprocesses, NOT your Claude Code login.
Claude Code authenticates separately (OAuth, Max plan, etc.).

  [1] openrouter       * configured (env)    All openrouter-* proxies, OSS workflow models
  [2] anthropic-api    * configured (env)    Forge subprocesses, litellm-anthropic-local
  [3] openai-api       - not configured      litellm-openai-local proxy
  [4] gemini-api       * configured (file)   litellm-gemini-local proxy
  [5] litellm-remote   - not configured      Shared/remote LiteLLM server

Select credentials [1-5, comma-separated, or 'all'] (default: all):
```

Five states for multi-var credentials like `litellm-remote`:

| State                   | Meaning                                           |
| ----------------------- | ------------------------------------------------- |
| `configured (env)`      | All required vars set via environment             |
| `configured (file)`     | All required vars set via credential file         |
| `configured (env+file)` | Mixed sources (e.g., key from env, URL from file) |
| `partially configured`  | Some required vars set, others missing            |
| `not configured`        | No required vars set                              |

When `auth.ignore_env` is active and env vars exist but are being skipped, credentials that were only in env show
`not configured (env ignored)` — not `configured (file)` (which would be misleading).

Single-var credentials (most of them) only see `configured (env)`, `configured (file)`, or `not configured`.

No nesting, no sub-menus. Enter = all.

### 2. Env-aware prompting

When prompting for a key that's already set via env var, indicate that storing it is optional:

```
anthropic-api: Anthropic API key (pay-per-token)

  NOT your Claude Code login -- that's separate (OAuth/Max plan) and still works.
  NOT needed for Claude via OpenRouter or LiteLLM -- those use their own keys.

  This key is for:
    - Forge subprocesses (supervisor, handoff agent, direct panel workers)
    - litellm-anthropic-local proxy template

  ANTHROPIC_API_KEY: already set via environment variable
  Storing in credential file is optional (env var takes precedence).
  Press Enter to skip, or enter a value to store as fallback.

  ANTHROPIC_API_KEY [skip]: ____
```

This avoids two problems: user stores a duplicate thinking they need to, or overwrites an env-managed key with a stale
value in the credential file.

### 3. Ignoring env vars (`auth.ignore_env`)

Sometimes a user has `ANTHROPIC_API_KEY` set in their shell for Claude Code but wants Forge to use a different key from
the credential file (e.g., a separate billing account for subprocess work). A boolean runtime config setting tells Forge
to skip all env vars for credential resolution:

```yaml
# ~/.forge/config.yaml
auth:
  ignore_env: true    # Use credential file only, ignore shell env vars
```

When `ignore_env` is true, the resolution chain skips `EnvSecretsProvider` entirely and resolves from the credential
file only. The menu and status show `configured (file)` for all credentials. Per-machine (runtime config), not
per-session or per-proxy.

**Tip in error/status output**: When env vars are present but Forge subprocesses fail (e.g., the env key is for a
different account), the error message includes:

```
Tip: If your shell API keys are for Claude Code (not Forge),
     run: forge config set auth.ignore_env true
     Then store Forge keys with: forge auth login
```

### 3. Capability registry (`forge.core.auth.capabilities`)

New module — the single source of truth for credential metadata. `template_secrets.py` becomes a thin wrapper that maps
templates to credentials defined here.

```python
# src/forge/core/auth/capabilities.py

@dataclass(frozen=True)
class EnvVar:
    name: str                              # e.g. "ANTHROPIC_API_KEY"
    required: bool = True                  # Required for this credential to work
    secret: bool = True                    # Mask in display (API keys = True)
    connection_value: bool = False         # URL/endpoint, not a secret

# Examples:
#   EnvVar("ANTHROPIC_API_KEY")                                    → required, masked
#   EnvVar("LITELLM_BASE_URL", secret=False, connection_value=True) → required, shown plaintext
#   EnvVar("OPENROUTER_BASE_URL", required=False, secret=False, connection_value=True)  → optional URL

@dataclass(frozen=True)
class Credential:
    name: str                              # e.g. "anthropic-api"
    env_vars: list[EnvVar]                 # Structured env var metadata
    unlocks_features: list[str]            # Non-proxy capabilities
    signup_url: str | None                 # Where to get the key
    note: str | None                       # Disambiguation text
    not_needed_for: list[str] | None       # False-urgency reduction (anthropic-api only)

    # unlocks_proxy is NOT manually maintained — derived from template_secrets mapping
    # and validated by tests (see "Template coverage" below)

CREDENTIALS: dict[str, Credential] = {
    "openrouter": Credential(
        name="openrouter",
        env_vars=[
            EnvVar("OPENROUTER_API_KEY"),
            EnvVar("OPENROUTER_BASE_URL", required=False, secret=False, connection_value=True),
        ],
        unlocks_features=["OSS workflow model workers"],
        signup_url="https://openrouter.ai/keys",
        note="Routes to Claude, GPT, Gemini, DeepSeek, etc. via OpenRouter",
        not_needed_for=None,
    ),
    "anthropic-api": Credential(
        name="anthropic-api",
        env_vars=[EnvVar("ANTHROPIC_API_KEY")],
        unlocks_features=["supervisor", "handoff agent", "direct panel/debate workers"],
        signup_url="https://console.anthropic.com/",
        note="Pay-per-token API key. Not Claude Code login.",
        not_needed_for=[
            "forge session start (uses Claude Code's own auth)",
            "Claude via openrouter-anthropic (uses OPENROUTER_API_KEY)",
            "Claude via litellm-anthropic (uses LITELLM_API_KEY)",
        ],
    ),
    "openai-api": Credential(
        name="openai-api",
        env_vars=[EnvVar("OPENAI_API_KEY")],
        unlocks_features=[],
        signup_url="https://platform.openai.com/api-keys",
        note=None,
        not_needed_for=None,
    ),
    "gemini-api": Credential(
        name="gemini-api",
        env_vars=[EnvVar("GEMINI_API_KEY")],
        unlocks_features=[],
        signup_url="https://aistudio.google.com/apikey",
        note=None,
        not_needed_for=None,
    ),
    "litellm-remote": Credential(
        name="litellm-remote",
        env_vars=[
            EnvVar("LITELLM_API_KEY"),
            EnvVar("LITELLM_BASE_URL", secret=False, connection_value=True),
        ],
        unlocks_features=[],
        signup_url=None,
        note="Shared/internal LiteLLM server (team setups)",
        not_needed_for=None,
    ),
}


def credentials_for_template(template: str) -> list[Credential]:
    """Which credentials does this template need? Returns list (most templates need one)."""
    ...

def credential_for_env_var(var_name: str) -> Credential | None:
    """Which credential owns this env var?"""
    ...

def format_missing_credential_error(
    credential: Credential,
    *,
    missing_vars: list[str],          # Which specific vars are missing (may be subset of credential.env_vars)
    template: str | None = None,      # Template that triggered the error (for proxy creation failures)
    context: str | None = None,       # What was trying to use the credential ("Supervisor", "Handoff agent")
    extra_hint: str | None = None,    # Additional guidance ("Or use --subprocess-proxy to route through a proxy")
    profile: str | None = None,       # Active profile for exact --profile hint in remediation command
    env_ignored: bool = False,        # Whether auth.ignore_env is active (adds diagnostic note)
) -> str:
    """Actionable error message. Includes not_needed_for only for anthropic-api.

    For litellm-remote, names which var is missing (LITELLM_API_KEY vs LITELLM_BASE_URL vs both).
    When profile is set, hint says 'forge auth login -c X --profile Y' instead of just 'forge auth login -c X'.
    When env_ignored is True and the env var exists, adds: 'Note: ANTHROPIC_API_KEY is set in env but
    auth.ignore_env is active. Run forge config set auth.ignore_env false to use it.'
    """
    ...
```

#### Template coverage (test-derived, not manually maintained)

`unlocks_proxy` is **not** a field on `Credential`. Instead, the template-to-credential mapping is derived from
`template_secrets.py` (which maps templates to env vars) and the `Credential.env_vars` registry. A test validates that
every shipped template resolves to the expected credential(s):

```python
# tests/src/core/auth/test_capabilities.py
from pathlib import Path

TEMPLATE_DIR = Path("src/forge/config/defaults/templates")

def _shipped_template_names() -> list[str]:
    """Scan actual template files — catches templates missing from TEMPLATE_SECRETS."""
    return [p.stem for p in TEMPLATE_DIR.glob("*.yaml")]

def test_every_shipped_template_has_secrets():
    """Every template YAML file must appear in TEMPLATE_SECRETS."""
    for name in _shipped_template_names():
        assert name in TEMPLATE_SECRETS, f"Template '{name}' has no entry in TEMPLATE_SECRETS"

def test_every_template_maps_to_credential():
    """Every template in TEMPLATE_SECRETS resolves to at least one credential."""
    for template in TEMPLATE_SECRETS:
        creds = credentials_for_template(template)
        assert creds, f"Template '{template}' has no matching credential"

def test_openrouter_templates_use_openrouter_credential():
    for name in _shipped_template_names():
        if name.startswith("openrouter-"):
            creds = credentials_for_template(name)
            assert any(c.name == "openrouter" for c in creds)

def test_litellm_local_templates_use_upstream_credential():
    """litellm-anthropic-local -> anthropic-api, litellm-gemini-local -> gemini-api, etc."""
    expected = {
        "litellm-anthropic-local": "anthropic-api",
        "litellm-gemini-local": "gemini-api",
        "litellm-openai-local": "openai-api",
    }
    for template, cred_name in expected.items():
        creds = credentials_for_template(template)
        assert any(c.name == cred_name for c in creds), f"{template} should need {cred_name}"
```

Tests iterate **actual shipped template files** (`src/forge/config/defaults/templates/*.yaml`), not just
`TEMPLATE_SECRETS`. This catches two kinds of drift: a new template file added but forgotten in `TEMPLATE_SECRETS`, and
a `TEMPLATE_SECRETS` entry that doesn't map to any credential.

**Deferred hardening**: Parse template YAML files for env var references (e.g., `${OPENROUTER_API_KEY}` in config
values) and assert they match the `TEMPLATE_SECRETS` entries. This would catch cases where a template references an env
var that `TEMPLATE_SECRETS` doesn't list, or vice versa. Not v1 critical — filename-based coverage catches most drift.

### 4. Enforcement with actionable errors

The highest-value part. When a credential is missing at the point of use, the error message includes five things:

1. What failed
2. What key is needed
3. What capability it unlocks
4. Signup URL (if known)
5. The exact `forge auth login` command

**Subprocess failure** (missing `ANTHROPIC_API_KEY` when spawning `claude -p --bare`):

```
Error: Supervisor requires ANTHROPIC_API_KEY (Forge subprocess auth).

  This key is for Forge subprocesses that run 'claude -p --bare'.
  It is NOT your Claude Code login (OAuth/Max plan still works without it).
  NOT needed for Claude via OpenRouter or LiteLLM (those use their own keys).

  Get one at https://console.anthropic.com/
  Tip: Run 'forge auth login -c anthropic-api' to configure.
       Or use --subprocess-proxy to route through an existing proxy.
```

The `not_needed_for` lines render **only for `anthropic-api`** — that's the one credential where false urgency is
genuinely likely. Other credentials skip it.

**Proxy creation failure** (missing `OPENROUTER_API_KEY`):

```
Error: Template 'openrouter-anthropic' requires OPENROUTER_API_KEY.

  Unlocks: all openrouter-* proxy templates (Claude, GPT, Gemini, DeepSeek, etc.)
  Get one at https://openrouter.ai/keys
  Tip: Run 'forge auth login -c openrouter' to configure.
```

These are generated by `format_missing_credential_error()` from the capability registry — not hand-written per call
site.

### 5. Status command: both views

`forge auth status` shows capability summary first, then flat key/source listing:

```
Credential status (profile: default)
=====================================

Configured capabilities:
  * Proxy routing:      openrouter-* (OPENROUTER_API_KEY, env)
  * Forge subprocesses: supervisor, handoff, direct workers (ANTHROPIC_API_KEY, env)
  * Local Gemini proxy: litellm-gemini-local (GEMINI_API_KEY, file:default)

Not configured (set up if needed):
  - Local OpenAI proxy: litellm-openai-local (openai-api)
  - Remote LiteLLM:     litellm-* proxies (litellm-remote)

Credential details:
  openrouter
    * OPENROUTER_API_KEY = sk-or…ab12  (env)
    - OPENROUTER_BASE_URL = https://openrouter.ai/api/v1  (default)

  anthropic-api
    * ANTHROPIC_API_KEY = sk-a…xy34  (env)

  openai-api
    - OPENAI_API_KEY  not configured

  gemini-api
    * GEMINI_API_KEY = AIza…9f2e  (file:default)

  litellm-remote
    - LITELLM_API_KEY  not configured
    - LITELLM_BASE_URL  not configured
```

Capability view answers "what can I do?" with source attribution; flat view answers "where is each key coming from?"
Both are needed.

Unconfigured credentials use neutral language ("not configured") not error language ("MISSING"). A missing
`LITELLM_API_KEY` is not a problem if the user never uses remote LiteLLM. Red/error wording is reserved for enforcement
errors at the point of use.

### 6. Warning policy

**Do not warn on every `forge session start`**. Too noisy for users who don't use subprocesses.

**Warn lazily**: only at the moment a feature actually needs the credential:

- Supervisor tries to spawn `claude -p --bare` → error with actionable message
- Handoff agent tries to run → same
- `forge proxy create litellm-anthropic-local` → error if `ANTHROPIC_API_KEY` missing

**One exception**: `forge session start` with explicit `--subprocess-proxy` that can't resolve → fail fast at launch
(already implemented).

## Scope

### Files to create

| File                                  | Purpose                                                    |
| ------------------------------------- | ---------------------------------------------------------- |
| `src/forge/core/auth/capabilities.py` | Credential registry, capability metadata, error formatting |

### Files to change

| File                                        | Change                                                                          |
| ------------------------------------------- | ------------------------------------------------------------------------------- |
| `src/forge/cli/auth.py`                     | Credential menu, disambiguation prompts, retired name handling (see note below) |
| `src/forge/core/auth/template_secrets.py`   | Thin wrapper over `capabilities.py` (backward compat)                           |
| `src/forge/core/reactive/env.py`            | Use `format_missing_credential_error()` for subprocess auth                     |
| `src/forge/core/reactive/session_runner.py` | Same                                                                            |
| `src/forge/proxy/client_factory.py`         | Same for proxy creation                                                         |

### Files to test

| Test file                                  | What it validates                                       |
| ------------------------------------------ | ------------------------------------------------------- |
| `tests/src/core/auth/test_capabilities.py` | Every template maps to credential(s), env var ownership |
| Existing `test_template_secrets.py`        | Backward compat of `get_secrets_for_template()`         |

### Implementation note: Click option type

The current `--provider` uses `click.Choice(list(PROVIDERS.keys()))` which rejects unknown values before the command
body runs. This means retired names like `anthropic` would get Click's generic "Invalid value" error instead of our
custom migration message.

**Fix**: Use a plain `str` option, then validate manually in the command body:

```python
@click.option("--credential", "-c", "--provider", "-p", type=str, default=None,
              help="Credential to configure")
def login(credential: str | None, profile: str | None) -> None:
    if credential is not None:
        if credential in RETIRED_NAMES:
            click.secho(RETIRED_NAMES[credential], fg="yellow", err=True)
            raise SystemExit(1)
        if credential not in CREDENTIALS:
            click.secho(f"Unknown credential '{credential}'.", fg="red", err=True)
            click.echo(f"Available: {', '.join(CREDENTIALS)}", err=True)
            raise SystemExit(1)
```

This gives us full control over error messages for both retired and unknown names.

**Deferred improvement**: A custom `click.ParamType` subclass would be cleaner than manual validation + `SystemExit` —
it integrates with Click's formatting, shell completion, and test helpers. Not blocking for v1, but worth upgrading to
if the validation logic grows.

### Unchanged

- `--profile` support
- Credential file format (`~/.forge/credentials.yaml`)
- Resolution chain (env > file > config), unless `auth.ignore_env` is set
- `forge auth logout`, `forge auth profiles`

## Migration

- `--credential/-c` is the new preferred flag
- `--provider/-p` kept as backward-compatible alias for the flag itself (same behavior, just flag rename)
- No credential file format change
- Old env vars unchanged (`ANTHROPIC_API_KEY` is still `ANTHROPIC_API_KEY`)

### Retired credential names

Old names are **not silently accepted**. They produce a clear error with the new name and context:

```python
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
```

This teaches the new model instead of hiding it behind aliases. Pre-1.0 project — clean breaks over compatibility shims
(per coding-standards.md internal surface policy).

## Resolved decisions

01. **`forge auth status`**: Show both views — capability summary first, flat key/source listing second.
02. **Warning policy**: Lazy only — warn at the moment a feature needs the credential, not at session start.
03. **Anthropic naming**: `anthropic-api` (not `forge-subprocess` — too implementation-shaped).
04. **No credential name aliases**: Old names (`anthropic`, `litellm-local`) produce a helpful error, not silent
    acceptance. Aliases would preserve the exact confusion the redesign exists to eliminate.
05. **`litellm-local`**: Not a credential at all. It was a setup path that consumed upstream keys. Users configure
    `gemini-api`, `openai-api`, or `anthropic-api` directly.
06. **Data source**: New `forge.core.auth.capabilities` module, not overloading `template_secrets.py`.
07. **CLI flag**: `--credential/-c` preferred, `--provider/-p` as backward-compatible alias (flag rename only — the
    credential *values* must be canonical names).
08. **Env var metadata**: Structured `EnvVar` dataclass with `required`, `secret`, `connection_value` fields.
    `LITELLM_BASE_URL` and `OPENROUTER_BASE_URL` are shown plaintext (not masked).
09. **Template mapping**: `credentials_for_template()` returns `list[Credential]` (plural). Template lists are
    test-derived from `template_secrets.py`, not manually maintained on the `Credential` dataclass.
10. **Status tone**: Unconfigured credentials say "not configured" not "MISSING". Error wording reserved for enforcement
    at point of use.
11. **`not_needed_for`**: Rendered only for `anthropic-api` prompts and errors (the one credential where false urgency
    is likely). Not shown for other credentials.
12. **Menu prompts**: Plain `click.prompt` for input. Rich for display formatting if already convenient. No interactive
    TUI pickers.
13. **Env var handling**: Five-state display for multi-var credentials (`configured (env)`, `configured (file)`,
    `configured (env+file)`, `partially configured`, `not configured`). Plus `not configured (env ignored)` when
    `auth.ignore_env` is active. Env-set keys show "press Enter to skip" during login. `auth.ignore_env: true` in
    `~/.forge/config.yaml` skips all env vars wholesale — no per-variable list.
14. **Error formatter includes profile**: `format_missing_credential_error()` takes active `profile` name so hints say
    `forge auth login -c X --profile work`. Also takes `env_ignored` flag for diagnostic notes when env vars exist but
    are being skipped.
15. **Click ParamType**: Deferred. Manual validation + `SystemExit` for v1; custom `ParamType` subclass if validation
    logic grows.
16. **Template env placeholder parsing**: Deferred. Filename-based coverage for v1; parse template YAML for env var
    references as future hardening.
