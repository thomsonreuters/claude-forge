<!-- prereq: 0.3 -->

## 4. Proxy Management

### 4.1 List Proxies and Templates

<!-- auto -->

```bash
# List existing proxies (none yet)
forge proxy list

# Expected: "No proxies found." + tip to run 'forge proxy template list'
# Templates are listed via the template subcommand, not inline in proxy list.

# List available templates
forge proxy template list
```

- [ ] `forge proxy list` shows "No proxies found." when none exist
- [ ] `forge proxy list` shows tip to run `forge proxy template list`
- [ ] `forge proxy template list` shows available templates (13 user-facing: litellm-anthropic, litellm-anthropic-local,
  litellm-gemini, litellm-gemini-flash-local, litellm-gemini-local, litellm-openai, litellm-openai-codex-local,
  litellm-openai-local, openrouter-anthropic, openrouter-gemini, openrouter-gemini-flash, openrouter-openai,
  openrouter-openai-codex)
- [ ] Internal test-only templates (e.g., litellm-gemini-test) are hidden from the default list

### 4.2 Create a Proxy

<!-- auto -->

```bash
# Clean up from previous runs
forge proxy delete litellm-gemini --force 2>/dev/null || true
forge proxy delete litellm-openai --force 2>/dev/null || true
forge proxy delete test-proxy-nostart --force 2>/dev/null || true

# Create a named remote proxy used by downstream session/review steps
forge proxy create litellm-gemini --name litellm-gemini

# Create a named review proxy with per-tier overrides
forge proxy create litellm-openai --name litellm-openai --opus-reasoning high --sonnet-temperature 0.7

# Create config only (don't start the server)
forge proxy create litellm-openai --no-start --name test-proxy-nostart

# List proxies again
forge proxy list
```

- [ ] Named proxies `litellm-gemini` and `litellm-openai` created successfully (or note if skipped)
- [ ] Named proxies and `test-proxy-nostart` appear in the list with expected base_url/port information
- [ ] Per-tier overrides applied to `litellm-openai`

### 4.3 Show Proxy Details

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# Show details of a specific proxy (created in 4.2)
forge proxy show test-proxy-nostart
```

- [ ] Shows template, base_url, tier mappings
- [ ] Shows proxy configuration YAML (port, tiers, provider settings)

### 4.4 Proxy Edit and Validate

<!-- prereq: 4.2 -->

<!-- human:guided -->

In the **container shell**, run these commands to view, edit, validate, and delete a proxy. The `edit` command opens
`$EDITOR` — verify it launches.

```
# View proxy config
forge proxy show <proxy_id>

# Edit proxy config (opens in $EDITOR)
forge proxy edit <proxy_id>

# Validate proxy config
forge proxy validate <proxy_id>

# Delete a proxy
forge proxy delete <proxy_id>
```

- [ ] `show` displays full proxy configuration
- [ ] `edit` opens proxy.yaml in editor
- [ ] `validate` reports config health
- [ ] `delete` removes proxy and cleans up registry

### 4.5 Proxy Clean

<!-- auto -->

```bash
# Clean up stale proxies (dead processes)
forge proxy clean
```

- [ ] Clean removes stale entries (or reports none found)

### 4.6 Launch Session with Host Proxy

<!-- prereq: 2.4, 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, create a session bound to a proxy, then launch Claude through the proxy.

```
# Clean up from previous runs
forge session delete proxy-session --force 2>/dev/null || true

# Create a session bound to the proxy created in 4.2 (accepts proxy_id or template name)
forge session start proxy-session --proxy litellm-openai --no-launch

# Verify session recorded proxy identity
cat .forge/sessions/proxy-session/forge.session.json | jq '.intent.proxy'
# Should show template and base_url fields
```

- [ ] `--proxy` binds session to the named proxy
- [ ] Session manifest `.intent.proxy` shows template and base_url

Now launch Claude through the named proxy. This opens an interactive Claude session — exit with Ctrl-C or `/exit` when
done verifying.

```
# Launch Claude through the running proxy created in 4.2
forge claude start --proxy litellm-openai -- --debug
# Claude should start with ANTHROPIC_BASE_URL pointing to the proxy
# Verify by checking the status line or running: echo $ANTHROPIC_BASE_URL inside Claude
# Exit Claude when done (Ctrl-C or /exit)
```

- [ ] `forge claude start --proxy` starts Claude routed through the named proxy
- [ ] Status line shows proxy info (template, tier mappings) when running with proxy

### 4.7 Live % Commands in Proxy Session

<!-- prereq: 2.4, 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

Now launch Claude (or reuse the session from 4.6):

```
forge claude start --proxy litellm-openai -- --debug
```

In the **live Claude session**, type these prompts:

```
%help
%session list
%proxy list
%proxy show litellm-openai
```

- [ ] `%help` returns help text listing available `%` commands
- [ ] `%session list` shows sessions (including proxy-session)
- [ ] `%proxy list` shows proxies from inside the session
- [ ] `%proxy show` displays proxy details (template, tier mappings)
- [ ] Commands are intercepted by `UserPromptSubmit` hook (not passed to Claude as prompts)

Exit the Claude session when done.

### 4.8 Proxy Delete UX (Confirmation + Smart-Pointer Semantics)

<!-- prereq: 4.2, 4.6 -->

<!-- human:guided -->

Test that `forge proxy delete` requires confirmation, shows the related shared-port items, and uses smart-pointer
semantics (only kills the server when deleting the last registry entry for that port).

In the **container shell**:

```
# Clean up from previous runs
forge proxy delete delete-test-proxy --force 2>/dev/null || true

# Create an alias on the same shared port as litellm-openai
forge proxy create litellm-openai --no-start --name delete-test-proxy

# Try to delete the alias -- should prompt for confirmation and list the related proxy entry
forge proxy delete delete-test-proxy
# Choose N to cancel
# Expected:
# - confirmation prompt appears
# - related proxies on the same port are listed (including litellm-openai)
# - no false warning about proxy-session/proxy-session-url just because they share port 8085

# Verify alias still exists after cancelling
forge proxy list
# Expected: delete-test-proxy still listed

# Now confirm deletion of the alias
forge proxy delete delete-test-proxy
# Choose y to confirm
# Expected:
# - "Deleted proxy 'delete-test-proxy'"
# - shared server references are kept alive via litellm-openai

# Verify alias gone but litellm-openai still present
forge proxy list

# Finally test deleting the last alias on that port
forge proxy delete litellm-openai
# Choose N to cancel
# Expected:
# - warning lists related sessions on http://localhost:8085 (for example proxy-session / proxy-session-url)
# - prompt makes clear this is the last shared-port proxy
```

- [ ] `forge proxy delete` prompts for confirmation (not auto-deleted)
- [ ] Deleting a non-terminal alias lists the related proxy entries sharing that port
- [ ] Choosing N cancels the delete; alias still in `forge proxy list`
- [ ] Choosing y deletes the alias while keeping the shared server alive
- [ ] Deleting the last alias lists the related sessions affected on that port
- [ ] No false warnings about sessions when deleting a non-terminal alias that merely shares the same port

### 4.9 Template Management

<!-- auto -->

```bash
# List available templates
forge proxy template list

# Show a template
forge proxy template show litellm-openai-local

# Raw YAML output (no syntax highlighting)
forge proxy template show litellm-openai-local --raw
```

- [ ] `forge proxy template list` shows all templates with source labels (built-in / customized)
- [ ] `forge proxy template show` displays template YAML
- [ ] `--raw` outputs plain YAML

### 4.10 Show Raw YAML for a Proxy Instance

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# Show raw YAML for an existing proxy instance (created earlier)
forge proxy show test-proxy-nostart --raw
```

- [ ] Proxy instance YAML printed (no syntax highlighting)
- [ ] YAML includes the expected template/provider fields

### 4.11 Set and Validate Proxy Config (No Editor)

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# Mutate a single value via CLI (no interactive editor)
forge proxy set test-proxy-nostart default_tier=opus

# Validate after mutation
forge proxy validate test-proxy-nostart
```

- [ ] `forge proxy set` succeeds
- [ ] `forge proxy validate` reports config is valid

### 4.12 Stop a Non-Running Proxy (Shared-Port Semantics)

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# test-proxy-nostart shares port 8085 with the running litellm-openai proxy.
# Smart-pointer semantics prevent stopping the shared server without --force.
forge proxy stop test-proxy-nostart 2>&1 || true

# Verify: error about shared port, not a silent no-op
forge proxy stop test-proxy-nostart 2>&1; echo "EXIT=$?"
```

- [ ] Command refuses to stop: reports other proxies share the port
- [ ] Exit code is non-zero (shared-port conflict)

### 4.13 Proxy Metrics (Running Proxy)

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# Metrics for a running proxy (litellm-gemini created in 4.2)
forge proxy metrics litellm-gemini

# JSON output
forge proxy metrics litellm-gemini --json

# All proxies
forge proxy metrics --all

# All proxies JSON (must be a single valid JSON object)
forge proxy metrics --all --json
```

- [ ] `forge proxy metrics` displays request counts, token totals, per-tier breakdown
- [ ] Per-tier breakdown includes avg latency
- [ ] `--json` outputs valid parseable JSON
- [ ] `--all --json` outputs a single valid JSON object (not one per proxy)
- [ ] Unreachable proxies show `null` in `--all --json` output

### 4.14 Proxy Metrics (Not Found / Shared-Port)

<!-- auto -->

```bash
# Metrics for a non-existent proxy (not in registry)
forge proxy metrics nonexistent-proxy 2>&1; echo "EXIT=$?"

# Metrics for test-proxy-nostart: shares port 8085 with litellm-openai,
# so smart-pointer semantics mean it reports metrics from the shared server.
forge proxy metrics test-proxy-nostart
```

- [ ] Non-existent proxy shows error and exits non-zero
- [ ] Shared-port proxy (test-proxy-nostart) returns metrics from the shared server (exit 0)

### 4.15 Backend List (Proxy Dependency)

<!-- auto -->

```bash
# List running backend instances (LiteLLM, etc.)
forge backend list
```

- [ ] Shows "No backends found." (or lists running backends)
- [ ] Command suggests `forge backend create litellm` when empty

### 4.16 Backend Create (LiteLLM Config)

<!-- auto -->

```bash
# Create backend config (shared by all instances)
forge backend create litellm

# Show config + status (even if not running)
forge backend show litellm-4000 --raw
```

- [ ] Backend config created (or reports it already exists)
- [ ] `forge backend show` displays config YAML

### 4.17 OpenRouter Templates

<!-- auto -->

```bash
# List all templates -- should now include OpenRouter alongside LiteLLM
forge proxy template list

# Show each OpenRouter template
forge proxy template show openrouter-anthropic
forge proxy template show openrouter-openai
forge proxy template show openrouter-openai-codex
forge proxy template show openrouter-gemini
forge proxy template show openrouter-gemini-flash
```

- [ ] `forge proxy template list` shows 13 user-facing templates total (8 litellm + 5 openrouter)
- [ ] `openrouter-anthropic` maps tiers to Claude models (haiku=claude-haiku-4.5, sonnet=claude-sonnet-4.6,
  opus=claude-opus-4.6)
- [ ] `openrouter-openai` maps tiers to GPT models (haiku=gpt-5.4-mini, sonnet=gpt-5.5, opus=gpt-5.5)
- [ ] `openrouter-openai-codex` maps tiers to Codex models (haiku=gpt-5.1-codex-mini, sonnet=gpt-5.3-codex,
  opus=gpt-5.5)
- [ ] `openrouter-gemini` maps tiers to Gemini models (haiku=gemini-2.5-flash, sonnet=gemini-3.1-pro-preview,
  opus=gemini-3.1-pro-preview)
- [ ] `openrouter-gemini-flash` maps all tiers to gemini-2.5-flash with tier_overrides for reasoning_effort
  (low/medium/high)
- [ ] Each OpenRouter template has a distinct default_port (8095-8099)

### 4.18 OpenRouter Proxy Create

<!-- auto -->

```bash
# Clean up from previous runs
forge proxy delete openrouter-test --force 2>/dev/null || true

# Create an OpenRouter proxy without starting it (no OPENROUTER_API_KEY needed for config-only)
forge proxy create openrouter-anthropic --name openrouter-test --no-start

# Show proxy details
forge proxy show openrouter-test

# Show raw YAML
forge proxy show openrouter-test --raw
```

- [ ] OpenRouter proxy created from template (exit 0)
- [ ] `forge proxy show` or `forge proxy validate` displays `Provider: openrouter`
- [ ] Raw YAML shows `provider: openrouter` and tier mappings with `anthropic/` prefixed model IDs
- [ ] Proxy uses port 8095 (openrouter-anthropic default)

### 4.19 Model Alternatives

<!-- prereq: 4.18 -->

<!-- auto -->

```bash
# Check model_alternatives in the openrouter-anthropic template
forge proxy template show openrouter-anthropic --raw | grep -A3 model_alternatives

# Check instance inherits alternatives
forge proxy show openrouter-test --raw | grep -A3 model_alternatives

echo "---"

# Clean up
forge proxy delete openrouter-test --force 2>/dev/null || true
```

- [ ] Template YAML includes `model_alternatives` section under opus tier
- [ ] Opus alternative maps `claude-opus-4-7` to `anthropic/claude-opus-4.7`
- [ ] Proxy instance inherits `model_alternatives` from template
- [ ] `openrouter-test` proxy cleaned up

---
