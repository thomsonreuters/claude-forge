# Forge Authentication

> **Alias:** `forge auth` is a shorthand for `forge authentication`. Both work interchangeably.

Store and manage API keys for Forge proxy routing and subprocesses. Forge keeps credentials in
`~/.forge/credentials.yaml` with named profile support, so you don't need to manage `.env` files or export environment
variables manually.

**These are NOT your Claude Code login** — Claude Code authenticates separately (OAuth, Max plan, etc.). Forge
credentials are for proxy routing and headless subprocesses (`supervisor`, `handoff agent`, `direct panel workers`).

- Runtime config: [`configs.md`](configs.md)
- Proxy templates (which providers to use): [`proxies.md`](proxies.md)

---

## Quick start

```bash
# Interactive credential selection menu
forge auth login

# Configure a single credential
forge auth login -c anthropic-api

# Store credentials in a named profile
forge auth login -c anthropic-api --profile work

# Check what's configured and where each key comes from
forge auth status

# Switch active profile
export FORGE_PROFILE=work
forge auth status
```

---

## How credential resolution works

Forge resolves credentials through a two-level chain. The first truthy value wins:

```
1. Environment variables (.env, shell exports)     <- always checked first
2. Credential file (~/.forge/credentials.yaml)     <- file-based fallback
```

Environment variables always override file-based credentials. This lets you temporarily swap a key without touching the
credential file:

```bash
# One-off override
ANTHROPIC_API_KEY=sk-ant-temp forge session start test
```

### Ignoring environment variables (`auth_ignore_env`)

Sometimes your shell `ANTHROPIC_API_KEY` is for a different account or billing context than what you want Forge
subprocesses to use. Set `auth_ignore_env` to skip all env vars for credential resolution:

```bash
forge config set auth_ignore_env=true
forge auth login -c anthropic-api   # Store the Forge-specific key
```

When active, the resolution chain becomes credential file only. `forge auth status` shows `(env ignored)` for
credentials that have env vars present but skipped.

---

## Credentials and capabilities

`forge auth login` shows a credential selection menu. Each credential maps to specific Forge capabilities:

| Credential       | Env var(s)                             | What it unlocks                                                         |
| ---------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| `openrouter`     | `OPENROUTER_API_KEY`                   | All `openrouter-*` proxy templates, OSS workflow models                 |
| `anthropic-api`  | `ANTHROPIC_API_KEY`                    | Forge subprocesses, direct Anthropic workers, `litellm-anthropic-local` |
| `openai-api`     | `OPENAI_API_KEY`                       | `litellm-openai-local` proxy                                            |
| `gemini-api`     | `GEMINI_API_KEY`                       | `litellm-gemini-local` proxy                                            |
| `litellm-remote` | `LITELLM_API_KEY` + `LITELLM_BASE_URL` | All remote `litellm-*` proxy templates                                  |

### Anthropic API key disambiguation

`anthropic-api` is for Forge subprocess auth (pay-per-token API key). It is **NOT**:

- Your Claude Code login (OAuth/Max plan still works without it)
- Needed for Claude via `openrouter-anthropic` (uses `OPENROUTER_API_KEY`)
- Needed for Claude via `litellm-anthropic` (uses `LITELLM_API_KEY`)

If you see authentication errors in workflow output, run `forge auth login -c anthropic-api` to store the key. Or use
`--subprocess-proxy` to route subprocesses through an existing proxy instead.

### Which auth do I need?

| Flow                                         | Needs                                                                  |
| -------------------------------------------- | ---------------------------------------------------------------------- |
| `forge session start` (direct)               | Claude Code login/subscription is enough                               |
| `forge workflow analyze` (default)           | `ANTHROPIC_API_KEY`                                                    |
| `forge workflow panel` (default)             | `ANTHROPIC_API_KEY` + active `openrouter-openai` + `openrouter-gemini` |
| `forge session resume --fresh -s ai-curated` | `OPENROUTER_API_KEY`                                                   |
| OpenRouter proxy (`openrouter-*`)            | `OPENROUTER_API_KEY`                                                   |
| Remote LiteLLM proxy (`litellm-openai`)      | `LITELLM_API_KEY` + `LITELLM_BASE_URL`                                 |
| Local LiteLLM proxy (`litellm-openai-local`) | `OPENAI_API_KEY`                                                       |
| Local LiteLLM proxy (`litellm-gemini-local`) | `GEMINI_API_KEY`                                                       |

---

## CLI reference

### `forge authentication login`

```bash
forge authentication login [--credential NAME] [--profile PROFILE]
```

Prompts for API keys and stores them in `~/.forge/credentials.yaml`.

- `--credential`, `-c` — Configure a single credential (e.g., `anthropic-api`, `openrouter`). Omit for the selection
  menu.
- `--provider`, `-p` — Alias for `--credential` (backward compatible).
- `--profile` — Profile name to store credentials in. Defaults to `"default"` (or `FORGE_PROFILE` env var).

When no `--credential` is given, an interactive numbered menu shows all credentials with their configuration state and
capability descriptions. Enter comma-separated numbers, `all`, or press Enter for all.

Env-aware prompting: when a key is already set via an environment variable, the prompt shows the value and lets you skip
(press Enter). When `auth_ignore_env` is active, the prompt explains the env var is present but ignored.

Old credential names (`anthropic`, `litellm-local`) produce a clear error with migration guidance.

```bash
# Credential selection menu
forge auth login

# Single credential, named profile
forge auth login -c anthropic-api --profile work
```

### `forge authentication status`

```bash
forge authentication status [--profile PROFILE]
```

Shows a two-section view:

1. **Capability summary** — what's configured and what's not, with source attribution
2. **Credential details** — per-variable values (masked for secrets), source labels, and defaults

```
Credential status (profile: default)
==================================================

Configured capabilities:
  * openrouter           OpenRouter proxy templates, OSS workflow models (Routes to Claude, GPT, Gemini, DeepSeek, etc. via OpenRouter)  (env)

Not configured (set up if needed):
  - anthropic-api        Forge subprocesses, direct Anthropic workers, ...  (not configured)

Credential details:

  openrouter
    * OPENROUTER_API_KEY = sk-o...ab12  (env)
    - OPENROUTER_BASE_URL = https://openrouter.ai/api/v1  (default)

  anthropic-api
    - ANTHROPIC_API_KEY  not configured
```

Source labels:

| Label                          | Meaning                                             |
| ------------------------------ | --------------------------------------------------- |
| `(env)`                        | From environment variable (highest priority)        |
| `(file:default)`               | From credential file, `default` profile             |
| `(file:work)`                  | From credential file, `work` profile                |
| `(default)`                    | Built-in default (e.g., OpenRouter base URL)        |
| `not configured`               | Not found (neutral — only an error at point of use) |
| `not configured (env ignored)` | Env var exists but `auth_ignore_env` is active      |

### `forge authentication logout`

```bash
forge authentication logout [--profile PROFILE] [--yes]
```

Removes a profile from the credential file. Asks for confirmation unless `-y` is passed. Environment variables are not
affected.

```bash
forge authentication logout                     # Remove default profile (with confirmation)
forge authentication logout --profile work -y   # Remove work profile, skip confirmation
```

### `forge authentication profiles`

```bash
forge authentication profiles
```

Lists saved profiles with key counts and marks the active one:

```
Saved profiles (2):
------------------------------
  default (3 keys) <- active
  work (1 keys)
```

The active profile is determined by: `--profile` CLI flag > `FORGE_PROFILE` env var > `"default"`.

---

## Profiles

Profiles let you maintain separate credential sets — for example, team keys vs personal keys, or production vs staging.

```bash
# Create profiles
forge auth login -c litellm-remote                       # -> default profile
forge auth login -c anthropic-api --profile personal     # -> personal profile

# Switch active profile
export FORGE_PROFILE=personal

# Check which profile is active
forge authentication profiles
```

Profile names must match `[A-Za-z0-9_-]` — no spaces or path separators.

---

## Credential file format

Location: `~/.forge/credentials.yaml` (permissions: `0o600`)

```yaml
# Forge Credential Store — managed by `forge authentication login`

version: 1
profiles:
  default:
    OPENROUTER_API_KEY: "sk-or-..."
    LITELLM_API_KEY: "sk-litellm-..."
  personal:
    ANTHROPIC_API_KEY: "sk-ant-..."
    GEMINI_API_KEY: "AIza..."
```

Key names match the environment variable names that Forge already looks up — no mapping layer between them.

**Do not edit this file by hand** unless you know what you're doing. Use `forge authentication login` to manage it. The
file uses atomic writes and advisory locking to prevent corruption from concurrent access.

---

## Migrating from `.env`

If you currently keep credentials in a `.env` file, you can migrate to the credential store:

```bash
# 1. Run login for each credential you use
forge auth login -c anthropic-api
forge auth login -c litellm-remote

# 2. Verify everything resolved
forge auth status

# 3. Remove credential lines from .env (keep non-secret config if any)
```

You can also keep using `.env` — environment variables take precedence over the credential file, so both approaches
work. The credential file is convenient when you want profile switching or don't want to manage `.env` files across
projects.

### Migrating from old credential names

If you used `--provider anthropic` or `--provider litellm-local`, the new names are:

| Old name        | New name        | Notes                                                             |
| --------------- | --------------- | ----------------------------------------------------------------- |
| `anthropic`     | `anthropic-api` | Pay-per-token API key, not Claude Code login                      |
| `litellm-local` | *(removed)*     | Configure `gemini-api`, `openai-api`, or `anthropic-api` directly |

The old names produce a helpful error with migration instructions.

---

## Troubleshooting

### Key shows "not configured" but you know you stored it

Check which profile you're looking at:

```bash
forge auth status                           # Checks default profile
forge auth status --profile work            # Checks work profile
echo $FORGE_PROFILE                         # Check active profile env var
```

Keys stored in one profile aren't visible from another.

### Environment variable overriding stored credential

`forge auth status` shows `(env)` when an environment variable is set. To use the file-based value instead, either unset
the env var or enable `auth_ignore_env`:

```bash
# Option 1: Unset the env var
unset ANTHROPIC_API_KEY
forge auth status   # Should now show (file:default)

# Option 2: Ignore all env vars for credential resolution
forge config set auth_ignore_env=true
forge auth status   # Shows (env ignored) for env-provided vars
```

### Corrupt credentials file

If `forge authentication` commands fail with a parse error:

```bash
# Back up and recreate
mv ~/.forge/credentials.yaml ~/.forge/credentials.yaml.corrupt
forge auth login
```

### Wrong file permissions

The credential file should be readable only by you:

```bash
ls -la ~/.forge/credentials.yaml
# Expected: -rw------- (0o600)

# Fix if needed
chmod 600 ~/.forge/credentials.yaml
```

---

## Files reference

| File                        | Purpose                            |
| --------------------------- | ---------------------------------- |
| `~/.forge/credentials.yaml` | Credential store (profiles + keys) |
| `.env`                      | Environment-based secrets (legacy) |
