# Forge Configuration — Quick Reference

Configuration is split by ownership. Each type of setting has a single authoritative location:

| What you want to change                          | Where                              | Command                      |
| ------------------------------------------------ | ---------------------------------- | ---------------------------- |
| Proxy mode, context limit, timeouts, logging     | `~/.forge/config.yaml`             | `forge config set/edit`      |
| Model routing, reasoning effort, temperature     | `~/.forge/proxies/<id>/proxy.yaml` | `forge proxy set/edit`       |
| Claude Code hooks, status line, permissions, env | `~/.forge/claude.preset.json`      | `forge claude preset ...`    |
| Policy, memory, verification settings            | Session manifest                   | `forge session set`          |
| Multi-model review and analysis                  | N/A (uses proxy/session config)    | [workflows.md](workflows.md) |
| Automatic doc updates after sessions             | Session manifest (`memory.*`)      | [handoff.md](handoff.md)     |
| API keys and credentials                         | `~/.forge/credentials.yaml`        | [auth.md](auth.md)           |

---

## Runtime config (`~/.forge/config.yaml`)

Global Forge preferences. This file is **optional** — Forge works with built-in defaults when it's missing.
`forge config` auto-creates the file on first access with documented defaults and comments.

```bash
# Shorthand for `forge config show`
forge config

# Auto-create with commented defaults, then view effective config
forge config show
forge config show --raw     # YAML only, no headings or syntax highlighting

# Set a value
forge config set proxy_mode=sidecar
forge config set status_timeout=1.0

# Edit in $EDITOR
forge config edit

# Reset to built-in defaults
forge config reset proxy_mode   # Reset one key
forge config reset              # Delete config.yaml and use defaults
```

Notes:

- `forge config show` displays the effective config: built-in defaults, file values, and any environment overrides.
- `forge config edit` validates the edited YAML before applying it.
- `forge config reset <key>` removes that key from the file; `forge config reset` removes the whole file.
- `%config` inside Claude Code is read-only and shows the same effective runtime config.

Available settings:

| Key                              | Default                | Description                                                                                                                                |
| -------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `proxy_mode`                     | `host`                 | `host` (proxy on host) or `sidecar` (bundled in Docker)                                                                                    |
| `sidecar_image`                  | `forge-sidecar:latest` | Docker image for sidecar mode                                                                                                              |
| `user_agent_claude_code_version` | *(empty)*              | Version in User-Agent header sent to upstream LLM providers                                                                                |
| `context_limit`                  | `200000`               | Fallback auto-compact window for proxy mode (passed as `CLAUDE_CODE_AUTO_COMPACT_WINDOW`)                                                  |
| `status_timeout`                 | `2.0`                  | Status line proxy/git call timeout (seconds)                                                                                               |
| `handoff_timeout`                | `300`                  | Handoff agent timeout (seconds)                                                                                                            |
| `log_level`                      | `off`                  | File logging level (`off`, `debug`, `info`, `warning`)                                                                                     |
| `policy_summary_feedback`        | `on`                   | Post-evaluation summary lines and additionalContext (`on`/`off`)                                                                           |
| `log_tool_failures`              | `false`                | Log tool failures to `~/.forge/logs/tool_failures/` (proxy; includes tool inputs/errors)                                                   |
| `auth_ignore_env`                | `false`                | Ignore env vars for credential resolution; use credential file only. See [auth.md](auth.md#ignoring-environment-variables-auth_ignore_env) |

Environment overrides:

- `FORGE_DEBUG` overrides `log_level`. Accepted values: `1/true/yes` -> `debug`, `0/false/no/off` -> `off`, or explicit
  `debug/info/warning`

**Note on running processes:** Runtime config is cached per-process. Changes via `forge config set` take effect for new
CLI invocations and new sessions, but **already-running proxies do not pick up changes until restart**. To toggle
`log_tool_failures` on a live proxy, run `forge proxy stop <id> && forge proxy start <id>`.

**In-session access (read-only):** Type `%config` in the Claude prompt to see effective config. See
[hooks.md](hooks.md#in-session-commands--commands) for all `%` commands.

---

## Claude Code preset (`~/.forge/claude.preset.json`)

Forge keeps Claude Code settings customizations in a separate JSON preset. This file is user-editable and is merged into
Claude Code `settings.json` when you run `forge extension enable`.

```bash
# Shorthand for `forge claude preset show`
forge claude preset

# Show the current preset
forge claude preset show
forge claude preset show --raw

# Edit in $EDITOR
forge claude preset edit

# Reset to built-in defaults
forge claude preset reset
forge claude preset reset --yes
```

Built-in defaults include only Forge infrastructure:

- `hooks`: Forge hook wiring (`forge hook ...`)
- `statusLine`: `forge status-line`
- `permissions`: Write/Edit (required by handoff agent)

Forge merges only four setting families from the preset: `hooks`, `statusLine`, `env`, and `permissions`.

Use the preset when you want Forge to keep applying your preferred Claude Code settings on enable/re-enable, for
example:

- extra `env` entries
- personal `permissions`
- advanced hook or status-line customization if you intentionally want to override Forge defaults

Notes:

- The preset file is auto-created on first access.
- `forge claude preset edit` validates JSON before saving.
- `forge claude preset reset` restores the built-in preset; without `--force`, it asks for confirmation.
- If the preset file is corrupted, Forge tells you to fix it with `forge claude preset edit` or reset it.

---

## Secrets (`forge authentication`)

API keys and credentials are managed via `forge auth login` and stored in `~/.forge/credentials.yaml`. These are for
Forge proxy routing and subprocesses, not your Claude Code login. Environment variables (`.env`, shell exports) still
work and take precedence over stored credentials (unless `auth_ignore_env` is set).

```bash
# Interactive credential menu
forge auth login

# Configure a single credential
forge auth login -c anthropic-api

# Check what's configured and where each key comes from
forge auth status
```

See [auth.md](auth.md) for credential details, profiles, migration, and full CLI reference.

**Rule:** Credential storage holds secrets and connection values (e.g., `LITELLM_BASE_URL`). Connection values are a
convenience fallback for bootstrapping proxy creation. Once `proxy.yaml` exists, proxy-owned routing is authoritative.

---

## Proxy files (`~/.forge/proxies/<id>/proxy.yaml`)

Model routing and hyperparameters. Each proxy is a self-contained YAML file — no merge with templates at runtime.

See [proxies.md](proxies.md).

---

## Worktree config (auto-copied)

When `forge session fork --worktree` or `forge session start --worktree` creates a git worktree, Forge copies untracked
runtime config from the main repo. These files are NOT git-tracked, so worktrees wouldn't have them otherwise.

**Copied automatically:**

| Path                           | Purpose                                       |
| ------------------------------ | --------------------------------------------- |
| `.env`, `.env.local`           | Environment variables (API keys, base URLs)   |
| `.envrc`                       | direnv configuration                          |
| `.mcp.json`, `.mcp.local.json` | MCP server configuration                      |
| `docker/certs/`                | Additional CA certificates (entire directory) |

Files/directories are skipped if they already exist in the target or are tracked by git. `--into` forks skip this copy
entirely (the target worktree already has its own config).

## Additional CA certificates

For environments with SSL inspection (e.g. enterprise, Zscaler), place **CA certificate** files in `docker/certs/`:

```bash
# CA certificate .pem or .crt files are auto-installed in Docker builds
cp your-ca.pem docker/certs/
```

The Dockerfile discovers all `.pem` and `.crt` files (top-level only — subdirectories are not scanned), copies them into
the Debian system trust store (`/usr/local/share/ca-certificates/`), and runs `update-ca-certificates` to merge them
into the canonical OS bundle at `/etc/ssl/certs/ca-certificates.crt`. Node.js (Claude Code) reads that bundle via
`ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt`, which is set unconditionally — the file always exists
(Mozilla defaults are present even with no user-added certs), so there is no empty-file warning. No filename convention
required — any `.pem` or `.crt` works.

**Security**: Only place CA certificate files here. **Never place private keys** (`.pem` files containing `PRIVATE KEY`
blocks) in this directory — they would be concatenated into the trust bundle and baked into the Docker image layer.

For worktree forks, the `docker/certs/` directory is automatically copied from the main repo (see above).

---

## Internal (not user-editable)

| What            | Location                                 |
| --------------- | ---------------------------------------- |
| Model catalog   | `src/forge/core/data/model_catalog.yaml` |
| Proxy templates | `src/forge/config/defaults/templates/`   |

To customize routing, create a proxy from a template and edit it. See [proxies.md](proxies.md).
