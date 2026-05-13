# Design Appendix (Reference Details)

**Companion to [design.md](design.md).** Precision reference material extracted to keep the main doc focused on
architectural narrative. Each section notes its origin for cross-referencing.

---

## A. Configuration Reference

Extracted from [design.md §3.6](design.md#36-configuration-system). Core definitions, ownership invariants, and proxy
lifecycle UX remain in design.md. This section covers detailed schemas, templates, and operational guidance.

### A.1 Proxy overlay schema (§3.6.4 — user edit surface)

The **only** user-editable config for routing defaults:

```yaml
# ~/.forge/proxies/<proxy_id>/proxy.yaml
proxy:
  default_tier: sonnet                    # Top-level tier default
  litellm:                                # Provider-namespaced overrides
    tier_overrides:
      sonnet:
        reasoning_effort: medium
        temperature: 0.7
        max_tokens: 8192
      opus:
        reasoning_effort: high
        thinking_budget_tokens: 16384
        max_tokens: 16384
      haiku:
        temperature: 0.3
        max_tokens: 4096
    model_alternatives:                   # Per-tier alternative backend mappings
      opus:
        claude-opus-4-7: anthropic/claude-opus-4-7
```

**Note:** All hyperparameters are per-tier because each model has different limits and optimal defaults.

**Precedence chain** (first non-null wins):

1. Request explicit value (e.g., `temperature` in API call)
2. Per-tier override (`proxy.<provider>.tier_overrides.<tier>.*`)
3. Model catalog default (built-in per-model defaults)

> **Implementation note:** Internally, config is layered (base defaults -> proxy defaults -> template overlay -> proxy
> overlay -> env). Users only edit the proxy overlay. `validate_user_config()` enforces this by rejecting proxy-owned
> and template-owned keys in `~/.forge/config.yaml`.

**Note:** Provider/base_url/template are set when the proxy is created. The per-proxy overlay only tunes defaults
**within** that proxy's routing scope.

### A.2 Proxy templates vs user-defined proxies (§3.6.5)

**Proxy templates** (internal, pre-canned configurations):

| Template                  | Use case                                    |
| ------------------------- | ------------------------------------------- |
| `openrouter-anthropic`    | Claude models via OpenRouter (direct)       |
| `openrouter-deepseek`     | DeepSeek models via OpenRouter (direct)     |
| `openrouter-glm`          | GLM / Z.ai models via OpenRouter (direct)   |
| `openrouter-kimi`         | Kimi models via OpenRouter (direct)         |
| `openrouter-minimax`      | MiniMax models via OpenRouter (direct)      |
| `openrouter-openai`       | GPT models via OpenRouter (direct)          |
| `openrouter-qwen`         | Qwen models via OpenRouter (direct)         |
| `openrouter-gemini`       | Gemini models via OpenRouter (direct)       |
| `openrouter-openai-codex` | OpenAI Codex via OpenRouter (direct)        |
| `openrouter-gemini-flash` | Gemini Flash via OpenRouter (cheap, direct) |
| `litellm-openai`          | OpenAI models via remote/shared LiteLLM     |
| `litellm-gemini`          | Gemini models via remote/shared LiteLLM     |
| `litellm-anthropic`       | Anthropic models via remote/shared LiteLLM  |
| `litellm-gemini-local`    | Local LiteLLM + Gemini API key              |
| `litellm-anthropic-local` | Local LiteLLM + Anthropic API key           |

A proxy template is an operational profile:

- Location: `src/forge/config/defaults/templates/*.yaml`
- Defines: `proxy.preferred_provider`, `proxy.default_port`, tier->model mappings, `tier_overrides`
- **NOT a user edit surface** -- clone into a proxy to customize

**User-defined proxies:**

Currently, set overrides at create time:

```bash
forge proxy create openrouter-openai --opus-reasoning high
```

Create-and-edit pattern:

```bash
forge proxy create openrouter-openai --name my-high-reasoning
forge proxy edit my-high-reasoning
```

**Principle:** Create from template, then edit (don't modify internals).

### A.3 Confusion traps / anti-patterns (§3.6.6)

| Anti-pattern                            | Why it fails                                                                        |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| "Session changes routing"               | Proxy cannot apply per-session routing without a stable session ID in requests.     |
| "Global config changes tier->model"     | Tier->model mapping is defined by proxy templates/proxies only.                     |
| "Proxy overlay in ~/.forge/config.yaml" | Wrong location. Per-proxy overlays belong under `~/.forge/proxies/<id>/proxy.yaml`. |

YAML config ignores `null` (no-op); session overrides (JSON) use `null` to clear fields. Do NOT share override
implementations.

### A.4 Runtime truth vs files (§3.6.7)

Status line should read live proxy truth when available; clearly label file fallbacks (see design.md §3.7).

### A.5 Model catalog (§3.6.8)

The model catalog is **authoritative internal data**:

- Location: `src/forge/core/data/model_catalog.yaml`
- Defines: model capabilities, context windows, provider mappings
- **NOT a user edit surface**

### A.6 Credentials and Connection Values (§3.6.9)

Credentials resolve from environment variables first (`.env`, shell exports), then fall back to the Forge credential
store (`~/.forge/credentials.yaml`, managed by `forge auth login`). Env vars override stored credentials unless
`auth_ignore_env` is set in `~/.forge/config.yaml`.

Five atomic credentials (defined in `forge.core.auth.capabilities`):

| Credential       | Env var(s)                             | Capabilities                                        |
| ---------------- | -------------------------------------- | --------------------------------------------------- |
| `openrouter`     | `OPENROUTER_API_KEY`                   | All `openrouter-*` proxies, OSS workflow models     |
| `anthropic-api`  | `ANTHROPIC_API_KEY`                    | Forge subprocesses, `litellm-anthropic-local` proxy |
| `openai-api`     | `OPENAI_API_KEY`                       | `litellm-openai-local` proxy                        |
| `gemini-api`     | `GEMINI_API_KEY`                       | `litellm-gemini-local` proxy                        |
| `litellm-remote` | `LITELLM_API_KEY` + `LITELLM_BASE_URL` | All remote `litellm-*` proxy templates              |

`auth_ignore_env: true` in runtime config (`~/.forge/config.yaml`) skips all env vars for credential resolution. Both
the sync path (`resolve_env_or_credential`) and async path (`CredentialManager` via `EnvSecretsProvider`) respect the
flag. `build_claude_env()` hydrates credential-file values into subprocess env dicts when the flag is active.

**Rule:** Credential storage holds secrets and connection values (e.g., `LITELLM_BASE_URL`). Connection values are a
convenience fallback for bootstrapping proxy creation (`forge proxy create`). Once `proxy.yaml` exists, proxy-owned
routing is authoritative. Do NOT store other routing configuration in credential storage.

### A.7 Runtime config (§3.6.10 -- `~/.forge/config.yaml`)

Global Forge runtime preferences. **Separate from `ForgeConfig`** -- the proxy imports `forge.config.config` as a
singleton; runtime preferences must not leak into routing. Runtime config lives in `forge.runtime_config`.

```yaml
proxy_mode: host              # host | sidecar
sidecar_image: forge-sidecar:latest
user_agent_claude_code_version: ""
context_limit: 200000
status_timeout: 2.0
handoff_timeout: 300
log_level: off               # off | debug | info | warning
```

- **Optional**: missing file = built-in defaults
- **Auto-created on first access**: `forge config` / `forge config show` seeds the file with documented defaults
- **Fail-open**: invalid YAML warns, returns defaults
- **Unknown keys**: warned, ignored (forward compatible)
- **CLI**: `forge config` (show), `forge config show [--raw]`, `forge config set`, `forge config edit`,
  `forge config reset`; `%config` (read-only) in-session

See [docs/end-user/configs.md](end-user/configs.md) for the full user guide.

### A.7a Claude settings preset (`~/.forge/claude.preset.json`)

User-editable JSON merged into Claude Code `settings.json` by `forge extension enable`.

```json
{
  "hooks": {
    "...": "forge hook ..."
  },
  "statusLine": {
    "type": "command",
    "command": "forge status-line",
    "padding": 0
  },
  "permissions": {
    "allow": ["Write", "Edit"]
  }
}
```

- **Auto-created on first access**: `forge claude preset` / `forge claude preset show`
- **Built-in defaults are intentionally minimal**: hooks, status line, and handoff agent permissions
- **Merged keys only**: `hooks`, `statusLine`, `env`, and `permissions`
- **User customization surface**: usually permissions and extra env vars; hooks/status line only if intentionally
  overriding Forge defaults
- **Validation**: must be valid JSON object; corruption errors include recovery hints
- **CLI**: `forge claude preset` (show), `forge claude preset show [--raw]`, `forge claude preset edit`,
  `forge claude preset reset [--yes]`

See [docs/end-user/configs.md](end-user/configs.md) for the full user guide.

### A.8 Status line guidance (§3.6.11)

Status line reads three sources via env vars set at launch:

| Source         | Env Var                                | What it provides                   | Availability          |
| -------------- | -------------------------------------- | ---------------------------------- | --------------------- |
| Session file   | `FORGE_SESSION`                        | Intent, overrides, confirmed facts | Always (file)         |
| Proxy registry | `ANTHROPIC_BASE_URL` -> reverse lookup | proxy_id, template, port           | Always (file)         |
| Proxy `GET /`  | `ANTHROPIC_BASE_URL` -> query          | tier mappings, context windows     | Only if proxy running |

**Information strategy:**

1. **Session identity**: Read `FORGE_SESSION` -> locate `.forge/sessions/<name>/forge.session.json`
2. **Proxy identity**: Reverse lookup `ANTHROPIC_BASE_URL` in `~/.forge/proxies/index.json`
3. **Runtime truth**: Query proxy `GET /` for tier mappings and context windows (may fail gracefully)

**Note:** Status line does NOT get `session_id` from Claude Code (only hooks do); it relies on `FORGE_SESSION`.

**No CWD fallback:** If `FORGE_SESSION` is not set, the status line shows no session information. It does not scan CWD
for `.forge/` directories.

**Display sections:**

| Section | Shows                                             | Source           |
| ------- | ------------------------------------------------- | ---------------- |
| Proxy   | template, base_url, tier mappings, context window | Registry + Proxy |
| Session | policy/TDD mode, worktree, overrides summary      | Session file     |

**Labeling:** Proxy info is authoritative for routing. Session info is authoritative for workflow.

### A.9 Proxy cost configuration and logs (§3.14)

Per-proxy cost controls live in the user-owned proxy file:

```yaml
# ~/.forge/proxies/<proxy_id>/proxy.yaml
costs:
  caps:
    per_day: 20.00
    per_month: 100.00
  cap_mode: post
  on_cap_hit: reject
```

| Field                  | Values           | Meaning                                                                 |
| ---------------------- | ---------------- | ----------------------------------------------------------------------- |
| `costs.caps.per_day`   | positive USD     | Rolling 24-hour cap                                                     |
| `costs.caps.per_month` | positive USD     | Calendar-month cap                                                      |
| `costs.cap_mode`       | `post`, `strict` | `post` checks accumulated spend; `strict` includes a preflight estimate |
| `costs.on_cap_hit`     | `reject`, `warn` | `reject` returns 429; `warn` adds `X-Spend-Warning` and continues       |

CLI updates use the normal proxy edit surface:

```bash
forge proxy set openrouter-anthropic costs.caps.per_day=20.00
forge proxy set openrouter-anthropic costs.cap_mode=strict
forge proxy set openrouter-anthropic costs.on_cap_hit=warn
```

Runtime logs:

| Path                              | Schema owner                        | Retention policy        |
| --------------------------------- | ----------------------------------- | ----------------------- |
| `~/.forge/costs/requests/*.jsonl` | `forge.proxy.cost_logger`           | Append-only, user-prune |
| `~/.forge/costs/verbs/*.jsonl`    | `forge.core.reactive.cost_tracking` | Append-only, user-prune |

Request records contain timestamp, proxy ID, model/tier, token counts, cost in microdollars, request ID, latency, and
pricing source. Verb records contain timestamp, verb name, proxy URL/ID when known, before/after snapshots, total cost
delta, request count delta, and `estimated=true`.

The proxy `GET /` endpoint reports in-memory metrics and cost totals for live status. The JSONL request logs remain the
bootstrap source for cap enforcement after restart.

---

## B. Direct Command Reference

Extracted from [design.md §3.11](design.md#311-direct-commands-userpromptsubmit-dispatcher). Design goal, mechanism, and
scope rationale remain in design.md.

### B.1 Scope policy table

| Category             | Allowed via `%`                                                                                                | Not allowed via `%`                                           |
| -------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Session / plan       | `%session list`, `%plan`                                                                                       | --                                                            |
| Proxy                | `%proxy list`, `%proxy show` (read-only)                                                                       | `%proxy create`, `%proxy edit`, `%proxy set`, `%proxy delete` |
| Guard / verification | `%guard status`, `%guard enable`, `%guard disable`, `%guard check`, `%guard supervise`, `%cancel-verification` | --                                                            |
| Cleanup              | `%clean [--scope repo\|project\|all]` (read-only report)                                                       | destructive cleanup (use `forge clean --yes` from terminal)   |
| Utilities / config   | `%h`, `%help`, `%config`                                                                                       | --                                                            |

### B.2 Current shipped commands

%-only utilities:

- `%h` / `%help`: show direct command help
- `%config`: show effective runtime config (read-only)

Shared commands (mirrors CLI syntax):

- `%session list` (calls the same command-core op as `forge session list`)
- `%plan` (shows the current session's recorded plan file path)
- `%proxy list` (read-only: shows available proxies)
- `%proxy show <id>` (read-only: shows proxy details and tier mappings)
- `%guard status` (shows current policy config and state)
- `%guard enable --bundle tdd [--permissive]` (enables policy enforcement)
- `%guard disable` (disables all policies for the session)
- `%guard check [--staged] [--bundle <name>]` (diagnostic policy evaluation against git diff)
- `%guard supervise <target>` (set supervisor), `off` (suspend), `on` (resume), `remove` (delete)
- `%guard supervise reload [path]` (reload latest approved plan, or from explicit path)
- `%cancel-verification` (bypasses the active Stop-hook verification loop)
- `%clean [--scope repo|project|all]` (read-only: shows orphaned state report, default scope=project)

---

## C. Work Queue Internals

Extracted from [design.md §3.13](design.md#313-async-work-queue). Design goals and rationale remain in design.md.

### C.1 Marker schema (v2)

```json
{
    "schema_version": 2,
    "kind": "stop",
    "marker_id": "uuid-123",
    "forge_version": "0.9.0",
    "created_at": "2026-01-07T12:00:00Z",
    "payload": {
        "session_id": "uuid-123",
        "forge_root": "/abs/path/to/forge/project",
        "project_root": "/abs/path/to/repo",
        "session_name": "my-session",
        "transcript_snapshot_rel": ".forge/artifacts/..."
    },
    "attempt_count": 0,
    "last_attempt_at": null,
    "last_error": null
}
```

**Key fields:** `kind` = routing key (which handler); `marker_id` = filename key (caller chooses idempotency, e.g.
session ID); `payload` = kind-specific data; `attempt_count`/`last_error` = retry tracking. Marker ID validated with
`^[A-Za-z0-9._-]+$`.

### C.2 Processing contract

Handlers are passed explicitly as a `handlers` dict (no global registry -- avoids import-order coupling and test state
leakage): `process_pending_work(handlers={"stop": handler, "index": handler})`.

| Outcome                             | Behavior                                                                |
| ----------------------------------- | ----------------------------------------------------------------------- |
| Handler succeeds                    | Delete marker under lock                                                |
| Handler raises                      | Keep marker, increment `attempt_count`, write `last_error` under lock   |
| Lock contention                     | Skip (another process holds it)                                         |
| No handler for kind                 | Skip, log warning (leave in place)                                      |
| `attempt_count >= MAX_ATTEMPTS` (5) | Move to `pending-work/failed/` (poison marker, preserved for debugging) |

### C.3 Known marker kinds

| Kind      | Producer            | Handler                             |
| --------- | ------------------- | ----------------------------------- |
| `stop`    | Stop hook           | No-op (delete only)                 |
| `index`   | Stop hook           | Index transcript for search         |
| `handoff` | Stop hook (planned) | Spawn handoff agent for memory docs |

---

## D. Policy Internals

Extracted from [design.md §4.1](design.md#41-policy-enforcement). Policy type definitions, supervisor mechanism,
verification loop, and action context remain in design.md.

### D.1 Shared library scope (from §4.1.2)

| Utility            | Extracted from                      | API                                                          |
| ------------------ | ----------------------------------- | ------------------------------------------------------------ |
| Session runner     | `supervisor.py`, `handoff_agent.py` | `run_claude_session(prompt, resume_id?, base_url?, timeout)` |
| Proxy resolution   | both                                | `resolve_base_url(proxy_id?, explicit_url?, fallbacks)`      |
| Throttle cache     | `guard/store.py`                    | `ThrottleCache(ttl).check(key) / .update(key, value)`        |
| Structured output  | `verdict.py`                        | `extract_json_verdict(stdout, schema)`                       |
| Tagger             | new                                 | `tag_action(context, model, prompt) -> tags[]`               |
| Env builder        | both                                | `build_claude_env(base_url?) -> dict`                        |
| Fan-out runner     | `src/forge/review/engine.py`        | `run_multi_review(prompt, models, per_worker_prompts?)`      |
| Adversarial runner | `src/forge/review/adversarial.py`   | `run_adversarial(proposal, skill_resource, stances, models)` |

### D.2 Example: Writing a new policy (from §4.1.2)

A developer adding a policy imports a few utilities and writes a class. Three abstract properties are required:
`policy_id`, `description`, and `intent` (see §4.1.1).

```python
# Example: block database migrations without review
from forge.core.reactive import tag_action, run_claude_session, ThrottleCache

class MigrationReviewPolicy(DeterministicPolicy):
    policy_id = "custom.migration_review"
    description = "Require review for database migrations"
    intent = "Prevent unreviewed schema changes from reaching production"

    def applies_to(self, ctx):
        return ctx.tool_name == "Write" and "migration" in (ctx.target_path or "")

    def _evaluate(self, ctx):
        tags = tag_action(ctx, model="haiku", prompt="Is this a schema migration? tags: migration | safe")
        if "migration" not in tags:
            return self._allow()
        verdict = run_claude_session(prompt=REVIEW_PROMPT.format(...), resume_id=config.resume_id)
        return verdict_to_decision(verdict)
```

On deny, the message includes the `intent` so models understand why the policy exists and can surface conflicts to the
user rather than working around the check.

### D.3 Policy definition ownership (from §4.1.6)

| Setting                                        | Owner   | Location                           |
| ---------------------------------------------- | ------- | ---------------------------------- |
| Supervisor model (which model to use as guard) | Proxy   | `~/.forge/proxies/<id>/proxy.yaml` |
| Throttling settings (check frequency)          | Proxy   | `~/.forge/proxies/<id>/proxy.yaml` |
| TDD mode (off/permissive/strict)               | Session | Session file `intent.tdd_mode`     |
| Policy enabled/disabled                        | Session | Session file `intent.policy_mode`  |
| Verification config                            | Session | Session file `intent.verification` |

### D.4 Policy state ownership (from §4.1.6)

| State                    | Owner           | Location                             |
| ------------------------ | --------------- | ------------------------------------ |
| Enforcement decisions    | Session (hooks) | `confirmed.policy` in session file   |
| Cached verdicts          | Session (hooks) | `confirmed.policy` in session file   |
| "Tests touched" tracking | Session (hooks) | `confirmed.policy` in session file   |
| Verification iteration   | Session (hooks) | `confirmed.verification.iterations`  |
| Last verification result | Session (hooks) | `confirmed.verification.last_result` |

---

## E. Install Model Reference

Extracted from [design.md §5.1-5.4](design.md#5-extensions-install-model). Overview remains in design.md.

### E.1 Scope model (§5.1 -- mirrors Claude Code)

| Scope       | Extensions Path                       | Settings Path                 | Use case                                           |
| ----------- | ------------------------------------- | ----------------------------- | -------------------------------------------------- |
| `--user`    | `~/.claude/{commands,agents,skills}/` | `~/.claude/settings.json`     | Personal global (default; prevents worktree drift) |
| `--project` | `.claude/{commands,agents,skills}/`   | `.claude/settings.json`       | Team-shared (checked in)                           |
| `--local`   | `.claude/{commands,agents,skills}/`   | `.claude/settings.local.json` | Personal per-project                               |

### E.2 Installable modules + profiles (§5.2)

| Module        | Installs                                           | Notes                                                        |
| ------------- | -------------------------------------------------- | ------------------------------------------------------------ |
| `commands`    | Slash commands markdown                            |                                                              |
| `agents`      | Subagents markdown                                 |                                                              |
| `skills`      | Skills (SKILL.md + resources/scripts)              | Scripting layer for Forge workflows (see §5.5)               |
| `hooks`       | Hook settings entries (invoke `forge hook ...`)    | No hook scripts installed; requires `hooks.*` settings merge |
| `status-line` | `statusLine` setting (invokes `forge status-line`) | No scripts installed; same pattern as hooks                  |
| `permissions` | Forge-required permission entries                  | Merged as unions                                             |

Profiles:

- `minimal`: `commands`
- `standard`: `commands`, `agents`, `skills`, `hooks`, `permissions`, `status-line` (default)
- `full`: all modules (same as standard; reserved for future heavy modules)

### E.3 Settings merge rules (§5.3 -- normative)

| Setting             | Merge behavior                                             |
| ------------------- | ---------------------------------------------------------- |
| `hooks.*`           | Append + dedupe by command path (invokes `forge hook ...`) |
| `permissions.allow` | Union unique entries                                       |
| `permissions.deny`  | Union unique entries                                       |
| `statusLine`        | Scalar merge; conflict fails unless `--force`              |
| `model`             | Never touched                                              |

All settings modifications must be backed up first (`settings.json.forge-backup`).

### E.4 Tracking file (§5.4 -- `~/.forge/installed.json`)

The installer must track what it changed so:

- `forge update` updates only tracked items
- `forge uninstall` removes only tracked files and reverts only Forge-added settings entries

### E.5 Multi-scope installation (§5.5 -- skill resolution)

Skills use `${CLAUDE_SKILL_DIR}` (a Claude Code built-in) to reference co-located resources. This variable resolves to
the directory of the **executing** SKILL.md, so each installation is self-contained -- resources always come from the
same scope as the SKILL.md that was invoked.

**Dual-scope behavior:** Installing Forge at two scopes (e.g., `--user` + `--project`) creates independent copies of
every skill. Each copy has its own SKILL.md, resources, and scripts. Forge does **not** deduplicate across scopes.

| Concern             | Behavior                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------- |
| Resource resolution | Safe: `${CLAUDE_SKILL_DIR}` is self-referential (no cross-scope mismatch)                 |
| Which copy runs     | Determined by Claude Code's scope precedence (not controlled by Forge)                    |
| Version skew        | If scopes are updated independently, one copy may be stale                                |
| Hook duplication    | Both scopes add hook entries to their respective settings files; hooks may fire from both |
| Uninstall           | Scope-specific: `forge extension disable` removes only the targeted scope                 |

**Recommendation:** Use a single scope per project. If both exist, disable one:

```bash
forge extension disable --user     # Remove user-level
forge extension enable --project   # Keep project-level only
```

---

## F. Workflow Runner and Skill Details

Extracted from [design.md §5.5.5-5.5.9](design.md#555-workflow-runners). Three-layer architecture, four runner types,
design principles, skills-vs-policies relationship, and CLI surfaces remain in design.md.

### F.1 Fan-out runner details (from §5.5.5)

`run_multi_review()` in `src/forge/review/engine.py`:

- N workers, each with model/proxy via `ModelSpec`
- Per-worker prompt via `ModelSpec.prompt`
- Per-worker context: `--context resume:<id>` or `--context blind`
- Direct Claude workers use `ANTHROPIC_MODEL` plus `ANTHROPIC_DEFAULT_*_MODEL`, not Claude CLI `--model`
- Parallel via `ThreadPoolExecutor` + process group cleanup
- `/forge:analyze`: single-model fan-out with an analyze resource

### F.2 Adversarial runner details (from §5.5.5)

Adversarial runner:

- Constrained to review/eval skills (stance injection)
- Inject stance via `{stance_prompt}` in resources
- Mandatory blinding: proposal + files only (no peer outputs)
- Stances: for/against/neutral with guardrails (lens, not honesty)
- Synthesize agreement vs disagreement; evidence-weighted recommendation

Adversarial-compatible skills include `{stance_prompt}` in their resource .md; `/forge:debate` enforces this.

### F.3 Panel engine details (from SS5.5.7)

Panel is the reference invocation of the fan-out runner. It fans out a review task to N models via different proxies and
collects independent findings for synthesis.

**Engine:** `forge workflow panel` CLI command.

Spawns N `claude -p` subprocesses, each with a different `ANTHROPIC_BASE_URL`. Each reviewer is a full Claude Code agent
-- it can read files, investigate, and find issues with real file:line evidence.

**Execution:** Fork mode gives each reviewer the main agent's full context. Summary mode sends a focused prompt.

**Target-based review:** Positional `target` argument loads a bundled review framework (docreview.md by default,
codereview.md with `--code`). Combined with per-worker prompt support (`ModelSpec.prompt`), this enables specialized
fan-out patterns -- code review, document review, security audit -- all using the same runner.

**Synthesis:** The main agent reads all N reviews and synthesizes -- identifying consensus findings (2+ models agree),
unique insights, and conflicts. Because the main agent has full project context, it can **investigate conflicts** by
reading the disputed code -- something external synthesis (which merges text without context) cannot do.

**`/forge:analyze` as degenerate fan-out:** Single-model fan-out with an analyze-specific resource. Same panel engine
with N=1. The resource instructs the model to act as a senior engineering collaborator with deep analysis guidelines.

**Dual use:** The panel serves as both a skill (`/forge:panel src/session/ --code`) and a policy (automatic multi-model
gate before committing). Same `run_multi_review()` function, two callers -- the programmer wires both.

### F.4 Debate / adversarial reference skill (from SS5.5.8)

Debate is the reference invocation of the adversarial runner. It assigns stances to workers, blinds them from each
other, and synthesizes by weighing agreement against disagreement.

**Stances:** Each worker receives a stance directive (for/against/neutral) injected via `{stance_prompt}` in the
evaluation template. Stances influence the evaluative lens, not honesty -- all stances include ethical guardrails that
override positional framing (a "for" evaluator must still flag genuine critical issues).

**Blinding:** Each worker sees only the original proposal + files + stance prompt. Workers never see other workers'
output. Achieved by spawning separate `claude -p` processes without `--resume` (no shared session context).

**Skill constraint:** Only review/evaluation skills are adversarial-compatible. The runner checks for a
`{stance_prompt}` marker in the evaluation resource and rejects resources without it. This prevents misuse (adversarial
code generation makes no sense).

**Templates:** Two debate evaluation frameworks (embedded in CLI): a proposal evaluation template (7-point: feasibility,
correctness, trade-offs, risks, completeness, alternatives, recommendation) and a code evaluation template (5-point:
quality, security, performance, architecture, risks). `--code` selects the code template. Both produce structured
verdict output (Verdict/Confidence/Key Findings).

**Execution flow:** Parse subject -> select template (proposal or code via `--code`) -> fill template with subject ->
write to temp file -> N x adversarial runner with stance injection -> collect results -> synthesize (agreement areas,
disagreement areas, evidence-weighted recommendation). Temp file cleaned up via try/finally.

### F.5 Operational constraints (from §5.5.9)

**Recursion guard:** Skills invoke `forge` commands. `forge` commands spawn `claude -p` subprocesses. Those subprocesses
trigger hooks. If a hook spawns another subprocess, you get recursion. `build_claude_env()` sets `FORGE_DEPTH` (starting
at 0, incremented per subprocess layer). Hooks that spawn subprocesses (supervisor, handoff agent) skip at depth >= 2.

**JSON output contract:** `forge` commands invoked by skills must support `--json` for structured output. Skills should
never parse human-readable CLI text -- it drifts. JSON schemas are the API contract between skills and CLI.

**Child process lifecycle:** Parallel fan-out (panel runner) spawns N `claude -p` processes. If the parent is killed
(Ctrl+C), children must be terminated via process group signal (`os.killpg`). All child processes must have timeouts
(the `timeout_seconds` parameter in `run_claude_session()`).

**Skill script dependency tiers:** Skills are installed by file copy (`forge extension enable`), not as Python packages.
Scripts in `skills/*/scripts/` have no access to `forge.*` imports or third-party deps. Three tiers handle this:

| Tier               | When                                                | How                                                    | Example                                |
| ------------------ | --------------------------------------------------- | ------------------------------------------------------ | -------------------------------------- |
| Pure stdlib        | Script needs only Python builtins                   | `python3 script.py`                                    | `walkthrough-state.py`                 |
| Forge CLI command  | Script needs `forge.*` or third-party deps          | `forge <group> <cmd>`                                  | `forge hook stop`, `forge status-line` |
| `uv run` + PEP 723 | Script needs 1-2 external deps, not worth a CLI cmd | `uv run script.py` with inline `# /// script` metadata | --                                     |

**Graduation rule:** When a pure-stdlib script needs deps, promote it to a Forge CLI command (one step, no intermediate
stages). This follows the hooks pattern: `forge hook <name>` runs as a CLI command with full package deps, not as an
installed script. The same principle applies to skill scripts.

---

## G. Memory Doc Reference

Extracted from [design.md §5.6.3-5.6.6](design.md#56-designated-memory-docs). Philosophy, handoff agent concept, and two
operating modes remain in design.md.

### G.1 Strategy registry (from §5.6.3)

**Direct update strategies** (Mode 1):

| Strategy        | Behavior                                      |
| --------------- | --------------------------------------------- |
| `project-state` | Update focus, active work, decisions, handoff |
| `checklist`     | Mark `[x]` completed, add discovered tasks    |
| `changelog`     | Add accomplishments, follow existing format   |
| `debugging`     | Record error causes + solutions               |
| `patterns`      | Record architecture patterns + conventions    |
| `generic`       | Add any new information (default fallback)    |

**Shadow strategy** (Mode 2):

| Strategy    | Behavior                                                               |
| ----------- | ---------------------------------------------------------------------- |
| `suggested` | Propose additions to official doc as `- [ ]` checkboxes with rationale |

### G.2 Example configuration (from §5.6.4)

```yaml
memory:
  auto_update:
    enabled: true
    min_turns: 5
  designated_docs:
    # Direct update (Mode 1) -- agent maintains these
    - path: docs/checklist.md
      strategy: checklist
    - path: docs/changelog.md
      strategy: changelog
    - path: .forge/memory/debugging.md
      strategy: debugging
    - path: .forge/memory/patterns.md
      strategy: patterns

    # Shadow/propose (Mode 2) -- human reviews and merges
    - path: .forge/memory/suggested_coding_standards.md
      strategy: suggested
      shadows: docs/developer/coding-standards.md
    - path: .forge/memory/suggested_testing.md
      strategy: suggested
      shadows: docs/developer/testing-guidelines.md
```

All docs are processed in one `claude -p` call with per-doc strategy instructions (and official-doc-first for shadows).

### G.3 Worktree resolution (from §5.6.5)

Managed sessions always launch from `forge_root`. The handoff agent resolves designated doc paths relative to
`forge_root`, so git-tracked docs (e.g., `docs/checklist.md`) target the correct branch when working in a worktree.

Trackedness is controlled by path choice; the agent doesn't distinguish:

- `docs/checklist.md` -> git-tracked, branch-specific (moves with the branch)
- `.forge/memory/debugging.md` -> untracked, per-Forge-project (`.forge/` is in `.gitignore`)
- `docs/suggested/coding_standards.md` -> git-tracked shadow doc (visible in PRs if desired)

Shadow docs also resolve relative to `forge_root`, so the agent reads the branch-correct official doc.

**Transcript path handling:** Transcripts live under `<forge_root>/.forge/artifacts/`. Because `cwd` is `forge_root`,
transcript paths in the prompt must be **absolute**; designated doc paths remain relative (resolved against `cwd`).

> **Note:** Artifacts (transcripts/plans) consolidate at `forge_root` for per-project visibility. Designated docs are
> working documents and belong with branch content.

### G.4 Comparison with Claude Code auto-memory (from §5.6.6)

Claude Code (Feb 2026) ships **auto-memory**: Claude writes free-form notes to `~/.claude/projects/<project>/memory/`
during sessions. `MEMORY.md` (first 200 lines) loads at startup; topic files load on demand.

Forge's handoff agent is complementary, not competitive:

| Aspect          | Auto-Memory                  | Handoff Agent                              |
| --------------- | ---------------------------- | ------------------------------------------ |
| Timing          | During session (incremental) | After session (retrospective)              |
| Signal quality  | In-the-moment judgment       | Full-session hindsight                     |
| Structure       | Free-form, model-organized   | Per-doc strategies with constraints        |
| Target files    | User-local memory dir        | Project docs (repo-tracked, shareable)     |
| Curation        | None -- entries accumulate   | Shadow pattern provides human review gate  |
| Graduation path | None                         | Shadow doc -> human review -> official doc |

**Key design rationale:** Free-form capture relies on model judgment and tends to accumulate noise over time. The
handoff agent reduces this via (a) retrospective synthesis, (b) per-doc topic constraints, and (c) the shadow pattern
(human curation gate).

Auto-memory is better for long-lived preferences; the handoff agent is better for structured project docs and proposed
standards evolution.

**Deliberate non-integration:** The handoff agent does not read auto-memory (`~/.claude/projects/<project>/memory/`) as
input. It's outside the project root (containment guard), is free-form (hard to dedupe against structured strategies),
and targets different information (preferences/patterns vs project state/standards). Occasional duplication is cheaper
than cross-format deduplication. If overlap becomes painful, a small prompt tweak can address it.

---

## H. (Removed)

CLI patching was removed for the OSS release. Forge now uses the native `CLAUDE_CODE_AUTO_COMPACT_WINDOW` env var to
control compaction timing in proxy mode.

---

## I. Interactive Manual Testing

Extracted from [design.md §5.8](design.md#58-interactive-manual-testing). Rationale, three-skill table, three-window
model, and key design decisions remain in design.md. See also [testing-guidelines.md](developer/testing-guidelines.md)
for the full testing reference.

### I.1 Annotation types

| Annotation               | Session A does                                 | User does                              |
| ------------------------ | ---------------------------------------------- | -------------------------------------- |
| `<!-- auto -->`          | Runs command via wrapper, checks assertions    | Nothing                                |
| `<!-- human:confirm -->` | Runs command, shows output                     | Eyeballs output in Session A, confirms |
| `<!-- human:guided -->`  | Tells user what to do in Session B or Terminal | Does it, reports back to Session A     |
| `<!-- requires: X -->`   | Checks infra probe                             | Skip if unavailable                    |
| `<!-- destructive -->`   | Runs command (safe in sandbox)                 | Nothing                                |

### I.2 Wrapper abstraction

| Skill                | Wrapper                        | Isolation                        |
| -------------------- | ------------------------------ | -------------------------------- |
| `/forge:walkthrough` | `bash run-in-repo.sh <cmd>`    | env redirection + 4 safety gates |
| `/forge:qa`          | `docker exec $CONTAINER <cmd>` | OS-level container boundary      |

**Three-window model:** Session A prompts the user to open Terminal early. Session B is launched only when the checklist
first needs interactive verification.

### I.3 Per-skill details

**Smoke test** (`smoke-test.sh`): Read-only probes with mtime snapshot assertions. Not checklist-driven.

**Walkthrough** (checklist-driven via `run-in-repo.sh`): Annotated checklist (11 sections) covering install, verify,
guided exploration, proxy/session creation, live Claude session, and cleanup. Hermetic isolation via
`setup-test-repo.sh` (FORGE_HOME redirection, marker file, 4 safety gates in `run-in-repo.sh`).

**Full QA** (checklist-driven via `docker exec`): 312-item checklist split into per-section files
(`resources/checklist.md` index + `resources/checklist/*.md`, 20 sections). Includes `human:guided` items for
interactive verification. State tracking with `--from X.Y` resume. Separate skill prevents cross-mode contamination.

**Deterministic bookkeeper** (`walkthrough-state.py`): Shared script (both skills) that parses checklist markdown into
structured JSON. Seven commands: `index`, `step N.X`, `summary` (read-only) + `init`, `record`, `var`, `report` (state
machine). Code blocks tagged `runnable` (`bash` = true, plain \`\`\`\`\`\`\`\` = display-only). State file uses SHA-256
hash for drift detection. 58 unit tests.

---

## J. Shared LLM Client (`src/forge/core/llm/`)

`AnthropicClient` deferred; currently uses `OpenAIClient` for all providers via LiteLLM.

**Purpose:** Unified async-first LLM client abstraction for Proxy, Guard, and Skills components.

### J.1 Design principles

1. **Async-first**: All clients async; sync usage via `SyncAdapter` wrapper
2. **Canonical types**: `Message`, `CompletionResponse`, `StreamEvent` -- no raw dicts
3. **Injectable credentials**: `CredentialManager` with TTL caching, testable
4. **Separation**: LLM calls only; tier orchestration stays in Proxy

### J.2 Module structure

```text
src/forge/core/llm/
├── types.py        # Message, StreamEvent, ModelHyperparameters, ToolCall
├── protocols.py    # LLMClient protocol
├── credentials.py  # CredentialManager (injectable singleton)
├── errors.py       # NoApiKeyError, AuthenticationError, ProviderError
└── clients/        # LiteLLMClient
```

### J.3 Core types (signatures)

```python
class ModelHyperparameters(BaseModel):
    max_tokens: int; temperature: float | None; reasoning_effort: ReasoningEffort | None
    thinking: ThinkingConfig | None; strict: bool  # Error vs warn on unsupported params

class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict]; tool_calls: list[ToolCall] | None

class CompletionResponse(BaseModel): text: str; tool_calls: list[ToolCall] | None; usage: dict
class StreamEvent(BaseModel): type: Literal["text_delta", "tool_call_delta", "response_end", ...]
```

### J.4 Client protocol

```python
class LLMClient(Protocol):
    @property
    def model(self) -> str: ...
    async def complete(self, messages: list[Message], *, tools=None, hyperparams=None) -> CompletionResponse: ...
    async def stream(self, messages, *, tools=None, hyperparams=None) -> AsyncGenerator[StreamEvent, None]: ...
    async def count_tokens(self, messages, tools=None) -> int: ...
```

### J.5 Factory and provider detection

```python
def get_client(model: str, *, provider: ProviderType | None = None) -> LLMClient:
    """Sync factory, async methods. Provider auto-detected from model prefix."""
    # vertex_ai/, openai/, anthropic/ -> litellm_remote
    # gemini/ -> litellm_local
```

### J.6 Sync adapter

```python
class SyncAdapter:
    """Wraps async client for sync contexts. Uses asyncio.run() -- cannot nest in event loop."""
    def ask(self, prompt: str, *, system: str | None = None) -> str: ...
```

> **Trap:** Guard uses `SyncAdapter`; Proxy is async. Don't import sync Guard logic into Proxy -- `asyncio.run()`
> crashes in running loop. Use async-first at boundaries.

### J.7 Unsupported parameter policy

| Mode                     | Behavior                         |
| ------------------------ | -------------------------------- |
| `strict=False` (default) | Warn + ignore unsupported params |
| `strict=True`            | Raise `UnsupportedParamError`    |

### J.8 Relationship to Proxy

| Concern                        | Owner              |
| ------------------------------ | ------------------ |
| LLM API calls, auth, streaming | `core.llm`         |
| Tier mappings, templates       | `proxy.templates`  |
| Format conversion              | `proxy.converters` |

---

## K. WorkflowPolicy Cost Model

Migrated from the former archived Appendix C. Contextualizes why the tagger->checker->reviewer pipeline (design.md
§4.1.2) uses a branching architecture.

Cost model for a divergence-from-mean workflow: tagger ($0.001/call) filters 80% of changes as non-architectural. Of the
20% that reach a checker ($0.001), ~80% short-circuit as aligned. Only ~4% reach the reviewer ($0.05). Total: ~$0.32/100
changes vs $5.00 reviewing everything.
