# Runtime Abstraction

Status: proposal, aligned with PR #8

## Summary

Forge should make the agent runtime an explicit abstraction. Claude Code stays supported, and remains the first shipping
frontend, but Forge should be able to run Codex as a first-class runtime for headless work and, later, interactive
sessions.

PR #8 is an enabling slice for this proposal, not the runtime abstraction itself. It strengthens the model gateway,
credential, routing, and cost-accounting foundations that an agent runtime layer will need. The runtime registry,
headless invoker interface, Codex/Gemini native invokers, normalized hooks, and durable cross-runtime usage ledger
remain future work.

The design separates concepts that are currently too easy to blur:

```text
Forge orchestration
  -> frontend runtime: Claude Code | Codex
  -> headless invoker: claude -p | codex exec | gemini -p
  -> hook adapter: Claude hooks | Codex hooks
  -> model gateway/provider: Forge proxy | LiteLLM | direct API
```

This lets Forge keep Claude Code as the user-facing shell while extracting the parts that are not inherently Claude:
worker invocation, hook policy, usage attribution, and runtime capability checks.

## PR #8 Alignment

The current implementation already separates model gateways and subprocess routing from Claude Code session UX:

- Native OpenRouter support makes "gateway" a first-class Forge proxy concern rather than a side effect of LiteLLM.
- The credential capability registry separates provider auth from runtime auth and avoids treating Claude Code login as
  a generic backend credential.
- The model catalog now carries provider refs, families, capabilities, pricing, and prompt addendums used by routing and
  cost decisions.
- `derive_model_routes()`, `resolve_subprocess_routing()`, and `WorkerRoutingPlan` move workflow routing toward a
  capability-based contract that can later feed non-Claude invokers (see
  [design.md §3.6.12](../design.md#3612-subprocess-routing-resolution-normative) and
  [design_appendix.md §L](../design_appendix.md#l-subprocess-routing-reference) for the current contract).
- `--subprocess-proxy` lets a direct Claude Code frontend route headless child work through an API-backed proxy, which
  is an important transitional bridge.
- Proxy request logs, verb-level cost attribution, `forge proxy costs`, and per-proxy spend caps provide an initial
  cost-control layer before a full runtime usage ledger exists.

What it does not yet do:

- It does not introduce a runtime registry.
- It does not abstract `claude -p` behind a runtime-neutral `HeadlessInvoker`.
- It does not add native `codex exec` or `gemini -p` workers.
- It does not normalize Claude/Codex hooks into a shared policy event model.
- It does not unify proxy logs and runtime-native usage events into a single durable ledger.

So the proposal should remain in the PR as future architecture, but framed as the next layer above this PR's
multi-provider routing and cost-control work.

## Problem

Forge's current design assumes Claude Code in several places:

- Session launch and extension installation are shaped around `.claude/`.
- Workflow workers are described as `claude -p` subprocesses.
- Hook policy is coupled to Claude hook payloads.
- Proxy setup and auth docs can imply that headless workers require `ANTHROPIC_API_KEY`.
- Usage visibility is split across proxy logs, runtime output, and provider dashboards.

That worked for the first architecture, but it now blocks three goals:

- Run Codex as a native agent runtime, not merely as a model behind Claude Code.
- Use each runtime's own headless mode, prompts, tool semantics, auth, and usage output.
- Give users clear cost visibility without pretending that subscriptions, API keys, and gateways are the same thing.

## Design Thesis

Runtime is not provider. Provider is not auth. Auth is not gateway.

Forge should preserve these boundaries:

| Concept          | Meaning                                      | Examples                                                  |
| ---------------- | -------------------------------------------- | --------------------------------------------------------- |
| Frontend runtime | Interactive agent shell used by a human      | Claude Code today, Codex later                            |
| Headless invoker | Programmatic worker used by workflows        | `claude -p`, `codex exec`, `gemini -p`                    |
| Hook adapter     | Runtime-specific event translator            | Claude hook commands, Codex hook commands                 |
| Model gateway    | API-compatible routing and accounting layer  | Forge proxy, LiteLLM, OpenRouter, direct provider SDK/API |
| Provider auth    | Credential accepted by the upstream provider | API key, Vertex ADC, saved official CLI auth              |

The immediate product value is not "replace Claude Code." It is to stop hard-coding Claude Code assumptions so Forge can
choose the best runtime for each job.

## Source-Grounded Posture

This section is an engineering policy, not legal advice. It records which official docs support the routes Forge intends
to use and where Forge will deliberately avoid ambiguous auth behavior.

### OpenAI Codex

OpenAI documents Codex CLI authentication through both ChatGPT sign-in and API key sign-in. The same docs state that the
CLI and IDE extension support both methods, and that API key usage is billed through the OpenAI Platform while ChatGPT
sign-in follows ChatGPT workspace controls and credits. See
[Codex authentication](https://developers.openai.com/codex/auth).

OpenAI also documents `codex exec` as non-interactive mode for scripts and CI, including JSONL output with usage events.
See [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive).

Codex hooks are stable as of Codex CLI 0.124.0 and still require explicit activation with
`[features] codex_hooks = true`. Lifecycle events include `PreToolUse`, `PermissionRequest`, `PostToolUse`,
`UserPromptSubmit`, and `Stop`. See [Codex hooks](https://developers.openai.com/codex/hooks) and the
[Codex changelog](https://developers.openai.com/codex/changelog).

Forge posture:

- Native Codex headless work should use `codex exec`.
- Codex frontend support should use the official Codex CLI, not a Claude compatibility shim.
- Usage ingestion should prefer Codex JSONL usage events when available.
- API-key routes and ChatGPT-subscription routes must be represented separately in Forge usage/cost output.
- ChatGPT-subscription-backed Codex through LiteLLM should remain a candidate compatibility route only after version
  verification and an explicit product decision about how subscription quota appears in Forge usage output. Native
  `codex exec` remains the preferred route for Codex-as-runtime work.

### Anthropic Claude Code

Anthropic's Claude Agent SDK docs describe API-key and cloud-provider authentication for programmable agents, including
Anthropic API keys, Bedrock, Vertex AI, and Azure. They also state that, unless previously approved, third-party
developers should not offer `claude.ai` login or rate limits for their products. See
[Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk).

Claude Code also documents LLM gateway configuration through environment variables such as `ANTHROPIC_BASE_URL`,
`ANTHROPIC_BEDROCK_BASE_URL`, and `ANTHROPIC_VERTEX_BASE_URL`, including LiteLLM examples and gateway benefits like
usage tracking. See [Claude Code LLM gateway configuration](https://code.claude.com/docs/en/llm-gateway) and
[Claude Code enterprise deployment overview](https://code.claude.com/docs/en/third-party-integrations).

Forge posture:

- Forge must not offer `claude.ai` consumer login or `claude.ai` rate limits as a Forge-provided backend.
- Direct Claude programmable workers require a legitimate Anthropic/API/cloud-provider/gateway credential path.
- Claude Code can remain the user's local frontend runtime, but Forge should not treat that consumer login as a generic
  provider credential.
- `claude -p` workers may use a configured gateway/proxy route, but that route must be described by the gateway/provider
  credentials it actually uses.
- If a user has only Claude consumer login and no API-backed provider or configured gateway, Forge should allow
  interactive Claude Code session features that can legitimately use that login, but block headless/API-backed workflows
  with setup guidance.

### Google Gemini

Gemini CLI documents headless mode for command-line scripts and automation, including `gemini -p` and JSON output with
statistics. See [Gemini CLI headless mode](https://google-gemini.github.io/gemini-cli/docs/cli/headless.html).

Google's Gemini API docs require an API key for Gemini API usage and document `GEMINI_API_KEY` / `GOOGLE_API_KEY`
environment variables. See [Using Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key). Vertex AI documents
Google Cloud authentication for Gemini on Vertex AI through API keys or Application Default Credentials. See
[Configure application default credentials](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/gcp-auth).

Forge posture:

- Gemini support has two lanes: API-backed provider routing, or native Gemini CLI headless invocation.
- Forge will not support Gemini consumer-account routing through LiteLLM.
- A Claude Code `claude -p` worker can call an API-backed Gemini proxy route, but that is provider/API routing, not
  Gemini consumer subscription routing.

### OpenRouter

OpenRouter is a hosted multi-provider gateway with an OpenAI-compatible API. A single API key provides access to models
from OpenAI, Anthropic, Google, Meta, and others. OpenRouter documents per-token pricing, model metadata, key-level
credit limits, and current-key usage. See [OpenRouter docs](https://openrouter.ai/docs).

Forge posture:

- OpenRouter is a supported model gateway alongside LiteLLM and direct provider APIs.
- Use OpenRouter as the easiest solo-developer API path for multi-provider access: create an OpenRouter API key, add a
  small credit balance, set a key-level credit limit, store `OPENROUTER_API_KEY`, then smoke-test `/api/v1/key` before
  routing expensive workflows through it.
- Provider/model discovery must use `GET /api/v1/models/user`, which OpenRouter filters by the user's provider
  preferences, privacy settings, and guardrails. This is the effective API model-ID view for this key when ZDR-only
  routing or other privacy controls are enabled. Do not use the public catalog endpoint for setup, smoke tests, model
  pickers, or "available model" output because it can include models blocked by account policy. Also do not assume this
  count will match OpenRouter website buckets such as "available" and "unavailable"; `/models/user` exposes only the
  effective user-filtered model list.
- OpenRouter routes are API-priced (per-token billing); represent them as API spend in usage/cost output.
- Forge proxy templates for OpenRouter should use Forge's native OpenRouter provider/client path. The client calls
  OpenRouter's OpenAI-compatible API directly, keeps template/accounting behavior inside Forge, and translates
  OpenRouter-specific parameters such as `reasoning` and `verbosity`.
- LiteLLM OpenRouter support can remain a compatibility path for existing deployments, but it is not the default Forge
  implementation path for OpenRouter templates.
- `OPENROUTER_API_KEY` is the credential; do not conflate it with provider-specific API keys.
- Optional app attribution headers should be supported later: OpenRouter uses `HTTP-Referer` and `X-OpenRouter-Title`.
  Users on the LiteLLM compatibility path set `OR_SITE_URL` and `OR_APP_NAME`.

### LiteLLM and Gateways

LiteLLM is a third-party gateway. Its docs describe provider translation, proxy server usage, cost tracking, budgets,
and Vertex/OpenAI/Anthropic examples. See [LiteLLM docs](https://docs.litellm.ai/).

Forge posture:

- Use LiteLLM for remote/shared gateways and local API-backed provider routes where it is the best compatibility layer.
- Treat LiteLLM's provider support as implementation detail that must be version-verified.
- Do not use LiteLLM as a way to launder unsupported consumer auth into an API route.
- Do not route OpenRouter through LiteLLM by default now that Forge has a native OpenRouter provider path.
- Consider ChatGPT-subscription-backed Codex through LiteLLM only after version verification and an explicit product
  decision about how subscription quota should appear in Forge usage output.
- Native `codex exec` remains the cleaner path for Codex-as-runtime work where Claude Code compatibility is not needed.

## Target Architecture

### Runtime Registry

Forge should introduce a runtime registry that answers:

- Is this runtime installed?
- Can it launch interactively?
- Can it run headless?
- Which hooks are supported?
- Can it emit usage statistics?
- Does it support native resume?
- Which install/config scopes does it use?

The registry should expose capability data instead of forcing all runtimes into the Claude Code shape.

### Headless Invocation

Existing `claude -p` calls should move behind a `HeadlessInvoker` interface:

```text
HeadlessRequest
  prompt
  cwd
  runtime
  model_or_tier
  proxy_id
  resume_id
  allowed_tools
  timeout
  attribution

HeadlessResult
  status
  output
  transcript_or_event_path
  runtime_session_id
  usage
  artifacts
  error
```

Initial implementations:

- `ClaudeHeadlessInvoker`: wraps `forge.core.reactive.session_runner.run_claude_session()` (today's shared subprocess
  runner used by supervisor, team supervisor, and handoff agent) plus the parallel fan-out behavior in
  `forge.review.engine`.
- `CodexHeadlessInvoker`: uses `codex exec`, `--json`, and `codex exec resume` where useful.
- `GeminiHeadlessInvoker`: uses `gemini -p --output-format json`.

The compatibility shim should keep current workflow callers working while the internals migrate.

The review engine is the migration wrinkle. Today the shared base subprocess runner lives in
`src/forge/core/reactive/session_runner.py`, while `src/forge/review/engine.py` still owns `run_multi_review()` and its
direct `Popen` lifecycle management for parallel `claude -p` fan-out. The invoker layer should absorb that
responsibility rather than leave review on a special path. `HeadlessInvoker` should therefore support timeouts,
process-group cleanup, cancellation, and parallel fan-out.

### Hooks and Policy

Forge policy should operate on normalized action context, not on Claude-specific JSON:

```text
ActionContext
  runtime
  event
  tool_name
  tool_input
  cwd
  session
  command
  risk_facts

PolicyDecision
  allow | warn | deny | needs_review
  reason
  runtime_output
```

Claude and Codex hook handlers become translators:

```text
Claude hook payload -> ActionContext -> policy core -> Claude hook response
Codex hook payload  -> ActionContext -> policy core -> Codex hook response
```

Codex `PreToolUse` is valuable enough for a beta, but it is not a full enforcement boundary. The hook docs note that it
does not intercept every tool path. Forge should expose that as a capability limitation rather than imply parity.

### Usage and Cost Visibility

PR #8 ships a narrower cost layer first:

- Proxy request logs: `~/.forge/costs/requests/<month>_<pid>.jsonl`
- Verb-level estimated cost logs: `~/.forge/costs/verbs/<month>_<pid>.jsonl`
- CLI summaries: `forge proxy costs [proxy_id]`
- Per-proxy API spend caps in `proxy.costs`

That layer is useful now, but it is not the final runtime usage system. It sees proxy-backed API calls well; it can only
estimate command attribution by snapshotting proxy metrics around a verb; and it does not represent native runtime usage
from future `codex exec` or `gemini -p` runs.

The next layer should create a durable usage ledger:

```text
~/.forge/usage/events.jsonl
```

Each event should include:

- `run_id`, `parent_run_id`
- `session`, `workflow`, `command`
- `runtime`, `provider`, `model`, `proxy_id`
- token counts and cached-token counts when available
- latency, status, and failure type
- `billing_mode`: `api`, `subscription_quota`, or `unknown`
- dollar cost only when the route is API-priced and pricing is known
- quota/token units for subscription-backed routes, without estimating dollar cost

Attribution environment variables should be injected into Forge-spawned processes:

```text
FORGE_RUN_ID
FORGE_PARENT_RUN_ID
FORGE_RUNTIME
FORGE_COMMAND
FORGE_SESSION
FORGE_WORKFLOW
FORGE_PROXY_ID
```

This gives users the answer they actually want: which command or workflow caused which provider/model call, and what it
cost or consumed.

The first instrumentation points are small enough to ship before the runtime abstraction:

| Callsite            | File                                     | Purpose                        | PR #8 status                                     |
| ------------------- | ---------------------------------------- | ------------------------------ | ------------------------------------------------ |
| Workflow verbs      | `src/forge/cli/workflow.py`              | Panel/analyze/debate/consensus | Initial verb-cost snapshots shipped              |
| Handoff agent       | `src/forge/session/handoff_agent.py`     | Post-session doc updates       | Initial verb-cost snapshots shipped              |
| Semantic supervisor | `src/forge/guard/semantic/supervisor.py` | Plan alignment checks          | Initial verb-cost snapshots shipped              |
| Team supervisor     | `src/forge/guard/team/handlers.py`       | Work divergence checks         | Still future                                     |
| Review engine       | `src/forge/review/engine.py`             | Multi-model fan-out            | Routing plan shipped; invoker abstraction future |
| Claude launcher     | `src/forge/cli/claude.py`                | Bare Claude Code launch        | Still future                                     |
| Session launcher    | `src/forge/cli/session.py`               | Managed Forge session launch   | Subprocess proxy env shipped; ledger future      |

### Cost Caps

Visibility is not enough. Forge skills and supervisors can fan out to expensive providers, so the usage ledger needs a
budget gate that can block before a provider call happens.

PR #8 implements the first budget gate at the proxy layer:

```yaml
# ~/.forge/proxies/<proxy_id>/proxy.yaml
proxy:
  costs:
    caps:
      per_day: 20.00
      per_month: 200.00
    cap_mode: strict   # post | strict
    on_cap_hit: reject # reject | warn
```

That is the right first boundary because the proxy is where API-priced traffic can be measured and rejected. Future
runtime-level budgets should build on it instead of introducing a competing config surface too early.

The future usage ledger can then add scoped budgets, for example global/workflow/provider/runtime caps. Hard caps should
be checked before proxy requests and before headless subprocess launch. A block response should include the cap scope,
current spend or quota count, attempted route, and originating command/workflow. Concurrent fan-out needs a lock or
reservation strategy so multiple workers cannot race past a hard cap.

Subscription-backed routes use quota/token caps, not fake dollar estimates. API-backed routes use API dollar caps when
pricing is known.

### Compliance and Auth Preflight

Forge should preflight provider auth and runtime capability before launching expensive or headless work. If the selected
route would rely on unsupported consumer-auth behavior, Forge should fail with a helpful message that names supported
alternatives:

- configure a Forge proxy backed by OpenAI, Gemini API, Vertex AI, Anthropic API, OpenRouter, or a supported LiteLLM
  route
- choose native `codex exec` for Codex headless work
- choose native `gemini -p` for Gemini headless work
- disable the workflow model that requires unavailable auth

This is a user-experience rule and a compliance posture: fail at the Forge boundary with a clear setup path.

## Runtime Capability Matrix

Initial target matrix:

| Capability               | Claude Code                      | Codex CLI                                       | Gemini CLI                    |
| ------------------------ | -------------------------------- | ----------------------------------------------- | ----------------------------- |
| Interactive frontend     | Current default                  | Target beta                                     | Not planned initially         |
| Headless worker          | `claude -p`                      | `codex exec`                                    | `gemini -p`                   |
| Native hooks             | Existing Forge integration       | Stable in 0.124.0+; enable `codex_hooks`        | No comparable hook target yet |
| Pre-tool policy          | Current Claude hooks             | `PreToolUse` adapter                            | Not initially                 |
| Usage source             | Transcript/status/proxy fallback | JSONL usage events                              | JSON stats                    |
| Resume                   | Claude session resume            | `codex exec resume`                             | Capability-check first        |
| Gateway route            | Anthropic-compatible base URLs   | Native CLI or first-class ChatGPT LiteLLM route | API/Vertex route only         |
| Consumer auth as gateway | Not supported by Forge           | Supported only through documented Codex routes  | Not supported                 |

## Shipping Strategy

The first shipping slice is PR #8: keep Claude Code as the frontend and ship native OpenRouter, capability-based
subprocess routing, API-backed workflow model selection, and proxy-level cost visibility/caps.

After PR #8, the runtime abstraction path should be:

1. Normalize the current Claude subprocess runner into `ClaudeHeadlessInvoker` without changing user-facing behavior.
2. Move review-engine fan-out behind the invoker contract, including process-group cleanup, timeout handling, and
   cancellation.
3. Promote proxy request logs and verb snapshots into a durable usage ledger that can also ingest runtime-native usage
   events.
4. Add `CodexHeadlessInvoker` using `codex exec` and JSONL usage events.
5. Add runtime capability checks and auth preflight for native Codex execution.
6. Normalize hook payloads into `ActionContext` / `PolicyDecision`.
7. Evaluate a Codex frontend runtime beta once headless invocation, usage accounting, and policy semantics are clear.

## Non-Goals

- Do not make every runtime emulate every Claude Code feature.
- Do not support Gemini consumer-account routing through LiteLLM.
- Do not offer Anthropic `claude.ai` login or rate limits as a Forge backend.
- Do not represent subscription usage as exact API dollar spend.
- Do not let expensive skills bypass hard caps once configured.
- Do not make Forge proxy the only path; native runtime execution should be preferred where it is cleaner.

## Open Questions

- What is the minimum Codex hook coverage required for frontend beta?
- How should `%` direct commands map onto Codex `UserPromptSubmit`?
- How should Forge represent runtimes that can run headless but cannot enforce pre-tool policy?
- How should run-tree attribution (`FORGE_RUN_ID`, `FORGE_PARENT_RUN_ID`) compose with the existing `FORGE_DEPTH`
  recursion guard and `FORGE_SESSION` identifier? Should `FORGE_DEPTH` become a derived property of the run tree?
