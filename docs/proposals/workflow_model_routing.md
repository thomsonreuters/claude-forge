# Subprocess Routing Unification — Capability-Based Model Routing

**Status**: Implemented (2026-05-14).

**Context**: Every Forge subprocess (workflow workers, supervisor, handoff agent) resolves its proxy routing
differently. Workflow models hardcode a proxy name per model. The supervisor has its own proxy flag and auto-seeding
logic. The handoff agent has a three-step fallback chain. The subprocess proxy is a session-level ambient that only some
paths respect. Meanwhile, the auth system (`capabilities.py`) already models credentials as first-class capabilities
that unlock multiple templates — but the routing layer hasn't adopted this pattern.

---

## Problem

### 1. Inconsistent routing across subprocess types

Five subprocess types, five different resolution chains:

| Subprocess           | Routing source                                   | CLI flag                 | Fallback behavior          |
| -------------------- | ------------------------------------------------ | ------------------------ | -------------------------- |
| Main session         | `--proxy` sets `ANTHROPIC_BASE_URL`              | `--proxy`                | Direct Anthropic           |
| Workflow workers     | `spec.proxy` hardcoded in catalog                | `--models` (no override) | Fail if proxy missing      |
| Supervisor           | `config.proxy` or `auto_seed_supervisor_proxy()` | `--supervisor-proxy`     | Session proxy, then direct |
| Handoff agent        | `proxy_id` config, confirmed proxy, env          | Config YAML only         | Session proxy, then direct |
| Generic subprocesses | `FORGE_SUBPROCESS_PROXY` env var                 | `--subprocess-proxy`     | Direct Anthropic           |

The supervisor and handoff agent fail open (degrade to direct). Workflow workers fail closed (error if proxy missing).
Generic subprocesses fail closed with a guard. No two paths use the same resolution logic.

### 2. One credential, N proxies

A user with a single `OPENROUTER_API_KEY` must create separate proxies per model family (`openrouter-openai`,
`openrouter-gemini`, `openrouter-deepseek`, ...) even though they're all the same account hitting the same endpoint. The
auth system knows they're the same capability; the workflow engine doesn't.

### 3. Hidden model-proxy coupling

`--models gpt-5.5` reads as "use GPT-5.5" but actually means "use the `openrouter-openai` proxy with its default model."
The proxy is invisible in the CLI surface and only discoverable via `forge workflow list-models`.

### 4. No routing override on workflows

There's no way to say "use this model but route it through my proxy." Users with custom proxy setups (non-standard
ports, corporate gateways, single-proxy OpenRouter) are stuck with catalog defaults.

### 5. Naming inconsistency

Four different flag names for "which proxy should this subprocess use":

| Flag                 | Command                            | Scope                         |
| -------------------- | ---------------------------------- | ----------------------------- |
| `--proxy`            | `session start`                    | Main session                  |
| `--subprocess-proxy` | `session start`                    | All child processes (ambient) |
| `--supervisor-proxy` | `session start`, `guard supervise` | Supervisor only               |
| (none)               | `workflow panel`                   | No override possible          |

The workflow commands have no proxy flag at all — a gap that forces the per-family proxy workaround.

### Routing path matrix

Five ways to reach GPT-5.5 from a workflow, each with different proxy requirements:

| Setup                             | Proxies needed              | User experience                       |
| --------------------------------- | --------------------------- | ------------------------------------- |
| Per-family OpenRouter proxies     | `openrouter-openai` running | Current default, works but verbose    |
| Single OpenRouter proxy           | Any `openrouter-*` running  | Fails: catalog demands specific proxy |
| Custom corporate proxy            | Custom proxy running        | Fails: no way to override catalog     |
| Subprocess proxy (`--subprocess`) | Subprocess proxy running    | Works only if `spec.proxy` is None    |
| Direct (no proxy)                 | None                        | Only works for `claude-*` workers     |

## Design

### Core change: model identity vs routing resolution

Today `ModelSpec` bundles identity and routing:

```python
# Current: proxy hardcoded
ModelSpec(name="gpt-5.5", proxy="openrouter-openai", model_flag=None, ...)
```

Proposed: `ModelSpec` declares **what the model is** and the provider model refs Forge knows how to request. Concrete
routes are derived at runtime from that compact model spec plus template/auth metadata. The catalog no longer
hand-writes one route per credential/template combination.

```python
@dataclass(frozen=True)
class ModelRoute:
    """Derived routing option. Not hand-authored in the model catalog."""
    provider: str                           # "openrouter", "litellm", or "direct"
    credential: str                         # Credential from capabilities.py
    family: str                             # Model family requested ("openai", "gemini", "anthropic", etc.)
    template_id: str | None                 # Exact proxy template this route can use; None for direct
    template_family: str | None             # Template's explicit family metadata; None for direct
    model_ref: str                          # Provider-specific model ID / direct model pin


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model_id: str                           # Forge-canonical ID (e.g., "gpt-5.5", "gemini-3.1-pro-preview")
    family: str                             # Model family used for route/template matching
    provider_refs: tuple[tuple[str, str], ...] # Ordered provider preference: namespace -> model ref
    description: str
    preferred_proxy: str | None = None      # Catalog recommendation (soft, overridable)
    prompt: str | None = None
    prompt_mode: PromptMode = "override"
    worker_id: str | None = None
```

`family` is the model's native family. Cross-family routing is represented by derived routes from `provider_refs`, not
by making the model itself multi-family. `provider_refs` is ordered configuration: the first provider is the model's
default provider for display/setup, and later providers are fallback/alternate execution providers. `prompt`,
`prompt_mode`, and `worker_id` are unchanged workflow metadata. They affect worker prompt composition and result
labeling, not routing. Routing code must ignore them.

Key changes:

| Field              | Old                     | New                                                            |
| ------------------ | ----------------------- | -------------------------------------------------------------- |
| `proxy`            | Required, hardcoded     | Replaced by `preferred_proxy` (soft recommendation)            |
| `model_flag`       | Implicit proxy default  | Replaced by provider-specific entries in `provider_refs`       |
| `credential`       | Not present             | Derived on `ModelRoute` from template/auth metadata            |
| `direct`           | Boolean source of truth | Removed; directness is derived from selected route provider    |
| `direct_model`     | Separate env pin        | Removed; direct env pin is `ModelRoute.model_ref`              |
| `supported_routes` | Not present             | Derived at runtime, not stored on `ModelSpec`                  |
| `preferred_proxy`  | Not present             | Catalog hint, overridable by explicit workflow/session routing |

**`model_id` is Forge-canonical, not provider-shaped.** The catalog stores Forge's own model names (`gpt-5.5`,
`gemini-3.1-pro-preview`, `claude-opus-4-6`). Provider-specific IDs live in `provider_refs` and are copied onto derived
routes:

```python
ModelSpec(
    name="gpt-5.5",
    model_id="gpt-5.5",
    family="openai",
    provider_refs=(
        ("openrouter", "openai/gpt-5.5"),
        ("litellm", "openai/gpt-5.5"),
    ),
    preferred_proxy="openrouter-openai",
    description="Logical problems, systematic code review",
)
```

This avoids coupling `model_id` itself to OpenRouter's naming convention while still preserving the provider-specific
model ID needed at execution time. The route, not the credential alone, is the compatibility contract.

**Routes are derived, not hand-authored.** Route generation combines:

- `ModelSpec.family`
- `ModelSpec.provider_refs`
- template metadata (`template_id`, `preferred_provider`, explicit `family`)
- auth metadata (`credentials_for_template()` from `capabilities.py`)

For `gpt-5.5`, that produces routes such as `openrouter/openrouter`, `litellm/litellm-remote`, and `litellm/openai-api`
without writing three literals in every model spec. This mirrors the auth redesign: credentials derive proxy capability
from template metadata instead of carrying a manual "unlocks proxy" list.

**Template family metadata is a prerequisite.** Route derivation must not infer family from template names
(`openrouter-openai`) or tier model IDs (`openai/gpt-5.4-mini`). Add an explicit `family:` field to every proxy template
YAML under `src/forge/config/defaults/templates/`, validate it at load time, and fail fast if a proxy template lacks it.
`preferred_provider` already gives the provider half of the metadata; `family` completes the route derivation input.

Template family is used for ranking and warnings, not as the only compatibility gate. For model-ref passthrough
providers such as OpenRouter, route generation can emit cross-family routes (`gpt-5.5` through `openrouter-anthropic`)
and rank the native-family template first. Family-scoped providers should only emit routes whose template family matches
`ModelSpec.family` unless the provider explicitly declares passthrough behavior.

**Every route has a credential, including direct.** Direct Claude workers run `claude -p --bare`, which needs
`ANTHROPIC_API_KEY`. The auth system models this as the `anthropic-api` credential. A direct model declares
`provider_refs=(("direct", "claude-opus-4-6"),)`; route generation turns that into a `provider="direct"` route with
`credential="anthropic-api"`. There is no separate `direct` boolean or `direct_model` field.

### Unified resolution chain

All non-direct subprocess routing should follow the same chain. Today only `build_claude_env()` and the handoff agent
come close; the supervisor and workflow engine have their own logic.

**Proposed chain** (same for every subprocess type):

```
1. Explicit proxy       CLI flag override (--via, --supervisor-proxy, config proxy_id)
2. Subprocess proxy     Session ambient (FORGE_SUBPROCESS_PROXY) — user intent for child jobs
3. Preferred proxy      Catalog or config recommendation (soft — skip if not running)
4. Route scan           Find any running proxy compatible with one derived route
5. Session proxy        Inherited ANTHROPIC_BASE_URL
6. Direct fallback      For fail-open paths (supervisor, handoff): use Anthropic directly
   OR actionable error  For fail-closed paths (workflows): credential-aware message
```

**Why subprocess proxy (step 2) comes before preferred proxy and route scan:** `FORGE_SUBPROCESS_PROXY` is explicit
session-level user intent from `forge session start --subprocess-proxy`. `preferred_proxy` is a catalog hint: it
preserves today's per-family default when no explicit workflow/session routing exists, but it is not a user-specific
per-model override. If Forge later adds user-configurable per-model routing, that should become a new chain step above
subprocess proxy. With the current surfaces, explicit ambient routing wins over catalog defaults.

Steps 1-5 are the same for all subprocess types. Step 6 varies by subprocess criticality:

- **Workflow workers**: fail closed (the user asked for this work; partial results are worse than an error)
- **Supervisor**: fail open (blocking the coding session because a proxy is down is worse than skipping a check)
- **Handoff agent**: fail open (async/best-effort; benefits future sessions, not the current one)

**Structured return type.** The resolver returns a result object, not a bare `str | None`. Without structure, "direct by
user choice" and "unresolved, falling back to direct" are indistinguishable — callers can't log meaningful diagnostics
or distinguish intentional direct routing from failed resolution.

```python
@dataclass(frozen=True)
class RoutingResult:
    base_url: str | None           # None = direct Anthropic
    proxy_id: str | None           # Resolved proxy identity (for cost tracking, logging)
    template: str | None           # Proxy template (for tier override awareness)
    source: str                    # Which chain step resolved: "explicit", "subprocess_proxy",
                                   #   "preferred_proxy", "route_scan", "session_proxy", "direct"
    route: ModelRoute | None       # Selected route; None only for unresolved fallback
    credential: str | None         # route.credential, duplicated for auth/status ergonomics
    warning: str | None = None     # Non-fatal diagnostic (e.g., "preferred proxy not running, fell back")
```

```python
def resolve_subprocess_routing(
    explicit_proxy: str | None = None,
    preferred_proxy: str | None = None,
    routes: tuple[ModelRoute, ...] = (),
) -> RoutingResult:
    """Unified routing resolution for all Forge subprocesses.

    Walks the resolution chain and returns a structured route/proxy decision.
    Callers decide fail-open vs fail-closed based on source and their use case.
    """
```

This replaces:

- `lookup_proxy_base_url(spec.proxy)` in `engine._run_single()`
- `resolve_handoff_base_url()` in `handoff_agent.py`
- Inline proxy resolution in `supervisor.py:320`
- Subprocess proxy fallback in `build_claude_env()`

**Callers use `source` and `warning` for decisions:**

```python
routes = derive_model_routes(spec)
result = resolve_subprocess_routing(routes=routes, preferred_proxy=spec.preferred_proxy)

# Workflow workers: fail closed on unresolved
if result.route is None:
    return error(format_no_route_error(spec, routes))

# Supervisor: fail open, log the fallback
if result.warning:
    _log.warning("Supervisor routing: %s", result.warning)
```

Step 4 is the new capability. The bridge already exists — `credentials_for_template()` in `capabilities.py` maps
templates to credentials. Route scan uses routes produced by `derive_model_routes(spec)`.

```python
def derive_model_routes(spec: ModelSpec) -> tuple[ModelRoute, ...]:
    """Expand compact model metadata into concrete routing options.

    For each provider ref on the model, inspect known proxy templates and
    credential metadata to build route records. The route list is stable:
    preferred_proxy first if it matches a derived route, then provider_refs
    order, then native-family templates before cross-family passthrough
    templates, then deterministic alphabetical tiebreakers. This function
    does not inspect the proxy registry or running proxy state.
    """
```

```python
def resolve_proxy_for_route(
    routes: tuple[ModelRoute, ...],
    preferred_family: str | None = None,
) -> RoutingResult | None:
    """Find a running proxy that matches one of the model's derived routes.

    Scans the proxy registry, maps each proxy template to credential(s)
    via TEMPLATE_SECRETS -> credential_for_env_var, then matches against
    ModelRoute.template_id + ModelRoute.credential.

    Ranking (deterministic, not first-found):
    1. Healthy (pid alive) over starting/stale
    2. Route/provider preference order from derive_model_routes()
    3. Template family match over cross-family route (e.g., openrouter-openai
       preferred over openrouter-anthropic when model family is "openai")
    4. Stable tiebreaker: proxy_id alphabetical

    preferred_family is ModelSpec.family (e.g., "openai" for gpt-5.5,
    "gemini" for gemini-3.1-pro-preview). It ranks native-family
    templates ahead of cross-family passthrough routes without parsing
    template names.
    """
```

**Why deterministic ranking matters:** Without it, the scan returns whichever proxy the registry iterator yields first —
which may change across restarts or after proxy create/delete. A user with `openrouter-anthropic` and
`openrouter-openai` both running could see `gpt-5.5` route through either one nondeterministically. The ranking ensures
`openrouter-openai` wins (family match) and behavior is reproducible.

### Tier override caveat

Routing a model through a non-native proxy works but loses template-specific tier overrides. If `openai/gpt-5.5` is
routed through `openrouter-anthropic`, the proxy's tier overrides (reasoning_effort, temperature, verbosity) come from
the Anthropic template, not the OpenAI template. The model itself works (OpenRouter routes by model ID), but it won't
inherit OpenAI-specific hyperparameter defaults.

This is an acceptable tradeoff for the one-proxy convenience path. Users who want per-family tuning still create
per-family proxies — those resolve at step 3 (preferred proxy) before route scan kicks in. The `RoutingResult` includes
`template` so callers (or future enhancements) can detect and warn about cross-family routing:

```python
if result.route and result.route.template_family != spec.family:
    _log.info("Note: %s routed through %s — tier overrides may differ from %s defaults",
              spec.name, result.template, spec_family)
```

### Model override contract (blocking prerequisite)

When a worker routes through any proxy, the engine MUST pass the selected route's provider-specific model ref as
`--model`. Without `--model`, the proxy uses its default tier model. For example, `gpt-5.5 --via openrouter-anthropic`
would silently hit Claude (the Anthropic default), not GPT-5.5.

**Transmission mechanism:**

```
ModelSpec.model_id  →  selected ModelRoute.model_ref  →  --model flag on claude -p
    "gpt-5.5"            "openai/gpt-5.5"                 claude -p --model openai/gpt-5.5
```

The engine already passes `--model` at `engine.py:200-201` when `spec.model_flag` is set. The change: `model_flag` is
replaced by `model_id`, routing selects a `ModelRoute`, and the route's `model_ref` is **always** passed when routing
through a proxy:

```python
def resolve_model_flag(route: ModelRoute) -> str | None:
    """Return the --model flag for a routed workflow worker.

    Direct workers use Claude Code env pins instead of --model.
    Proxied workers always send an explicit model ref so --models means
    the same thing regardless of which compatible proxy was selected.
    """
    if route.provider == "direct":
        return None
    return route.model_ref
```

**Rules:**

- Proxied workers: `--model` is mandatory and uses `RoutingResult.route.model_ref`.
- Direct workers: `--model` is not used. `RoutingResult.route.model_ref` sets env vars (`ANTHROPIC_MODEL`, etc.)
  instead.

**Why this is blocking:** The entire one-proxy convenience path (`--via openrouter-anthropic` with cross-family models)
silently misroutes without this contract. The engine must use the selected route's `model_ref` before building the
subprocess command.

### Compatibility validation (strengthened)

Credential match is necessary for a route, but it is not sufficient for model compatibility. A proxy is compatible only
when it matches one of the model's derived routes. This allows valid alternatives such as `gpt-5.5` via `openrouter`,
`litellm-remote`, or `openai-api` while still rejecting unrelated proxies such as `litellm-gemini-local`. A
route-compatible proxy may still fail at runtime due to remote LiteLLM allowlists, corporate gateway policy, or
unavailable provider models.

**Two-tier check:**

1. **Static validation (preflight)**: The proxy's template ID must match a derived `ModelRoute.template_id`, and the
   template credential (via `credentials_for_template()`) must include that route's credential. This catches obvious
   mismatches (`litellm-gemini-local` with `gpt-5.5`) cheaply without forbidding valid non-default routes.

2. **Live advisory check (best-effort)**: When the proxy is reachable, query `GET /` for its tier/model mappings. If the
   selected route's `model_ref` isn't in the proxy's advertised capabilities, warn:

   ```
   Warning: Proxy 'openrouter-anthropic' does not advertise 'openai/gpt-5.5' in tier mappings.
   The model may still work (OpenRouter routes by model ID) but tier overrides won't apply.
   ```

   Live checks are advisory because today's proxy metadata is not an authoritative supported-model list. OpenRouter, in
   particular, routes by explicit model ID even when a template's tier map does not mention that model. If a future
   proxy exposes an authoritative deny/allow response, that can become a hard error. Never block on a health check
   during fan-out (`ThreadPoolExecutor` would bottleneck). Run live checks during preflight only.

**Fail behavior by check type:**

- Static route mismatch → hard error (the proxy fundamentally cannot serve this model)
- Live advisory miss → warning (the model may work but isn't advertised; tier overrides may not apply)

### Sidecar mode treatment

Sidecar containers deliberately do not mount `~/.forge` (design.md §7 — UID issues, undermines port isolation). This
means the global proxy registry (`~/.forge/proxies/index.json`) is unavailable inside sidecars. The first implementation
does not add a new sidecar-specific `--via` URL syntax; it only preserves the existing host-resolved session/subprocess
proxy paths.

| Chain step          | In host mode          | In sidecar mode                                                |
| ------------------- | --------------------- | -------------------------------------------------------------- |
| 1. Explicit proxy   | Works (registry scan) | Works only if host pre-resolved it into the invocation plan    |
| 2. Subprocess proxy | Works (registry scan) | Works if launcher injected pre-resolved subprocess metadata    |
| 3. Preferred proxy  | Works (registry scan) | **No-op** unless host pre-resolved it into the invocation plan |
| 4. Route scan       | Works (registry scan) | **No-op** (registry unavailable)                               |
| 5. Session proxy    | Works (env inherited) | Works (env inherited from host launch)                         |
| 6. Fallback         | Normal                | Normal                                                         |

**Normative rule:** proxy IDs are resolved on the host before entering the sidecar. Inside the sidecar, routing uses URL
plus metadata, never a registry lookup. The launcher or host-side workflow command injects pre-resolved values:

```text
FORGE_SUBPROCESS_BASE_URL=http://host.docker.internal:8095
FORGE_SUBPROCESS_PROXY_ID=openrouter-anthropic
FORGE_SUBPROCESS_TEMPLATE=openrouter-anthropic
```

If the user supplies a plain proxy ID inside a sidecar and no matching injected metadata exists, Forge fails with an
actionable error: "Proxy registry is unavailable inside sidecar; start the session with `--subprocess-proxy <proxy_id>`
or run the workflow command on the host so Forge can pre-resolve the proxy."

**Detection:** `resolve_subprocess_routing()` checks for sidecar mode via `intent.launch.mode == "sidecar"` or the
`FORGE_SIDECAR` env var (if set by the sidecar launcher). When detected, it skips registry-dependent steps and logs a
debug message.

**User impact:** Minimal. Sidecar sessions already have `ANTHROPIC_BASE_URL` set by the launcher (session proxy, step
5), so most workflows resolve at step 5. The gap only appears if a user tries to use `--models` with cross-family
workers inside a sidecar without a host-resolved subprocess proxy — in which case the error message should suggest
starting the session with `--subprocess-proxy`.

### Per-invocation routing plan

The engine resolves routing for all workers **once** at invocation start and passes a stable plan to each
`_run_single()` call. This prevents three problems:

1. **Registry drift**: If a proxy restarts or is cleaned during a parallel fan-out, different workers could resolve
   differently. A snapshot at invocation start ensures all workers use the same routing.

2. **Preflight/runtime divergence**: Preflight checks and runtime resolution currently use separate code paths. A single
   routing plan used by both ensures consistency.

3. **Repeated resolution cost**: Route scan involves registry reads, template matching, and PID checks. Doing this once
   per invocation (not once per worker) keeps fan-out fast.

```python
@dataclass(frozen=True)
class WorkerRoutingPlan:
    """Pre-resolved routing for all workers in a workflow invocation."""
    routes: tuple[RoutingResult, ...] # Same order and length as the resolved ModelSpec list
    resolved_at: str                  # ISO timestamp for staleness detection
    via_override: str | None          # --via value, if set (for logging)

def resolve_invocation_routing(
    specs: list[ModelSpec],
    via: str | None = None,
) -> WorkerRoutingPlan:
    """Resolve routing for all workers at invocation start.

    Called once by the workflow CLI command. The plan is passed to
    run_multi_review() which passes each worker's RoutingResult to
    _run_single(). No per-worker resolution at runtime.
    """
```

The plan is frozen (`frozen=True`) and treated as immutable after creation. Workers receive their `RoutingResult` by
input index, not by `worker_id`; duplicate workers and role variants are valid fan-out patterns and must not collapse
into a dict entry.

For workflows, `resolve_invocation_routing()` is fail-closed: it either returns a complete plan with one non-null route
per worker, or raises/returns a CLI routing error before any workers start. It must not synthesize `source="direct"` for
a non-direct model just to keep the fan-out moving. Supervisor and handoff callers can still use
`resolve_subprocess_routing()` in fail-open mode because their outputs are best-effort.

### Resolution chain ordering rationale (step 4 vs step 5)

The chain places route scan (step 4) before session proxy (step 5). This means a workflow could route through a
route-scanned proxy even though the current session already has `ANTHROPIC_BASE_URL`. This is intentional:

**"Model-route fit beats session inheritance."**

The session proxy was chosen for the *main session's* model family (e.g., `openrouter-anthropic` for Claude). Workflow
workers may need different families (GPT, Gemini). Inheriting the session proxy would route `gpt-5.5` through an
Anthropic-default proxy — the exact problem this proposal solves. Route scan finds a proxy that actually matches one of
the model's derived routes, which is a better fit.

Session proxy (step 5) is still useful as a last resort: if no preferred proxy exists and no route-compatible proxy is
running, the session's ambient proxy may still work (especially for OpenRouter, which serves all families). But it
should not preempt a route-matched proxy that's a better fit.

If this ordering surprises users, `RoutingResult.source` and the routing log line make the resolution path visible:

```
Routing gpt-5.5 through openrouter-openai [route_scan]
  (session proxy openrouter-anthropic skipped — route match preferred)
```

### CLI surface

**New `--via` flag** on workflow commands (panel, analyze, debate, consensus):

```bash
# Today: need per-family proxies
forge proxy create openrouter-openai
forge proxy create openrouter-gemini
forge workflow panel --models gpt-5.5,gemini-3.1-pro-preview

# Proposed: one proxy, explicit override
forge proxy create openrouter-anthropic
forge workflow panel --models gpt-5.5,gemini-3.1-pro-preview --via openrouter-anthropic

# Also works: route-based auto-resolution
forge workflow panel --models gpt-5.5,gemini-3.1-pro-preview
# (engine finds any running openrouter-* proxy automatically)
```

`--via` applies to all proxy-based workers in the invocation. Per-worker proxy override is not proposed (complexity
without clear use case). Direct workers (`claude-opus`, `claude-opus-4.7`) ignore `--via` — they bypass proxies
entirely.

**Flag naming consistency:**

| Subprocess       | Flag                 | Scope                     | Status       |
| ---------------- | -------------------- | ------------------------- | ------------ |
| Main session     | `--proxy`            | Main Claude process       | Exists       |
| All subprocesses | `--subprocess-proxy` | Ambient for child jobs    | Exists       |
| Workflow workers | `--via`              | This invocation's workers | **Proposed** |
| Supervisor       | `--supervisor-proxy` | Supervisor subprocess     | Exists       |
| Handoff agent    | Config YAML          | Handoff subprocess        | Exists       |

`--via` is deliberately named differently from `--proxy` to avoid confusion: `--proxy` routes the *session*, `--via`
routes the *workers* in a single workflow invocation. Using `--proxy` on workflow commands would suggest it changes the
session routing.

**Precedence when multiple flags interact:**

```bash
# --subprocess-proxy sets the ambient; --via overrides for this invocation
forge session start feat --subprocess-proxy openrouter-anthropic
forge workflow panel --models gpt-5.5 --via openrouter-openai
# Workers use openrouter-openai (--via wins: step 1 beats step 2)

# Without --via, workers use the subprocess proxy
forge workflow panel --models gpt-5.5
# Workers use openrouter-anthropic (step 2: FORGE_SUBPROCESS_PROXY)

# Without --via or subprocess proxy, falls through to preferred/route scan
forge workflow panel --models gpt-5.5
# Workers use openrouter-openai if running (step 3: preferred_proxy)
# Or any route-compatible proxy (step 4: route scan)
```

This matches the resolution chain: step 1 (explicit `--via`) > step 2 (subprocess proxy) > step 3 (preferred) > step 4
(route scan).

### Model catalog changes

```python
# Before
"gpt-5.5": ModelSpec(
    name="gpt-5.5",
    proxy="openrouter-openai",
    model_flag=None,
    description="Logical problems, systematic code review",
),

# After
"gpt-5.5": ModelSpec(
    name="gpt-5.5",
    model_id="gpt-5.5",                     # Forge-canonical, not provider-shaped
    family="openai",
    provider_refs=(
        ("openrouter", "openai/gpt-5.5"),
        ("litellm", "openai/gpt-5.5"),
    ),
    preferred_proxy="openrouter-openai",
    description="Logical problems, systematic code review",
),
```

The `model_id` is Forge-canonical. Provider-specific refs (`gpt-5.5` to `openai/gpt-5.5` for OpenRouter) live in
`provider_refs`; `derive_model_routes()` copies the selected ref to `ModelRoute.model_ref`.

Direct workers declare a direct provider ref — they run `claude -p --bare` which needs `ANTHROPIC_API_KEY`:

```python
"claude-opus": ModelSpec(
    name="claude-opus",
    model_id="claude-opus",
    family="anthropic",
    provider_refs=(
        ("direct", "claude-opus-4-6"),
    ),
    description="Deep architectural analysis, complex reasoning",
),
```

`derive_model_routes()` turns that into a `provider="direct"` route with `credential="anthropic-api"`. This aligns with
`capabilities.py` where `anthropic-api` unlocks "Forge subprocesses (claude -p --bare)". Preflight checks and error
messages now use the same credential vocabulary as `forge auth status`.

### Preflight check updates

`check_model_availability()` currently checks `lookup_proxy_base_url(spec.proxy)`. With decoupling, it walks the full
resolution chain (same order as runtime):

1. If `--via` is set: check that proxy exists, is running, and is compatible with the model (see compatibility below)
2. Check subprocess proxy (`FORGE_SUBPROCESS_PROXY`): same liveness + compatibility check
3. If `preferred_proxy` is set: check it (soft — warn if missing, don't fail)
4. Fall back to route scan: check if any proxy matching one of the derived model routes is running
5. Check session proxy (inherited `ANTHROPIC_BASE_URL`)
6. Report: "model X needs a compatible route — run `forge auth login -c Y` and `forge proxy create <template>`"

Error messages use `format_missing_credential_error()` from `capabilities.py` — same actionable format as auth errors.

**Compatibility validation.** Explicit, subprocess, and session proxies (steps 1, 2, 5) are user-chosen — they are not
selected by route scan. The engine must validate that the chosen proxy can actually serve the requested model. If
`--via litellm-gemini-local` is used with `gpt-5.5`, fail with a clear route/model compatibility error instead of
blindly honoring the proxy:

```
Error: Proxy 'litellm-gemini-local' cannot serve model 'gpt-5.5'.
  Proxy route: litellm/gemini-api
  Supported routes: openrouter/openrouter, litellm/litellm-remote, litellm/openai-api
  Tip: Use '--via openrouter-openai' or 'forge proxy create openrouter-openai'.
```

Compatibility check: the proxy's template and credential (via `credentials_for_template()`) must match one of the
model's derived routes. Route scan (step 4) is inherently compatible by construction — it only returns proxies that
match a route.

### `list-models` display changes

Group by primary route credential instead of listing flat. The primary credential is derived from static configuration,
not from runtime proxy availability:

1. Derive routes from `ModelSpec` + template/auth metadata without reading the proxy registry.
2. Pick the first statically preferred route (`preferred_proxy` if it matches, otherwise `provider_refs` order).
3. Use that route's credential for grouping.

This keeps `forge workflow list-models` stable: `gpt-5.5` does not move between `openrouter` and `litellm-remote` just
because different proxies happen to be running.

```
Available Models (grouped by primary credential)

  openrouter (OPENROUTER_API_KEY)  [configured]
    gpt-5.5                 Logical problems, systematic code review         ready
    gemini-3.1-pro-preview  Balanced analysis, large context                 ready
    deepseek-r3             Cost-efficient reasoning                         ready
    ...

  anthropic-api (ANTHROPIC_API_KEY)  [configured]
    claude-opus             Deep architectural analysis, complex reasoning   ready
    claude-opus-4.7         Bounded single-shot review                       ready

  openai-api (OPENAI_API_KEY)  [not configured]
    (no models currently use this as their primary credential)
```

This mirrors the auth status display: capabilities first, then details.

## Scope and constraints

### What changes

| Component                      | Change                                                                                        |
| ------------------------------ | --------------------------------------------------------------------------------------------- |
| `ModelSpec`                    | Add `model_id`, `family`, `provider_refs`, `preferred_proxy`; remove direct/proxy duplication |
| `ModelRoute`                   | New derived provider/credential/template/model-ref compatibility record                       |
| Proxy template YAML            | Add explicit `family` metadata to every template; validate at load time                       |
| `derive_model_routes()`        | New: generate route records from model specs plus template/auth metadata                      |
| `_build_available_models`      | Populate compact model specs from catalog                                                     |
| `engine._run_single`           | Receive `RoutingResult` from plan, not resolve per-worker                                     |
| `engine.run_multi_review`      | Accept `WorkerRoutingPlan`, pass per-worker `RoutingResult`                                   |
| `preflight_check`              | Follow resolution chain, static validation + live advisory                                    |
| `list-models`                  | Group by primary route credential                                                             |
| Workflow CLI                   | Add `--via` to panel, analyze, debate, consensus                                              |
| `resolve_model_specs`          | Pass `--via` context to returned specs or engine                                              |
| `resolve_subprocess_routing()` | New shared function replacing 4 ad-hoc resolution paths                                       |
| `resolve_invocation_routing()` | New: batch-resolve all workers at invocation start (frozen plan)                              |
| `resolve_model_flag()`         | New: map selected `ModelRoute.model_ref` to proxied `--model`                                 |
| `resolve_handoff_base_url()`   | Migrate to `resolve_subprocess_routing()` (same behavior, shared code)                        |
| Supervisor proxy resolution    | Migrate to `resolve_subprocess_routing()` (same behavior, shared code)                        |
| `build_claude_env()`           | Subprocess proxy fallback delegates to shared function                                        |

### What doesn't change

- **Direct workers**: `claude-opus`, `claude-opus-4.7` — unchanged behavior; selected direct route bypasses proxies
- **Proxy template behavior**: Per-family templates still exist for users who want them; only their metadata shape
  changes by adding explicit `family`
- **Auth system**: No changes to `capabilities.py` or `template_secrets.py` — they already have the right abstractions
- **Proxy config**: No changes to proxy creation, overlay, or runtime truth
- **Existing proxy setups**: Per-family proxies still work via `preferred_proxy` (step 3 in the chain)
- **Supervisor model selection**: `auto_seed_supervisor_proxy()` can keep its model-selection policy; only the final
  proxy lookup moves to `resolve_subprocess_routing()`
- **Session start flags**: `--proxy`, `--subprocess-proxy`, `--supervisor-proxy` unchanged
- **Fail-open/closed behavior**: Each subprocess type keeps its existing failure mode

### Migration

- `ModelSpec.proxy` becomes `preferred_proxy` (same value, soft instead of hard)
- `ModelSpec.model_flag` becomes entries in `provider_refs` plus derived route-level `model_ref`
- `ModelSpec.direct` and `ModelSpec.direct_model` are removed; direct execution is represented only by a selected
  `provider="direct"` route
- Proxy template YAML gains a required `family` field before route derivation lands
- Per the README's research-preview policy and coding-standards §5 ("Pre-release (`0.x`) versions may break formats
  without a deprecation period"), `list-models --json` is updated atomically alongside the Python types. No deprecation
  aliases. A changelog entry covers the renames.
- Existing proxy setups work via `preferred_proxy` resolution
- `--models` syntax unchanged
- Supervisor and handoff agent behavior unchanged (just shared implementation)

### Simplified first-time setup

Today:

```bash
forge auth login -c openrouter        # One key
forge proxy create openrouter-openai  # But then N proxies...
forge proxy create openrouter-gemini
forge proxy create openrouter-deepseek
forge workflow panel                  # Finally works
```

After:

```bash
forge auth login -c openrouter           # One key
forge proxy create openrouter-anthropic  # One proxy
forge workflow panel                     # Works (route-based resolution)
```

Or with `--subprocess-proxy` for the full dual-auth setup:

```bash
forge auth login -c openrouter
forge proxy create openrouter-anthropic
forge session start feat --subprocess-proxy openrouter-anthropic
# Main session: free subscription
# Panels, supervisor, handoff: all route through the one proxy
# Step 2 (subprocess proxy) resolves openrouter-anthropic for all child jobs
```

### Design decisions (resolved)

01. **`model_id` is Forge-canonical.** `ModelSpec.model_id` uses Forge's own catalog names (`gpt-5.5`, not
    `openai/gpt-5.5`). Provider-specific IDs live in `ModelSpec.provider_refs` and are copied to `ModelRoute.model_ref`
    during route derivation. This keeps the model identity provider-neutral while making execution refs explicit.

02. **Routes are derived, not hand-authored.** The catalog declares compact model facts (`family`, `provider_refs`,
    `preferred_proxy`). `derive_model_routes()` combines those facts with template/auth metadata. This avoids a large
    repeated matrix of route literals and matches the auth redesign's derivation pattern.

03. **Provider preference is static configuration.** `provider_refs` is ordered and `preferred_proxy` is a static
    catalog hint. They determine primary credential display and route ranking before runtime availability is considered.
    `list-models` must not group models based on currently running proxies.

04. **Template family is explicit metadata.** Every proxy template YAML gets a required `family` field. Route derivation
    uses that field for native-family ranking and tier-override warnings. It must not infer family from template names
    or tier model IDs.

05. **Subprocess proxy before preferred proxy and route scan.** `FORGE_SUBPROCESS_PROXY` is explicit session-level user
    intent. `preferred_proxy` is a catalog default, not a user-specific per-model preference. If Forge later adds
    user-configured per-model routing, it should be a separate higher-precedence step.

06. **Structured return type.** `RoutingResult` carries `base_url`, `proxy_id`, `template`, `source`, selected `route`,
    `credential`, and `warning`. This lets callers distinguish "direct by choice" from "unresolved fallback" and log
    meaningful diagnostics. A bare `str | None` conflates these cases.

07. **Direct workers are just direct routes.** They declare `provider_refs=(("direct", "<claude-model>"),)`. Route
    derivation creates a `provider="direct"` route with `credential="anthropic-api"`. There is no separate `direct`
    boolean or `direct_model` field.

08. **Tier override caveat is an accepted tradeoff.** Cross-family routing (e.g., `gpt-5.5` through
    `openrouter-anthropic`) works but loses template-specific tier overrides. Users who want per-family tuning create
    per-family proxies. `RoutingResult.template` enables future cross-family warnings.

09. **Model override via `--model` is mandatory for proxied workflow workers.** Without passing the selected route's
    `model_ref`, the proxy defaults to its template's model. The engine passes `RoutingResult.route.model_ref` as
    `--model` on the `claude -p` command.

10. **Static validation, live advisory.** Static route checks catch obvious mismatches and fail workflows before
    fan-out. Live `GET /` introspection is advisory because current proxy metadata is not an authoritative
    supported-model list.

11. **Per-invocation routing plan.** All workers' routing is resolved once at invocation start as a frozen
    `WorkerRoutingPlan`. No per-worker resolution at runtime. Prevents registry drift and preflight/runtime divergence.
    Workflow plan resolution is fail-closed: no partial fan-out when any worker lacks a route.

12. **Model-route fit beats session inheritance.** Route scan (step 4) comes before session proxy (step 5) because the
    session proxy was chosen for the main session's family, not for cross-family workflow workers.

13. **Sidecar mode: host-resolved explicit routing only.** Registry-dependent steps are unavailable inside sidecars
    because `~/.forge` is not mounted. This proposal does not define a new sidecar URL+metadata CLI syntax; it relies on
    host-resolved session/subprocess proxy metadata or inherited session proxy.

### Open questions

1. **`--via` scope**: Per-invocation (all workers share one proxy) vs per-worker (`--models gpt-5.5:my-proxy`). The
   proposal starts with per-invocation. Per-worker can be added later if needed, but the syntax gets noisy.

2. **Route-based resolution fallback vs fail**: Should the engine auto-resolve to any compatible running proxy
   (convenient but surprising), or require explicit `--via` when `preferred_proxy` isn't running (predictable but
   verbose)? Proposal: auto-resolve with a visible log line ("Routing gpt-5.5 through openrouter-anthropic \[route
   match\]").

3. **Handoff agent proxy config**: Currently configured via YAML (`memory.auto_update.proxy`). Should it gain a CLI flag
   too (e.g., `--handoff-proxy` on `session start`)? Probably not — the handoff agent is async/best-effort and doesn't
   benefit from per-invocation control. Keep YAML config, just use shared resolution internally.

4. **`--via` on `forge guard supervise`**: The `guard supervise` command already has `--supervisor-proxy`. Should it
   also accept `--via` for consistency? Probably not — `--supervisor-proxy` is more descriptive for its context. The
   naming difference is intentional: `--via` routes anonymous workers, `--supervisor-proxy` routes a named role.
