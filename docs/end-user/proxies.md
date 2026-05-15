# Forge Proxies — Routing Configuration

**Proxies are where you configure model routing and LLM defaults.**

To use different models, change reasoning effort, or switch providers: create or customize a proxy.

- Canonical architecture: [`docs/design.md`](../design.md)
- Configuration overview: [`configs.md`](configs.md)
- Sessions (workflow settings): [`sessions.md`](sessions.md)

---

## Why proxies exist

Claude Code doesn't send session IDs downstream. The proxy identifies requests by which port they hit. Therefore:

- **Proxy = base_url/port = routing configuration**
- Different routing needs → different proxy
- Sessions reference proxies but cannot modify them

### Consequence (normative)

- **LLM routing + default hyperparameters are proxy-owned.**
- **Sessions cannot override proxy-owned routing/hyperparams.**

If you want different model mappings or thinking defaults: use a different proxy.

### Full model capabilities

Provider CLIs sometimes limit the models they serve. For example, OpenAI's Codex CLI caps GPT-5.5 at 400K tokens as a
serving-budget decision, even though the model supports 1,050,000 tokens via the API. Forge proxies route through the
API directly, so you get the model's full context window and the complete set of reasoning effort levels.

This also means access to models that product CLIs don't expose at all -- like `gpt-5.5-pro` (1M context, higher
reasoning quality) or mixing providers within a single workflow (GPT for planning, Claude for execution).

The tradeoff is cost: you pay API rates instead of bundled subscription pricing. Forge's
[spend caps](#cost-tracking-and-spend-caps) make this manageable.

### System prompt addendums

When routing to non-Anthropic models, Forge automatically injects a tool-discipline addendum into the system prompt at
session launch. Non-Anthropic models tend to hallucinate optional tool parameters (e.g., `"pages": ""` on Read calls)
and reach for Bash as a workaround for tool errors. The addendum teaches them to use minimal valid parameters and prefer
dedicated tools. No configuration needed.

Note: addendums are injected by the session launcher (`--append-system-prompt-file`), not by the proxy itself. Direct
HTTP use of the proxy does not include them.

### No-proxy mode

When using Claude Code directly (without Forge proxy), proxies are not used. Sessions still function for workflow
settings (worktrees, artifacts, policies, etc.), but tier/model routing and hyperparameter defaults do not apply — those
require a proxy instance.

---

## Proxy templates

Forge provides ready-to-use proxy configurations (internal templates):

| Template                     | Use case                                    |
| ---------------------------- | ------------------------------------------- |
| `openrouter-anthropic`       | Claude models via OpenRouter (direct)       |
| `openrouter-deepseek`        | DeepSeek models via OpenRouter (direct)     |
| `openrouter-glm`             | GLM / Z.ai models via OpenRouter (direct)   |
| `openrouter-kimi`            | Kimi models via OpenRouter (direct)         |
| `openrouter-minimax`         | MiniMax models via OpenRouter (direct)      |
| `openrouter-openai`          | GPT models via OpenRouter (direct)          |
| `openrouter-qwen`            | Qwen models via OpenRouter (direct)         |
| `openrouter-gemini`          | Gemini models via OpenRouter (direct)       |
| `openrouter-openai-codex`    | OpenAI Codex models via OpenRouter (direct) |
| `openrouter-gemini-flash`    | Gemini Flash via OpenRouter (cheap, direct) |
| `litellm-anthropic`          | Anthropic models via remote/shared LiteLLM  |
| `litellm-anthropic-local`    | Local LiteLLM + Anthropic API key           |
| `litellm-openai`             | OpenAI models via remote/shared LiteLLM     |
| `litellm-gemini`             | Gemini models via remote/shared LiteLLM     |
| `litellm-openai-local`       | Local LiteLLM + OpenAI API key              |
| `litellm-openai-codex-local` | Local LiteLLM + OpenAI Codex models         |
| `litellm-gemini-local`       | Local LiteLLM + Gemini API key              |
| `litellm-gemini-flash-local` | Local LiteLLM + Gemini Flash (fast/cheap)   |

`litellm-gemini-test` also exists internally, but it is hidden from normal end-user template lists.

---

## Core commands (cheat sheet)

```bash
# Templates
forge proxy template list        # List available templates
forge proxy template show <name> # Show template configuration
forge proxy template edit <name> # Customize a template (copy-on-first-edit)
forge proxy template reset <name># Reset to built-in default

# Create / start
forge proxy create <template> [--name <id>] [--no-start]
forge proxy start <proxy_id> [--smoke-test]
forge proxy stop <proxy_id>

# Show / list
forge proxy show <proxy_id>      # Full proxy configuration
forge proxy list                 # All proxies with status

# Modify
forge proxy edit <proxy_id>      # Open in $EDITOR
forge proxy set <proxy_id> <key>=<value>

# Delete
forge proxy delete <proxy_id> [--yes] [--kill-adopted]

# Metrics
forge proxy metrics [proxy_id]   # Runtime metrics (tokens, latency, failures)
forge proxy metrics --all        # Metrics for all active proxies
forge proxy metrics --json       # Raw JSON output

# Maintenance
forge proxy clean                # Clean up stale proxies
forge proxy validate <proxy_id>  # Validate config
```

---

## OpenRouter (direct, no LiteLLM)

OpenRouter templates (`openrouter-anthropic`, `openrouter-deepseek`, `openrouter-glm`, `openrouter-kimi`,
`openrouter-minimax`, `openrouter-openai`, `openrouter-qwen`, `openrouter-gemini`, `openrouter-openai-codex`,
`openrouter-gemini-flash`) call the OpenRouter API directly -- no LiteLLM subprocess needed.

```bash
# Store your key
forge auth login -c openrouter

# Create and start (pick a model family)
forge proxy create openrouter-anthropic

# Launch Claude Code through OpenRouter
forge claude start --proxy <proxy_id>
```

Default tiers use Anthropic Claude models on OpenRouter. Edit the proxy to use any OpenRouter model:

```bash
forge proxy edit <proxy_id>
# Change tiers to e.g.:
#   haiku: google/gemini-2.5-flash
#   sonnet: anthropic/claude-sonnet-4.6
#   opus: openai/gpt-5.5
```

Models not in Forge's catalog (e.g., `meta-llama/llama-3.1-70b`) work -- the proxy uses safe defaults for
`max_output_tokens` and `context_window` when catalog data is unavailable.

---

## Model alternatives

Anthropic proxy templates (`openrouter-anthropic`, `litellm-anthropic`, `litellm-anthropic-local`) configure user-facing
`model_alternatives` to support multiple Claude model versions at the same tier. The default opus model is Claude Opus
4.6; use `--model` to select an alternative:

```bash
# Default: opus tier routes to Claude Opus 4.6
forge session start my-session --proxy openrouter-anthropic

# Select Opus 4.7 instead
forge session start my-session --proxy openrouter-anthropic --model claude-opus-4-7
```

The proxy resolves the alternative at request time -- Claude Code sends the model name, the proxy looks up
`model_alternatives[tier][model]` and routes to the configured backend model. Tier-level hyperparameters
(reasoning_effort, etc.) still apply regardless of which alternative is selected.

`--model` is currently a Claude model pin. Other proxy templates may define `model_alternatives` for explicit proxy API
requests that already send the matching model name, but those alternatives are not selected by `forge session --model`.

To add or edit alternatives, use `forge proxy edit <proxy_id>`:

```yaml
model_alternatives:
  opus:
    claude-opus-4-7: anthropic/claude-opus-4.7
```

---

## Proxy lifecycle

### List available proxies

```bash
forge proxy list
```

Shows:

- proxy id
- template
- base_url / port
- status/health
- pid (if Forge spawned it)

### Create a proxy

`create` ensures the proxy is running (reuse/adopt/spawn as needed):

- Creates the proxy config if it doesn't exist
- Starts the proxy if it's not running
- Returns the base_url

```bash
# Create from template (reuse/adopt/spawn as needed)
forge proxy create openrouter-openai
# → Proxy created at http://localhost:8096

# Create with per-tier overrides
forge proxy create openrouter-openai \
  --opus-reasoning high

# Create with custom name
forge proxy create openrouter-openai --name my-high-reasoning

# Create config only (don't start the server)
forge proxy create openrouter-openai --no-start

# Start and verify upstream connectivity (sends a real request)
forge proxy start openrouter-openai --smoke-test
```

**Semantics (reuse/adopt/spawn):**

- Reuses an existing healthy proxy for that template if present
- Adopts an orphan proxy at the expected default port if found
- Spawns a new proxy if neither exists
- Blocks until the proxy is healthy (with timeout)
- Records in `~/.forge/proxies/index.json`

Use `--smoke-test` after first setup or credential changes to verify the proxy can reach its upstream LLM provider.
Without it, health checks only confirm the local proxy process is alive.

### Start Claude with a proxy

```bash
forge claude start --proxy <proxy_id>
```

What this does:

- Resolves `<proxy_id>` in `~/.forge/proxies/index.json`
- Healthchecks the proxy (`GET /`) and verifies proxy identity
- Launches `claude` with `ANTHROPIC_BASE_URL=<proxy.base_url>`
- Sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` based on proxy's model context window

### Delete a proxy

```bash
forge proxy delete <proxy_id>
```

Stops the proxy and cleans up registry entries and overlay files.

### Other commands

```bash
# Prune stale proxies (dead processes)
forge proxy clean

# Validate a proxy config file
forge proxy validate <proxy_id>
```

---

## Customizing proxies

### At creation time

Specify per-tier overrides when creating a proxy:

```bash
forge proxy create openrouter-openai \
  --opus-reasoning high \
  --sonnet-reasoning medium \
  --sonnet-temperature 0.7
```

These overrides are saved to the proxy file (`~/.forge/proxies/<proxy_id>/proxy.yaml`).

### Edit an existing proxy

After creating a proxy, customize it further:

```bash
# Edit the proxy file in $EDITOR
forge proxy edit <proxy_id>

# Or set individual values
forge proxy set <proxy_id> tier_overrides.opus.reasoning_effort=high

# View full configuration
forge proxy show <proxy_id>

# Validate the config
forge proxy validate <proxy_id>
```

### Proxy file format (user edit surface)

When you create a proxy, Forge writes a complete `proxy.yaml` from the template. You own this file and can edit it
directly. The key fields you'll typically customize are `default_tier` and `tier_overrides`:

```yaml
# ~/.forge/proxies/<proxy_id>/proxy.yaml
proxy_format: 1
template: openrouter-openai
template_digest: abc123...

provider: openrouter
proxy_endpoint: http://localhost:8096
port: 8096
upstream_base_url: https://openrouter.ai/api/v1

tiers:
  haiku: openai/gpt-5.4-mini
  sonnet: openai/gpt-5.5
  opus: openai/gpt-5.5

default_tier: sonnet

tier_overrides:
  sonnet:
    reasoning_effort: medium
    temperature: 0.7
  opus:
    reasoning_effort: high
    thinking_budget_tokens: 16384

provider_settings: {}
prompt_caching: passthrough
auto_cache_min_tokens: 1024

costs:
  caps:
    per_day: null
    per_month: null
  cap_mode: post
  on_cap_hit: reject
```

**What you'll typically edit:** `default_tier`, `tier_overrides`, and sometimes `provider_settings`. Leave
`proxy_format`, `template`, `provider`, `proxy_endpoint`, `upstream_base_url`, `port`, and `tiers` alone unless you know
what you're doing — those are set from the template at creation.

**Available tier_override keys:** `reasoning_effort`, `temperature`, `max_tokens`, `thinking_budget_tokens`. All are
per-tier because each model has different limits and optimal defaults.

**Precedence chain** (first non-null wins):

1. Request explicit value (e.g., `temperature` in API call)
2. Per-tier override (`tier_overrides.<tier>.*`)
3. Model catalog default (built-in per-model defaults)

**Example:** If a request includes `temperature=0.5`, it overrides the proxy's `tier_overrides.opus.temperature`.

Provider, upstream URL, and template are fixed at creation. The proxy file only tunes defaults **within** that proxy's
routing scope.

---

## Proxies are shared state

⚠︎ Multiple sessions can use the same proxy. Modifying a proxy affects ALL sessions using it.

```bash
# Safe: create a separate proxy for different config
forge proxy create openrouter-openai --opus-reasoning high

# Careful: modifying an existing proxy affects everyone using it
forge proxy edit shared-proxy
```

---

## Canonical workflow: Plan -> Execute -> Panel

1. Create a **planning proxy** (`openrouter-openai`) and start Session A with that template.
2. Approve plan; stop.
3. Fork to Session B and relaunch Claude against an **execution proxy** (`forge claude start --proxy <proxy_id>`).
4. Fork to Session C and relaunch Claude against a **review proxy** the same way.
5. Use A and C for independent reviews; have B synthesize and fix.

Proxies make this deterministic: each session's requests hit a specific base URL, so routing defaults are stable.

---

## Proxy metrics

Each running proxy tracks in-memory metrics: request counts, token usage (input/output/cached), per-tier and per-model
breakdowns, failure rates, and latency. Metrics reset on proxy restart.

```bash
# View metrics for a specific proxy
forge proxy metrics my-proxy

# View all active proxies
forge proxy metrics --all

# JSON output (for scripting)
forge proxy metrics --json
```

Metrics are also available via the proxy's `GET /` endpoint under the `metrics` key:

```bash
curl http://localhost:8085/ | jq .metrics
```

**What metrics track:**

- **Tokens**: input, output, cached (for cost visibility vs Codex)
- **Failed tokens**: tokens consumed by requests that failed (wasted spend)
- **Per-tier / per-model**: breakdown by routing tier and actual backend model
- **Failure types**: categorized by error type (tool_call_error, api_error, stream_error)
- **Latency**: average request duration

---

## Cost tracking and spend caps

Proxy request costs are logged to `~/.forge/costs/requests/` as JSONL. Forge subprocess verb costs are logged to
`~/.forge/costs/verbs/` as best-effort attribution records.

```bash
forge proxy costs                    # Today's costs, by verb
forge proxy costs --by-model         # Today's costs, by model
forge proxy costs --period week      # This week
forge proxy costs openrouter-anthropic         # Filter by proxy
```

Set caps on the proxy:

```bash
forge proxy set openrouter-anthropic costs.caps.per_day=20.00
forge proxy set openrouter-anthropic costs.caps.per_month=100.00
forge proxy set openrouter-anthropic costs.cap_mode=strict
forge proxy set openrouter-anthropic costs.on_cap_hit=warn
```

`cap_mode=post` blocks only after logged spend reaches a cap. `cap_mode=strict` also estimates the pending request
before forwarding it. `on_cap_hit=reject` returns HTTP 429 with `spend_cap_exceeded`; `on_cap_hit=warn` lets the request
continue and returns `X-Spend-Warning`.

Cap enforcement is process-local and best-effort. For reliable cap enforcement, run a single proxy process per proxy ID.
Cost logs accumulate in `~/.forge/costs/` — safely delete old JSONL files to reclaim space; the proxy re-bootstraps from
remaining logs at next startup.

---

## Prerequisites

- **Claude Code >= 2.1.81** -- required for `--bare` (used by workflow subprocesses for faster startup). Older versions
  produce `--bare: unknown option` errors.

---

## Troubleshooting

### "I changed my session but the proxy didn't change models"

That's expected. Sessions don't control proxy routing.

- Verify you launched Claude with the intended proxy (`forge claude start --proxy <id>`)
- Verify the proxy is healthy (`forge proxy list` / `GET /`)

### "A proxy is running but `forge proxy list` doesn't show it"

Re-create with `forge proxy create <template>` to register it.

### "I put tier→model in ~/.forge/config.yaml and nothing changed"

`~/.forge/config.yaml` is not for routing configuration. Per-proxy config belongs in
`~/.forge/proxies/<proxy_id>/proxy.yaml`.

### Where do I configure routing?

**In your proxy file:** `~/.forge/proxies/<proxy_id>/proxy.yaml`

Or **customize the template** before creating proxies: `forge proxy template edit <name>` creates a user copy at
`~/.forge/templates/<name>.yaml` that overrides the built-in. Future proxies created from that template will use your
customized version.

NOT in:

- Session files (cannot modify routing)
- `~/.forge/config.yaml` (not for routing; use per-proxy file or template)

---

## Advanced

### Proxy file anatomy (authoritative)

| File                                     | Purpose                                           |
| ---------------------------------------- | ------------------------------------------------- |
| `~/.forge/proxies/<proxy_id>/proxy.yaml` | Per-proxy configuration                           |
| `~/.forge/proxies/index.json`            | Registry of all proxies (name, port, pid, status) |
| `~/.forge/templates/<name>.yaml`         | User-customized templates (overrides built-in)    |
| `src/forge/config/defaults/templates/`   | Built-in templates (shipped with Forge)           |

### What `forge proxy create` actually does

The create command implements **reuse/adopt/spawn** logic:

1. **Reuse**: Check registry for existing healthy proxy with matching template
2. **Adopt**: Check expected default port for orphan proxy (not in registry)
3. **Spawn**: Start new proxy if neither exists

### Runtime truth

The proxy `GET /` endpoint is the authoritative source for:

- Proxy identity
- Tier→model mappings
- Current health status
- Runtime metrics (requests, tokens, latency)

File caches (index.json, proxy.yaml) are convenience; proxy state is truth.

### Gotchas

| Trap                                    | Explanation                                                |
| --------------------------------------- | ---------------------------------------------------------- |
| "Edited proxy.yaml but nothing changed" | Restart proxy or re-create for changes to take effect      |
| "Proxy says healthy but proxy is dead"  | Run `forge proxy clean` to clean stale entries             |
| "Can't find my proxy"                   | Check `~/.forge/proxies/index.json` for registered proxies |
