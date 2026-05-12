# Forge Authentication

> **Alias:** `forge auth` is a shorthand for `forge authentication`. Both work interchangeably.

Store and manage API keys for LLM providers. Forge keeps credentials in `~/.forge/credentials.yaml` with named profile
support, so you don't need to manage `.env` files or export environment variables manually.

- Runtime config: [`configs.md`](configs.md)
- Proxy templates (which providers to use): [`proxies.md`](proxies.md)

---

## Quick start

```bash
# Store credentials for a single provider
forge authentication login --provider anthropic

# Check what's configured and where each key comes from
forge authentication status

# Store credentials in a named profile
forge authentication login --provider anthropic --profile work

# Switch active profile
export FORGE_PROFILE=work
forge authentication status
```

---

## How credential resolution works

Forge resolves credentials through a two-level chain. The first truthy value wins:

```
1. Environment variables (.env, shell exports)     ← always checked first
2. Credential file (~/.forge/credentials.yaml)     ← file-based fallback
```

Environment variables always override file-based credentials. This lets you temporarily swap a key without touching the
credential file:

```bash
# One-off override
ANTHROPIC_API_KEY=sk-ant-temp forge session start test
```

---

## Providers and keys

`forge authentication login` knows which keys each provider needs:

| Provider         | Required keys                         | Optional keys                                               | Description                                  |
| ---------------- | ------------------------------------- | ----------------------------------------------------------- | -------------------------------------------- |
| `openrouter`     | `OPENROUTER_API_KEY`                  | `OPENROUTER_BASE_URL`                                       | OpenRouter multi-provider gateway            |
| `litellm-remote` | `LITELLM_API_KEY`, `LITELLM_BASE_URL` |                                                             | Remote/shared LiteLLM gateway                |
| `litellm-local`  |                                       | `GEMINI_API_KEY`, `OPENAI_API_KEY`, `LITELLM_LOCAL_API_KEY` | Local LiteLLM (store keys for your template) |
| `anthropic`      | `ANTHROPIC_API_KEY`                   |                                                             | Direct Anthropic API                         |

### Subprocess authentication

Multi-model workflows (`forge workflow panel`, `debate`, `analyze`, `consensus`) spawn headless `claude -p` workers.
These workers authenticate via `ANTHROPIC_API_KEY` regardless of how the main session authenticates (subscription auth
is interactive-only). Forge resolves `ANTHROPIC_API_KEY` from env or `~/.forge/credentials.yaml` automatically.

If you see authentication errors in workflow output, run `forge auth login -p anthropic` to store the key.

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
forge authentication login [--provider PROVIDER] [--profile PROFILE]
```

Prompts for API keys and stores them in `~/.forge/credentials.yaml`.

- `--provider`, `-p` — Configure a single provider (e.g., `anthropic`, `litellm-remote`). Omit to be prompted for all
  providers.
- `--profile` — Profile name to store credentials in. Defaults to `"default"` (or `FORGE_PROFILE` env var).

Sensitive keys (containing `API_KEY`, `SECRET`, `TOKEN`, `PASSWORD`) are entered with hidden input. If a value already
exists, it's shown as a masked default (e.g., `sk-a...7890`) — press Enter to keep it.

```bash
# Configure everything
forge authentication login

# Single provider, named profile
forge authentication login -p anthropic --profile work
```

### `forge authentication status`

```bash
forge authentication status [--profile PROFILE]
```

Shows each credential's source and masked value:

```
litellm-remote: Remote LiteLLM gateway
----------------------------------------
  ✗ LITELLM_API_KEY  MISSING

anthropic: Direct Anthropic API
----------------------------------------
  ✓ ANTHROPIC_API_KEY = sk-a…7890  (file:default)
```

Source labels:

| Label                | Meaning                                      |
| -------------------- | -------------------------------------------- |
| `(env)`              | From environment variable (highest priority) |
| `(file:default)`     | From credential file, `default` profile      |
| `(file:work)`        | From credential file, `work` profile         |
| `MISSING`            | Not found anywhere (required key)            |
| `not set (optional)` | Not found, but the key is optional           |

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
  default (3 keys) ← active
  work (1 keys)
```

The active profile is determined by: `--profile` CLI flag > `FORGE_PROFILE` env var > `"default"`.

---

## Profiles

Profiles let you maintain separate credential sets — for example, team keys vs personal keys, or production vs staging.

```bash
# Create profiles
forge authentication login -p litellm-remote                       # → default profile
forge authentication login -p anthropic --profile personal            # → personal profile

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
# 1. Run login for each provider you use
forge authentication login -p anthropic
forge authentication login -p litellm-remote

# 2. Verify everything resolved
forge authentication status

# 3. Remove credential lines from .env (keep non-secret config if any)
```

You can also keep using `.env` — environment variables take precedence over the credential file, so both approaches
work. The credential file is convenient when you want profile switching or don't want to manage `.env` files across
projects.

---

## Troubleshooting

### "MISSING" for a key you know you stored

Check which profile you're looking at:

```bash
forge authentication status                       # Checks default profile
forge authentication status --profile work        # Checks work profile
echo $FORGE_PROFILE                     # Check active profile env var
```

Keys stored in one profile aren't visible from another.

### Environment variable overriding stored credential

`forge authentication status` shows `(env)` when an environment variable is set. To use the file-based value instead,
unset the env var:

```bash
unset ANTHROPIC_API_KEY
forge authentication status   # Should now show (file:default)
```

### Corrupt credentials file

If `forge authentication` commands fail with a parse error:

```bash
# Back up and recreate
mv ~/.forge/credentials.yaml ~/.forge/credentials.yaml.corrupt
forge authentication login
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
