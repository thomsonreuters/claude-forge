# Forge Design (Unified Architecture)

- **Session manager usage**: [session-management.md](end-user/sessions.md) (session management guide)
- **Handoff agent usage**: [handoff.md](end-user/handoff.md) (automatic memory docs guide)
- **Search usage**: [search.md](end-user/search.md) (transcript search guide)
- **Skills usage**: [skills.md](end-user/skills.md) (review, understand, panel guide)
- **Visual diagrams**: [diagrams.md](diagrams.md) (architecture diagrams)
- **Reference details**: [design_appendix.md](design_appendix.md) (schemas, config tables, implementation specifics)

## 1. Philosophy: The "Glue" Approach

Forge is **not** a monolith. It is the **connective tissue** between specialized tools -- a monorepo of proven tools
sharing common libraries (Auth, Models, State) under a unified interface (`forge` CLI).

## 2. Core components (the "pieces")

These components run independently but share code (libraries/config).

| Component           | Responsibility                     | Location                    |
| :------------------ | :--------------------------------- | :-------------------------- |
| **Forge Proxy**     | Model routing, Auth, Tool fixing   | `src/forge/proxy/`          |
| **Forge Session**   | Session isolation, Worktrees       | `src/forge/session/`        |
| **Forge Skills**    | Agent workflows (Review, Planning) | `src/skills/` + `forge` CLI |
| **Forge Status**    | Visual feedback & Dashboard        | `src/forge/status/`         |
| **Forge Policy**    | Policy enforcement (TDD, safety)   | `src/forge/guard/`          |
| **Commands/Agents** | Claude Code extensions             | `src/{commands,agents}/`    |
| **Hooks**           | Lifecycle events (Claude Code)     | `src/forge/cli/hooks/`      |

> See [diagrams.md §1: Core Architecture Overview](diagrams.md#1-core-architecture-overview) for a visual overview.

## 3. Shared contracts: File-based state system

Forge uses file-based state instead of a DB. Two concepts are first-class and **must not be conflated**:

- **Session**: a Claude coding session (worktree, artifacts, user intent, hook-confirmed facts)
- **Proxy**: a proxy endpoint identity (base URL / port / template) that the proxy can actually enforce

> **Why proxy instances?** Claude Code proxy requests do **not** include a session identifier, so the proxy cannot know
> which session made a request. The only way to apply different routing or hyperparameters is to run separate proxy
> instances on different ports. A **proxy instance** is one such endpoint (base_url + port + template). Sessions
> reference proxies but cannot change proxy-owned routing—this is a technical constraint, not a design choice.

> See [diagrams.md §2: Session vs Proxy Separation](diagrams.md#2-session-vs-proxy-separation) for a visual explanation.

The **Proxy Orchestrator** lives in the Forge CLI (`forge proxy` subcommands). It manages proxy lifecycle: start
instances, register them in the proxy registry, and clean up stale proxies.

Forge uses a **three-part** contract:

1. **Session file** (per Forge project): `<forge_root>/.forge/sessions/<session_name>/forge.session.json`
2. **Proxy registry** (global): `~/.forge/proxies/index.json` → running proxies (template ↔ base_url ↔ pid)
3. **Runtime truth** (proxy mode only): live proxy introspection (`GET /` at the proxy base URL)

> **Clarification:** The session file is for **session UX** (artifacts, status, `forge session` commands), **not** proxy
> routing. The proxy's routing identity is the **proxy base URL** only.
>
> **Parallel sessions:** Multiple sessions can run in the same Forge project. Each session has its own subdirectory
> under `.forge/sessions/`. Hooks identify the session via `FORGE_SESSION` set at launch.

#### Project identity model

Forge has four scoping levels. They must be explicitly defined to avoid path confusion:

```text
project_root    (logical repo -- git identity, shared across worktrees)
  +-- checkout_root    (this worktree -- git rev-parse --show-toplevel)
       +-- forge_root      (enabled .claude/ + .forge/ project inside the checkout)
            +-- working_dir    (launch CWD -- for managed sessions, equals forge_root)
```

| Level             | Identity source                                  | Stored as       | Purpose                                               |
| ----------------- | ------------------------------------------------ | --------------- | ----------------------------------------------------- |
| **Logical Repo**  | `get_main_repo_root()` (git)                     | `project_root`  | Cross-project ops, `session list` default scope       |
| **Checkout**      | `git rev-parse --show-toplevel`                  | `checkout_root` | Worktree targeting for `--into`, relative_path anchor |
| **Forge Project** | Directory with `.claude/` + `.forge/`            | `forge_root`    | Session root, artifact root, state scoping anchor     |
| **Working Dir**   | Launch CWD (= `forge_root` for managed sessions) | implicit        | Managed sessions always launch from `forge_root`      |

**Four foundational rules (normative):**

1. A session may start only where `forge extension enable` has established a project/local install (`.forge/` exists).
2. The session root is exactly that install root (the **Forge project root**, `forge_root`).
3. Session state is scoped to `forge_root` -- manifests, artifacts, search index, `prev_sessions/` all live under that
   `.forge/`.
4. Project/local `forge extension enable` requires `.claude/` at the target directory. If missing, it is created
   silently (it is a directory, not a config file -- no ambiguity, no interactive prompt needed). User-level install
   (`--user`) goes to `~/.claude/` and does not require a project anchor.

**Definitions:**

- **Forge project** = directory containing both `.claude/` and `.forge/`, established by `forge extension enable`.
- **`forge_root`** = the Forge project root (where `.forge/` lives). Field in `SessionIndexEntry`.
- **`relative_path`** = `forge_root` relative to `checkout_root`. Preserved on `fork --into`.

**Fork `--into` rules (normative):**

- `--into` targets a **worktree** (different checkout), not an arbitrary path.
- Child session lands at the equivalent `forge_root` in the target worktree: `target_checkout_root / relative_path`.
- Target must have Forge enabled at that relative path. If not: error with "Run `forge extension enable` in
  `<target_checkout_root>/<relative_path>` first, or use `--worktree` to create a new checkout with auto-enable."
- No arbitrary path targeting -- you pick the worktree, the position is computed.

**Session command scoping (normative):**

- **`session list`**: repo-scoped by default (filter by `project_root`). Shows sessions across all Forge projects within
  the logical repo. `--scope project` narrows to current `forge_root`. `--scope all` shows everything globally.
- **`session show`, `session delete` (named), `session set`, `session reset`**: repo-scoped with current-project
  preference. Two-tier resolution: try current `forge_root` first (O(1)), fall back to repo-scoped scan. Prefers current
  `forge_root` as tiebreaker when the same name exists in multiple projects. Raises `AmbiguousSessionError` if truly
  ambiguous. Prints a cross-project note when resolving from a different `forge_root`.
- **`session delete --all`**: project-scoped (current `forge_root` only). Requires being inside a Forge project
  (`_cwd_forge_root() != None`); refuses to run outside one to prevent accidental global deletion.
- **`session resume`, `session fork`**: project-scoped. Cannot resolve cross-project because Claude Code's `--resume`
  and CWD namespace are tied to the project directory. Hints where the session lives on cross-project miss.
- **`session clean`**: global by default (no `forge_root` filter).
- **Artifacts, handoff, search**: Forge-project-scoped (all under `<forge_root>/.forge/`).
- **Cross-project resume** (handoff mode only): allowed within the same logical repo
  (`parent_project_root == child.project_root`). Reads parent artifacts by absolute path via `parent_forge_root` in the
  derivation record. **Native resume** (`--resume-mode native`) requires the same `forge_root` -- Claude Code cannot
  `--resume` across CWD boundaries (see §3.9).

**Exception:** `forge claude start` (bare launcher) works without `.forge/`. It does not create session state, does not
set `FORGE_SESSION`, and session-specific hooks/status behavior is a no-op. See §3.4.

> See [diagrams.md §10: Project Identity Hierarchy](diagrams.md#10-project-identity-hierarchy) for a visual overview.

#### Context model: Forge vs Claude Code

Claude Code scopes conversations to the project directory (`.claude/`). `--resume <uuid>` only finds conversations in
the current project's `.claude/`. Forge's project model (N sessions per Forge project, cross-project forking) extends
this.

When sessions cross **Forge project boundaries** (worktree forks, `fork --into`, resume), Forge uses **file-based
handoff**: `process_handoff()` reads the parent's transcript artifacts and generates a portable context file at
`<forge_root>/.forge/prev_sessions/<parent>.md`, appended at launch via `--append-system-prompt-file`. This is an
accepted tradeoff: handoff files are lossy compared to native `--resume` (structured summary vs full conversation), but
they enable branch isolation and cross-worktree workflows.

The `--strategy` knob controls fidelity: `minimal` (lineage pointer) → `structured` (conversation skeleton, default) →
`full` (complete transcript) → `ai-curated` (LLM-selected highlights). `--inline-plan` embeds the approved plan content
(from ExitPlanMode snapshots) directly into the handoff file — critical for review and supervision workflows where the
reader cannot access the original plan file.

Checkouts are **shared resources** (like proxies): multiple sessions can live in the same checkout. `delete_session()`
scans for co-resident sessions before removing a worktree, and sessions created via `--into` (`owns_worktree=False`)
never remove the worktree they're visiting. If the owning session is deleted before the last guest, Forge preserves the
checkout and leaves final cleanup to the user.

### 3.1 User story: Multi-proxy multi-session workflow

This workflow motivates Forge's separation of **Session** and **Proxy**.

**Goal:** Combine meticulous planning/review from one proxy (e.g., OpenAI-based) with fast/high-quality implementation
from another, while keeping artifacts and the working directory shared.

> See [diagrams.md §7: Multi-Proxy Workflow](diagrams.md#7-multi-proxy-workflow).

**Baseline flow:** Session A (planner, OpenAI proxy) → fork to Session B (executor, Anthropic proxy) → review loop
(resume A to review B's changes, feed fixes back). Optional Session C on a third proxy for independent review/synthesis.

**Why proxies, not session overrides:** Per-session routing is impossible without a session identifier in requests (see
§3). Sessions within a Forge project share the working directory; artifacts (plans, reviews) are captured per-session
for cross-session handoff. Worktrees are used when sessions write concurrently.

### 3.2 Contract files (authoritative paths)

| Artifact             | Path                                                             | Owned by                 | Purpose                                                                                 |
| -------------------- | ---------------------------------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------- |
| Session file         | `<forge_root>/.forge/sessions/<session_name>/forge.session.json` | Forge Session + Hooks    | Session `intent`, `overrides`, hook-written `confirmed`                                 |
| Global session index | `~/.forge/sessions/index.json`                                   | Forge Session            | Session metadata (name, `forge_root`, `project_root`); fast listing + project filtering |
| Active session index | `~/.forge/sessions/active.json`                                  | Forge Session            | Ephemeral live-launch registry for delete warnings + stale pruning                      |
| Proxy registry       | `~/.forge/proxies/index.json`                                    | Forge Proxy Orchestrator | Running proxies (template ↔ base_url/port ↔ pid)                                        |
| Runtime config       | `~/.forge/config.yaml`                                           | Forge CLI                | Global runtime preferences (proxy mode, timeouts, context limit)                        |
| Installed manifest   | `~/.forge/installed.json`                                        | Forge Installer          | Tracks what `forge extension enable` installed for update/uninstall                     |
| Work queue           | `~/.forge/pending-work/*.json`                                   | Forge Work Queue (§3.13) | Deferred work markers (stop, index, handoff)                                            |
| Optional events      | `~/.forge/events/*.jsonl`                                        | TBD                      | Debugging/analytics; optional                                                           |

The active session index is intentionally runtime-only. It is self-healed via launcher PID / sidecar container liveness
checks and must not be treated as durable session truth like the manifest or global session index.

**Global session index entry schema** (`~/.forge/sessions/index.json`):

```python
@dataclass
class SessionIndexEntry:
    project_root: str       # Logical repo -- cross-project ops, session list default scope
    checkout_root: str      # Worktree root -- --into targeting, relative_path anchor
    forge_root: str         # Forge project root -- state scoping anchor
    relative_path: str      # forge_root relative to checkout_root
    last_accessed_at: str
    is_fork: bool = False
    is_incognito: bool = False
    parent_session: str | None = None
    claude_session_id: str | None = None
```

`session list --scope` controls filtering: **`repo`** (default) filters by `project_root` -- shows sessions across all
worktrees and Forge projects within the logical repo. **`project`** filters by `forge_root` -- just this Forge project.
**`all`** shows everything globally.

### 3.3 Session file schema (`forge.session.json`)

**1:1 invariant:** Each Forge session corresponds to exactly one Claude process invocation.
`confirmed.claude_session_id` is **launch-owned** — it starts as `None` when a session is created, and is set by the
SessionStart hook when Claude actually starts. A non-null `claude_session_id` means "this session has been used."
Relaunching a used session creates a child with lineage (`parent_session`), not a reuse of the same session.

**Default resume behavior.** `forge session resume <name>` reattaches to the same Claude conversation without creating a
child. This relaxes the 1:1 model (a new process invocation on the same Forge session) and is the default path: the
session must have resumable evidence (hook confirmation or transcript-backed state) and must not currently be active.
Reattach mutates `confirmed` runtime facts (`confirmed_at`, `transcript_path`) — the confirmed section reflects "last
seen state." Use `--fresh` to derive a new child session with context assembly instead.

The session file has three sections:

> Schema is intentionally strict: unknown fields and unknown override keys are rejected.

| Section         | Definition                    | Written by            | Semantics                          |
| --------------- | ----------------------------- | --------------------- | ---------------------------------- |
| **`intent`**    | Baseline config Forge *wants* | `forge session start` | Session-owned fields only          |
| **`overrides`** | Live toggles on top of intent | `forge session set`   | Diff (can be cleared)              |
| **`confirmed`** | Ground truth of what happened | Hooks                 | Observed facts, immutable once set |

**`intent.launch`**: Forge-owned relaunch preferences for reproducible session launch:

```yaml
launch:
  mode: sidecar
  sidecar:
    mounts: [/data:/mnt/data:ro]
    image: my-dev-image:latest
```

This keeps `forge session resume <name>` honest for sidecar sessions without overloading `confirmed` with user-owned
preferences.

**`confirmed.started_with_proxy`**: the proxy this session is running with (set at start, immutable for the run):

```yaml
started_with_proxy:
  proxy_id: my-high-reasoning        # optional, same-machine convenience
  template: litellm-openai           # which template this proxy came from
  base_url: http://localhost:8085    # the actual routing identity
```

**Normative semantics:** `proxy_id` is optional. The portable fields are `template/base_url`.

#### Effective vs Confirmed (normative distinction)

| Term            | What it answers                | How computed                      | Stored?                |
| --------------- | ------------------------------ | --------------------------------- | ---------------------- |
| **`effective`** | "What *should* the config be?" | `intent` with `overrides` applied | No (derived on-demand) |
| **`confirmed`** | "What *actually happened*?"    | Hooks record facts                | Yes (persisted)        |

**Override rules** (for session `intent + overrides` only):

- Scalars: override replaces
- Lists: override replaces entirely (no concat)
- Dicts: recurse into nested keys (untouched keys preserved)
- Explicit `null`: clears the field

> **Note:** There is no "merging"—overrides simply win. The only subtlety is nested dicts: you can override
> `memory.tags` without losing `memory.auto_recall`. This applies to session-owned fields only (`tdd_mode`, `memory.*`,
> etc.). Proxy-owned fields come directly from the proxy.

### 3.4 Proxy vs no-proxy mode

- **Proxy mode**: Claude is configured to send requests to a proxy base URL (`ANTHROPIC_BASE_URL`).
  - The proxy (template ↔ base_url) is the **routing identity**.
  - Status/other tools may query the proxy (`GET /`) for tier→model mapping and context windows.
- **No-proxy mode**: Claude talks to Anthropic directly.
  - Sessions, worktrees, hooks, and overrides still work (for session-owned fields).
  - `forge session start` and `forge session incognito` default to direct mode. Use `--proxy` for proxy routing.
  - `forge claude start --no-proxy` is a bare launcher (no session state) -- see below.
  - Tier/model routing doesn't apply—it's proxy-only. Claude Code uses Anthropic models directly.

**Normative rule:** A session records which proxy it is running with (`confirmed.proxy`), but **cannot override**
proxy-owned routing properties. (Proxy requests do not carry a stable session identifier.)

**Normative requirement: Launch Claude through Forge.** Two launch paths exist:

**Session-managed launch** (`forge session start`, `forge session resume`):

- Requires `.forge/` at `forge_root` (i.e. `forge extension enable` must have run -- see project identity model above)
- Creates/reuses session state in `<forge_root>/.forge/sessions/`
- Sets `FORGE_SESSION` env var -- hooks and status line can locate the correct session file
- Sets `ANTHROPIC_BASE_URL` env var in proxy mode -- routes requests to the correct proxy
- Validates preconditions (proxy healthy, session file exists)
- Records `confirmed.proxy` at session start when proxy mode is active

**Bare launch** (`forge claude start`):

- Convenience proxy launcher -- does NOT create session state
- Does NOT set `FORGE_SESSION` -- session-specific hooks, status line session display, and artifacts are all no-ops
- Does NOT require `.forge/` -- works from any directory
- Only sets `ANTHROPIC_BASE_URL` (proxy mode) or nothing (direct mode)

Running `claude` directly bypasses both paths; neither proxy routing nor session integration will work.

> See [diagrams.md §6: Proxy Routing Flow](diagrams.md#6-proxy-routing-flow) for a sequence diagram.

### 3.5 File ownership boundaries (normative)

To avoid writer conflicts:

- Forge Session (CLI) writes:
  - `~/.forge/sessions/index.json` (includes `forge_root`, `checkout_root`, `project_root` per entry)
  - `intent` + `overrides` sections in `<forge_root>/.forge/sessions/<session_name>/forge.session.json`
  - `intent.launch` records relaunch mode plus sidecar-specific options (image, extra mounts) when the session is
    created or derived
  - `confirmed` bootstrap/runtime fields written by the CLI: `derivation` (resume metadata), `is_sandboxed` (updated at
    launch time to reflect whether Claude is running via sidecar)
  - Sets `FORGE_SESSION=<session_name>` when launching Claude
  - Note: `claude_session_id` is **not** pre-seeded by the CLI; it is set by the SessionStart hook (launch-owned)
- Hooks write:
  - `confirmed` section **during the session**: `claude_session_id`, proxy identity, artifacts, policy state, transcript
    paths. The SessionStart hook is the authoritative source for `claude_session_id`.
  - Locate session via `FORGE_SESSION`
- Forge Proxy Orchestrator writes:
  - `~/.forge/proxies/index.json`
  - per-proxy override files (if any)
- Forge Installer writes:
  - `~/.forge/installed.json`
  - installed extension files + merged settings per chosen scope
- Proxy writes:
  - proxy-owned snapshot/cache files (if any)
- Status:
  - read state; do not invent truth
- Guard:
  - reads state; enforces policy decisions at well-defined boundaries (hooks, proxy)
  - writes only hook-owned confirmed state (e.g., `confirmed.policy`) when running as a hook adapter

> See [diagrams.md §4: Ownership Boundaries](diagrams.md#4-ownership-boundaries).

### 3.6 Configuration System

#### 3.6.1 Definitions (normative)

| Concept            | Definition                                                                                                                                                                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Proxy**          | base_url/port/template + tier→model + default hyperparams. Canonical routing identity for a proxy.                                                                                                                                          |
| **Session**        | Forge-project-scoped intent/overrides/artifacts. May reference a proxy, cannot change proxy-owned fields.                                                                                                                                   |
| **Config**         | In-repo defaults + user credentials/connection values (env vars and/or `~/.forge/credentials.yaml`). Connection values (e.g., `LITELLM_BASE_URL`) bootstrap proxy creation; once `proxy.yaml` exists, proxy-owned routing is authoritative. |
| **Proxy Template** | Operational profile defining provider/endpoint/tier-mappings. Internal template for proxy creation.                                                                                                                                         |
| **Model Catalog**  | Authoritative internal data for model capabilities (`model_catalog.yaml`). Not user-editable.                                                                                                                                               |

#### 3.6.2 Field ownership invariants (normative)

| Owner             | Fields                                                                                                                         |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **Proxy-owned**   | tier→model mappings, provider/base_url, default hyperparams (reasoning_effort, temperature, verbosity, thinking_budget_tokens) |
| **Session-owned** | policy/TDD mode, memory/artifacts, `forge_root`, `checkout_root`, `relative_path`, session metadata                            |
| **Routing chain** | request explicit tier → proxy default tier                                                                                     |

**CLI enforcement:** Enforced in the CLI: `forge proxy` edits proxy settings; `forge session` edits session settings.
Session commands can't set proxy-owned keys.

#### 3.6.3 Proxy lifecycle UX

**Implemented:**

```bash
# List proxies
forge proxy list

# Create a proxy from template with optional per-tier overrides
forge proxy create litellm-openai \
  --opus-reasoning high \
  --sonnet-temperature 0.7

# Prune stale proxies (dead pids)
forge proxy clean
```

**Also implemented:**

```bash
# Start Claude pinned to this proxy
forge claude start --proxy <proxy_id>

# Edit proxy config
forge proxy edit <proxy_id>
# OR: forge proxy set <proxy_id> tier_overrides.opus.reasoning_effort=high

# Delete proxy
forge proxy delete <proxy_id>
```

**Key principle:** You do NOT edit internal templates/model catalog—only your proxy overlay.

> **Configuration reference details** — proxy overlay schema, template inventory, confusion traps, secrets, runtime
> config (`~/.forge/config.yaml`), model catalog, and status line guidance are in
> [design_appendix.md §A](design_appendix.md#a-configuration-reference).

### 3.7 Proxy runtime truth

When the proxy base URL is reachable, **live proxy introspection is authoritative** for tier→model mappings and context
windows. File caches are allowed but non-authoritative.

The proxy exposes runtime truth via `GET /`:

```json
{
  "is_proxy": true,
  "proxy": { "template": "litellm-openai", "base_url": "http://localhost:8085" },
  "tiers": {
    "haiku": { "model": "gpt-4o-mini", "context_window": 128000 },
    "sonnet": { "model": "gpt-4o", "context_window": 128000 },
    "opus": { "model": "o3", "context_window": 200000 }
  }
}
```

**Key points:**

- The proxy does **not** know about sessions (see §3.6.2)
- Session info comes from the session file, not the proxy
- Status line tools read both sources independently

**Tier selection precedence:**

1. Request explicit tier (model name contains `haiku|sonnet|opus`)
2. Proxy default tier (configured for that base URL)

### 3.8 Session artifacts (plans + transcripts)

Forge hooks capture **session-associated artifacts** to make sessions self-contained and inspectable later.

**Artifact storage (Forge-project-scoped):**

- `<forge_root>/.forge/artifacts/{session_name}/plans/`
- `<forge_root>/.forge/artifacts/{session_name}/transcripts/`

Notes:

- Artifacts are scoped to the **Forge project root** (`forge_root`). All sessions in a Forge project share one artifact
  namespace.
- Paths recorded into the session file under `confirmed` are **forge_root-relative** (portable across machines/paths).
- Cross-project operations (resume from a different checkout) read parent artifacts by **absolute path** via
  `parent_forge_root` in the derivation record (see §3.9).

**Plan snapshots:**

- We capture **approved** plan snapshots only (no drafts).
- Approval boundary: `ExitPlanMode`.
- Snapshot filename includes a timestamp suffix to handle replans (multiple approvals in a session).

**Transcript copies:**

- We copy the full transcript only at low-frequency boundaries:
  - `Stop` hook event (session end)
  - `/compact` or `/clear` rollover (captured by `SessionStart` with `source=compact|clear` before overwriting
    `confirmed.transcript_path`)
- Destination filename is `{session_id}.jsonl` (idempotent per Claude session UUID).

**Session file fields (hook-owned, additive):**

- `confirmed.latest_plan_path`: pointer to the latest plan file in `.claude/plans/…` (draft pointer)
- `confirmed.artifacts.plans[]`: entries like:
  - `{ kind: "approved", captured_at, source_path, snapshot_path }`
- `confirmed.artifacts.transcripts[]`: entries like:
  - `{ captured_at, reason: "stop"|"compact"|"clear", source_path, session_id, copied_path, copied }`

### 3.9 Session Resume (context management)

When context nears limits, `forge session resume --fresh` creates a new session with context assembled from the parent.
It's **two-phase**: raw artifacts stay immutable; context assembly is flexible.

**Phase 1: Handoff (parent session end)**

The Stop hook captures everything to artifacts — this is the **source of truth**:

```
<forge_root>/.forge/artifacts/<session>/
├── transcript.jsonl    # Full conversation (our normalized copy)
├── metadata.json       # Confirmed state, lineage pointer
└── plans/              # Approved plans
```

The hook also updates designated memory docs if work was completed.

**Phase 2: Resume (child session start)**

The resume command supports two **resume modes** (`--resume-mode`):

- **`handoff`** (default): Assembles parent context into a markdown file passed via `--append-system-prompt-file`. Lossy
  but survives `/compact` (lives in the system prompt). Size controlled by `--strategy`.
- **`native`**: Uses `--resume --fork-session` to carry full conversation history. Lossless but lost on `/compact`. No
  context file generated. Requires the parent to have a confirmed `claude_session_id`.

> **Why not native for worktree forks?** Claude Code stores sessions at `~/.claude/projects/<encoded-cwd>/`. `--resume`
> from a different CWD cannot find the session JSONL. Tested with Claude Code 2.1.90 (Apr 2026): all cross-CWD scenarios
> fail with "No conversation found." Worktree forks use handoff only.

**Handoff mode strategies** (`--resume-mode handoff`, default):

```bash
forge session resume <parent> --fresh --strategy <strategy> [--depth N]
```

| Strategy     | What child session sees                                        |
| ------------ | -------------------------------------------------------------- |
| `minimal`    | Lineage pointer only — "read parent if needed"                 |
| `structured` | Conversation skeleton with truncated tool results              |
| `full`       | Complete parent context (fails if exceeds proxy context limit) |
| `ai-curated` | AI-selected highlights from ancestry chain                     |

**Native mode** (`--resume-mode native`):

```bash
forge session resume <parent> --fresh --resume-mode native
```

No context assembly. Full conversation history carried over via Claude's `--fork-session`.

**Context budget enforcement:**

Resume knows the target proxy (inherited or via `--proxy`). For `full`, it **fails fast** if the parent transcript
exceeds the proxy context window:

```bash
$ forge session resume parent-session --fresh --strategy full
Error: Parent transcript (145K tokens) exceeds proxy context limit (128K).
       Use --strategy structured or --strategy ai-curated instead.
```

This prevents launching a session that would immediately hit limits. The check happens **before** spawning Claude—no
wasted tokens or mid-session failure.

For `structured` and `ai-curated`, strategies are bounded (truncation/AI selection), so no pre-flight check.

**Depth control:** Resume can traverse lineage, not just the immediate parent:

```yaml
depth: 1      # Immediate parent only (default)
depth: 3      # Parent + grandparent + great-grandparent
depth: all    # Full ancestry chain
```

This pulls relevant context from earlier sessions (e.g., a decision from 5 sessions ago).

**Processed context location:**

```
<forge_root>/.forge/prev_sessions/<parent-name>.md   # Strategy-dependent context view
```

You can resume the same parent with different strategies. Raw artifacts stay immutable; only the processed view changes.

**Session derivation tracking:**

Resumes and forks both populate `confirmed.derivation`; top-level `parent_session` remains a legacy lookup fallback for
older manifests.

```yaml
# In confirmed section of forge.session.json
derivation:
  parent_session: feature-auth-v1
  parent_forge_root: /abs/path/to/parent/forge/root   # Where to find parent artifacts
  parent_project_root: /abs/path/to/repo               # Must match child's project_root
  parent_transcript: .forge/artifacts/feature-auth-v1/transcript.jsonl
  inherited_proxy: litellm-anthropic     # From parent's proxy intent, if inherited
  resume_mode: handoff                  # "native" or "handoff" (authoritative)
  strategy: structured                  # null when resume_mode=native or handoff context not generated yet
  depth: 1
  resumed_at: 2025-01-02T15:30:00Z
  # Lineage chain (computed from parent pointers)
  lineage: [feature-auth-v1, feature-auth-v0, initial-planning]
```

Same-directory forks use `resume_mode: native`, `strategy: null`, `depth: 1`, and lineage containing the parent.
Worktree and `--into` forks start with `resume_mode: handoff`; the CLI enriches `strategy` and `context_file` when it
generates a handoff context file.

**Cross-project resume:** `parent_forge_root` tells the child where the parent's artifacts live (may differ from the
child's `forge_root` when the parent was in a different checkout). `parent_project_root` must equal the child's
`project_root` -- cross-repo resume is not supported.

**Context assembly (what child loads at start):**

1. Designated memory docs (always, via CLAUDE.md)
2. Processed handoff: `<forge_root>/.forge/prev_sessions/<parent>.md` (strategy-dependent)
3. Lineage reference: pointer to raw artifacts for deep reads

**Why two phases?**

- Raw artifacts preserve the full history for debugging and audit
- **Handoff** writes a portable, strategy-dependent view of that history
- **Resume** assembles context — user controls the fidelity/size trade-off
- Same raw data can produce different views for different needs

**Proxy inheritance:**

By default, the child inherits the parent's proxy (same routing):

```bash
# Parent was on litellm-anthropic proxy (opus)
# Child inherits same proxy by default
forge session resume feature-auth --fresh

# Override if needed
forge session resume feature-auth --fresh --proxy litellm-gemini
```

This keeps routing stable across resumes.

### 3.10 Hook handlers

Hooks write ground truth to the session file. The session manager writes `intent` (and user `overrides`); hooks write
`confirmed` facts (transcript paths, plan paths, proxy identity, etc.).

**Session identification:** Hooks locate the session via `FORGE_SESSION` (set at launch), enabling multiple sessions per
Forge project. Hooks use `FORGE_SESSION` + UUID lookup only. No CWD-based scan or fallback detection.

**Implementation:** Artifact capture uses first-class hook handlers (testable Python entrypoints), not ad-hoc scripts.

**Deployment model:** Forge installs hook **settings only** (no scripts in `.claude/`). Hooks run via the Forge CLI
(`forge hook <name>`), so runtime + deps live with the Forge package (single upgrade surface).

**Operational requirement:** `forge` must be on PATH for hook execution.

**Why `forge hook …` instead of installed scripts:**

1. **No dependency ambiguity** — install Forge once; deps resolved at install.
2. **No version drift** — hooks run the current Forge version.
3. **Auditable footprint** — `.claude/` contains config/markdown, not executables.
4. **Testable** — regular Python entrypoints (unit-testable, type-checkable).
5. **Session-aware** — reads session file; per-session decisions.

**Artifact capture hooks:**

- `forge hook plan-write` (PostToolUse:Write): Updates `confirmed.latest_plan_path` for plan files.
- `forge hook exit-plan-mode` (PreToolUse:ExitPlanMode): Snapshots approved plan to artifacts.
- `forge hook stop` (Stop:\*): Runs the Stop pipeline (see below).
- `forge hook pre-compact` (PreCompact): Captures full transcript before compaction to artifacts. Canonical compaction
  snapshot; SessionStart rollover is fallback for `/clear` and defense-in-depth.
- `forge hook post-compact` (PostCompact): Records compaction metadata (`last_compact_at`, `last_compact_type`).
- `forge hook worktree-create` (WorktreeCreate): Replaces Claude Code's default `git worktree add` to auto-install Forge
  extensions. Prints worktree path to stdout. Only hook that exits non-zero on failure.
- `forge hook subagent-stop` (SubagentStop): Tracks subagent activity (`total_count`, `by_type`, transcript path,
  message preview). Observe-only (phase 1).

**Stop hook pipeline:**

The Stop hook does multiple things. To avoid blocking exit and ensure idempotency across repeated invocations, it's
split into **sync** and **async** phases:

```
Stop Pipeline:

  [Sync - blocks exit decision, must be <100ms]
  1. capture_artifacts()    Copy transcript to .forge/artifacts/ (idempotent via UUID)
  2. run_verification()     Check completion promise → returns allow|block

  [Async - enqueued, fire and forget]
  3. enqueue_handoff_work() Mark session for handoff agent + indexing

  return verification_decision
```

The handoff agent runs **async** to avoid blocking exit. Memory doc updates are eventually consistent—fine since they
benefit future sessions.

**Idempotency rules** (verification can trigger Stop multiple times per session):

| Step          | Multiple invocations safe? | How                                                 |
| ------------- | -------------------------- | --------------------------------------------------- |
| Artifact copy | ✔ Yes                      | Writes to UUID-named path, overwrites are identical |
| Verification  | ✔ Yes                      | Stateless check of last message                     |
| Async enqueue | ✔ Yes                      | Marker file is idempotent (same content = no-op)    |

**Async enqueue:** The Stop hook enqueues a marker via `enqueue_stop_marker()` for deferred processing. See §3.13 (Async
Work Queue) for the queue contract, schema, and processing model.

This keeps the Stop hook fast (\<100ms) while ensuring handoff + indexing happen soon.

Design rule: hooks emit machine-readable JSON; no `systemMessage` required (handoff agent replaces manual reminders).

> See [diagrams.md §5: Hook Deployment Model](diagrams.md#5-hook-deployment-model).

### 3.11 Direct commands (UserPromptSubmit dispatcher)

Forge supports a **direct command** channel to invoke Forge actions inline from the Claude prompt without adding slash
commands or changing hook wiring.

**Design goal:** install **one** `UserPromptSubmit` hook, then add new `%<cmd>` handlers over time **without
reinstalling hooks**.

> **⚠︎ Limitation:** `UserPromptSubmit` hooks only fire in **interactive** Claude sessions. They do NOT fire in
> `claude --print` mode (non-interactive/piped). `--print` has no user prompt submission event. Do not rely on `%`
> commands working in `--print` mode or automated scripting that uses `--print`.

Mechanism:

- Claude Code `UserPromptSubmit` hook runs: `forge hook user-prompt-submit`
- The handler parses prompts that begin with `%` and dispatches to the appropriate command implementation.
- Unknown `%<cmd>` strings are ignored (normal Claude flow continues).

Response contract:

- When a direct command is handled, the hook returns a Claude Code decision payload:
  - `{ "decision": "block", "reason": "..." }`
- When not handled, it emits no output and exits successfully.

**Scope policy:** `%` commands are primarily session-scoped. Proxy commands are restricted to read-only operations
because proxies are global (modifying a proxy mid-session could affect other sessions using the same proxy). Proxy
management should be done deliberately from terminal.

> Full command list and scope policy table in [design_appendix.md §B](design_appendix.md#b-direct-command-reference).

### 3.12 Command-core ops (shared implementation)

Forge implements "Shared" operations once in a UI-agnostic command-core layer and exposes them via both:

- terminal CLI (`forge ...`), and
- direct commands (`%...` via `forge hook user-prompt-submit`).

**Location:** `src/forge/core/ops/`

**Contract:** ops contain pure logic (no Click, no printing, no hook JSON). They return structured data and raise typed
exceptions on failure.

This avoids duplicating business logic between terminal and in-session entry points.

### 3.13 Async work queue

A **general-purpose, file-based queue** for deferred work. Producers enqueue markers; CLI startup processes them
opportunistically. This is a core primitive used by the Stop pipeline, search indexing, and handoff agent.

**Module:** `forge.core.workqueue`

**Queue location:** `~/.forge/pending-work/` (respects `FORGE_HOME`)

#### Design goals

- **Best-effort enqueue**: failures are non-fatal (never block hooks or CLI)
- **Fast path**: no-op when queue is empty (cheap directory scan)
- **Concurrent-safe**: per-marker advisory locks (`<marker_id>.json.lock`)
- **Exactly-once-ish**: markers deleted on successful handler completion
- **Eventually consistent**: deferred work benefits future sessions, not the current one

Each marker is a JSON file with `kind` (routing key), `marker_id` (idempotency key), `payload` (kind-specific data), and
retry tracking (`attempt_count`/`last_error`). Handlers are passed as an explicit dict (no global registry). Successful
handling deletes the marker; poison markers (5+ attempts) move to `pending-work/failed/`.

> Marker schema, processing contract, and known kinds in
> [design_appendix.md §C](design_appendix.md#c-work-queue-internals).

## 4. Unified CLI (`forge`)

The primary entry point for all Forge operations.

### 4.0 Command reference

**Command aliases:** `authentication` (canonical) has alias `auth`; `extension` (canonical) has alias `ext` and
`extensions`; `session` (canonical) has alias `sess`. Full names always work; aliases are convenience shortcuts.

**Command-shape policy:** Forge uses explicit verbs for all commands. `config` and `search` support
`invoke_without_command` as convenience defaults (`config` shows effective config, `search -q` runs a query). All other
groups require an explicit subcommand. List/show commands support `--json` for scripting.

#### Installation

| Command                   | Purpose                                                      |
| ------------------------- | ------------------------------------------------------------ |
| `forge extension enable`  | Install Forge extensions (commands, agents, hooks, settings) |
| `forge extension sync`    | Update existing installation to current version              |
| `forge extension disable` | Remove Forge installation cleanly                            |
| `forge extension status`  | Show installation status (`--json`)                          |

#### Session management

| Command                                | Purpose                                                                           |
| -------------------------------------- | --------------------------------------------------------------------------------- |
| `forge session start [name]`           | Create and start a new session (auto-named if omitted)                            |
| `forge session resume [name]`          | Reattach to an existing session (default), or derive a fresh child with `--fresh` |
| `forge session fork <parent> [--name]` | Fork a session (same dir by default; `--worktree` for isolation)                  |
| `forge session show [session]`         | Show session details (`--json`, `--field`); accepts name or UUID                  |
| `forge session list`                   | List sessions (`--scope repo\|project\|all`; default `repo`; `--json`)            |
| `forge session set <key> <value>`      | Set a mid-session override                                                        |
| `forge session reset [key]`            | Reset overrides to intent                                                         |
| `forge session delete <name>...`       | Delete one or more sessions (`--all` for bulk deletion)                           |
| `forge session clean --older-than N`   | Bulk-delete sessions older than N days                                            |
| `forge session incognito [name]`       | Start an ephemeral session (auto-delete on exit)                                  |
| `forge session shell [name]`           | Open shell in sidecar container                                                   |

Note: `session context` is a deprecated alias for `session show`.

#### Proxy management

| Command                              | Purpose                                                |
| ------------------------------------ | ------------------------------------------------------ |
| `forge proxy create <template>`      | Create a proxy from template and start it              |
| `forge proxy list`                   | List all proxies (`--json`)                            |
| `forge proxy show <id>`              | Show proxy configuration (`--json`, `--raw`)           |
| `forge proxy edit <id>`              | Edit proxy overlay in $EDITOR                          |
| `forge proxy set <id> <key>=<value>` | Set a proxy configuration value                        |
| `forge proxy start <id>`             | Start server for existing proxy                        |
| `forge proxy stop <id>`              | Stop server (keeps config)                             |
| `forge proxy delete <id>...`         | Delete one or more proxies (`--all` for bulk deletion) |
| `forge proxy clean`                  | Remove stale proxies (dead pids)                       |
| `forge proxy validate <id>`          | Validate proxy configuration                           |
| `forge proxy metrics [id]`           | Show runtime metrics (`--json`, `--all`)               |
| `forge proxy template list`          | List available templates                               |
| `forge proxy template show <name>`   | Show template configuration (`--raw`)                  |
| `forge proxy template edit <name>`   | Customize a template (copy-on-first-edit)              |
| `forge proxy template reset <name>`  | Reset template to built-in defaults                    |

#### Claude Code management

| Command                           | Purpose                                     |
| --------------------------------- | ------------------------------------------- |
| `forge claude start --proxy <id>` | Launch Claude configured for a proxy        |
| `forge claude start --no-proxy`   | Launch Claude without proxy (Anthropic API) |
| `forge claude preset show`        | Show current settings preset (`--raw`)      |
| `forge claude preset edit`        | Edit settings preset in $EDITOR             |
| `forge claude preset reset`       | Reset preset to built-in defaults           |

#### Backend management

| Command                          | Purpose                           |
| -------------------------------- | --------------------------------- |
| `forge backend list`             | List backends (`--json`)          |
| `forge backend show <adapter>`   | Show backend details (`--raw`)    |
| `forge backend create <adapter>` | Create backend config             |
| `forge backend start <adapter>`  | Start backend instance            |
| `forge backend stop <adapter>`   | Stop backend instance             |
| `forge backend delete <adapter>` | Delete backend instance or config |

#### Policy enforcement

| Command                                       | Purpose                                       |
| --------------------------------------------- | --------------------------------------------- |
| `forge guard enable --bundle <name>`          | Enable policy enforcement for current session |
| `forge guard disable`                         | Disable policy enforcement                    |
| `forge guard status`                          | Show current policy state (`--json`)          |
| `forge guard list`                            | List available bundles and rules (`--json`)   |
| `forge guard check --bundle <name> -f <path>` | Evaluate policies on demand                   |
| `forge guard supervisor -f <path> -r <id>`    | Evaluate file against approved plan           |
| `forge guard supervise <target>`              | Set persistent supervisor for session         |
| `forge guard supervise --off / --on`          | Suspend/resume supervisor (preserves config)  |
| `forge guard supervise --remove`              | Remove supervisor entirely                    |
| `forge guard supervise --reload`              | Reload latest relevant approved plan          |
| `forge guard supervise --reload-from <path>`  | Reload plan from explicit file                |

#### Workflow

| Command                              | Purpose                                    |
| ------------------------------------ | ------------------------------------------ |
| `forge workflow panel [targets]`     | Fan out review to multiple models          |
| `forge workflow analyze [topic]`     | Deep single-model analysis                 |
| `forge workflow debate [subject]`    | Adversarial evaluation with stance workers |
| `forge workflow consensus [subject]` | Two-round multi-model convergence          |
| `forge workflow list-models`         | Show available model backends              |

#### Search

| Command                      | Purpose                              |
| ---------------------------- | ------------------------------------ |
| `forge search -q <query>`    | Search transcripts                   |
| `forge search rebuild-index` | Full index rebuild from artifacts    |
| `forge search status`        | Show index statistics                |
| `forge search clean`         | Remove orphaned documents from index |

#### System

| Command                       | Purpose                                    |
| ----------------------------- | ------------------------------------------ |
| `forge info`                  | Show global system information (`--json`)  |
| `forge clean`                 | Remove orphaned state (`--scope`, `--yes`) |
| `forge config`                | Manage global runtime preferences          |
| `forge authentication login`  | Store credentials for LLM providers        |
| `forge authentication status` | Show credential status per provider        |
| `forge logs`                  | Show log file locations and status         |

#### Internal (hidden from `forge --help`)

| Command             | Purpose                                    |
| ------------------- | ------------------------------------------ |
| `forge hook <name>` | Hook dispatcher (SessionStart, Stop, etc.) |
| `forge status-line` | Generate status line output                |
| `forge handoff run` | Run handoff agent for completed session    |

**Design principles:**

- **Narrow global config** -- `forge config` owns runtime preferences only; routing stays per-proxy and workflow state
  stays per-session
- **Explicit verbs** -- all groups require a subcommand (`config` and `search` are the two exceptions with
  `invoke_without_command` defaults)
- **Launch through Forge** -- `forge session start`, `forge session resume`, or `forge claude start --proxy` sets up env
  vars correctly

### 4.1 Policy (enforcement)

Forge Policy is an **enforcement system** with three types:

1. **Deterministic Policy (Guard)**: Static checks, file mapping, dependency rules (Fast/Free).
2. **Semantic Policy (Supervisor)**: LLM-based alignment checks against plans (Smart/Context-aware).
3. **Verification Policy**: Outcome-based checks at session boundaries (Feedback loop).

| Policy Type   | Boundary               | Question                         |
| ------------- | ---------------------- | -------------------------------- |
| Deterministic | PreToolUse             | "Is this action allowed?"        |
| Semantic      | PreToolUse (throttled) | "Is this aligned with the plan?" |
| Verification  | Stop                   | "Did it achieve the goal?"       |

**Definition:** a policy is an **enforcement function** that runs at a well-defined boundary (hook, proxy, commit hook)
and returns an **action decision** with an explanation.

At minimum:

- **Input**: an *action context* (what is about to happen)
- **Output**: `allow | warn | deny | needs_review` plus human-readable reasons. `needs_review` is an intermediate
  decision: the semantic supervisor must resolve it to `allow`, `warn`, or `deny`; if no configured supervisor resolves
  it, the hook blocks as unresolved.
- **Intent**: every policy declares *why* it exists — shown to models on deny so they can distinguish good workarounds
  (satisfy the goal) from bad ones (defeat it)

#### 4.1.1 Deterministic Policy (Forge Guard)

Forge Policy is designed to support **deterministic policies first**.

- **Engine**: policy interfaces, composition, decisions
- **Adapters**: hook boundary (no-proxy) and proxy boundary (proxy-mode)
- **Policy bundles**: TDD is expressed as a set of deterministic policies (e.g., "tests must exist before
  implementation").

**Base class contract** (`DeterministicPolicy`):

| Abstract property | Type  | Purpose                                                              |
| ----------------- | ----- | -------------------------------------------------------------------- |
| `policy_id`       | `str` | Unique identifier (e.g., `tdd.tests-before-impl`)                    |
| `description`     | `str` | Human-readable description                                           |
| `intent`          | `str` | Why this policy exists — shown on deny so models understand the goal |

All three are **required** (abstract). The `intent` field was added after observing that models (e.g., GPT-5.5) would
find creative workarounds that pass the check but defeat the goal (Unicode escapes to bypass byte-level emoji
detection). Showing the intent alongside the violation steers models toward compliant approaches or surfacing conflicts
to the user.

**Why enforce coding standards via policy?** AI assistants tend to favor gradual migration and backward compatibility
over clean breaks—even when explicitly instructed otherwise. Patterns like `warn+ignore`, fallback logic, and
compatibility shims sneak into codebases despite best efforts. Deterministic policies can catch these at commit/hook
boundaries:

- Reject code containing `# backward compat`, `# legacy`, `# deprecated` comments
- Flag new `if TYPE_CHECKING:` blocks (circular import workaround)
- Detect `warn` + `strip`/`ignore` patterns in validation code

This doesn't require a stateful system—detecting backward compat patterns in a diff is a pattern-matching task that
Haiku handles fine.

**Policy bundles** group related rules:

| Bundle             | Rules                                            | Purpose                 |
| ------------------ | ------------------------------------------------ | ----------------------- |
| `tdd`              | tests-before-impl, no-skip-tests                 | Test-driven development |
| `coding_standards` | no-bsd-sed, no-type-checking, no-backward-compat | Platform/style rules    |

Bundles are enabled per-session:

```yaml
# In session intent
policy:
  bundles: [tdd, coding_standards]
  tdd_mode: strict  # off | permissive | strict
```

#### 4.1.2 Semantic Policy (The Supervisor)

This enables **"Active Alignment"** checking using the **Side-Channel Architecture**.

Promotion flow (`--fork-current`) is deferred so the building blocks (supervisor, panel, session forking) can compose
before we hardcode a default. For now: `forge session set policy.supervisor.resume_id <uuid>`.

**Mechanism: "CLI-Fork Supervision"**

The `policy-check` hook runs the supervisor via `claude -p --resume <supervisor_id>`:

1. **Configure**: `forge session set policy.supervisor.resume_id <uuid>` (from planning session).
2. **Check**: Runs at PreToolUse for Write/Edit, throttled via cache (default 30s).
3. **Enforce**:
   - **Aligned**: Silent success (cached for throttle window).
   - **Divergent + high confidence + citations**: Block the tool (exit 2).
   - **Divergent + low confidence or no citations**: Warn via stderr, allow the tool.
   - **Unresolved review request**: Block the tool (exit 2) until a supervisor is configured or the user gives a new
     direction.

**Why this works:** Forking the planning session makes the plan's original context the enforcement authority (no RAG).
Side-channel architecture: Executor uses a high-IQ coder (Opus); Supervisor uses a high-context checker (Gemini).

**Promotion readiness:** Depends on ground truth quality: explicit acceptance criteria, invariant constraints, resolved
ambiguities.

**Supervisor lifecycle controls:**

- `--off` / `--on`: Toggle without config loss. `--off` sets `suspended=True` (config preserved, hook skips evaluation
  entirely — not registered in the policy engine); `--on` resumes. `--remove` is the destructive path. Both CLI and
  direct command surfaces. All three pre-check that a supervisor is configured before acting.
- `--reload` / `--reload-from <path>`: Inject an updated plan into supervisor evaluation context. `--reload` searches
  the supervision graph in order: current supervised session, related forks (sessions in the same `forge_root` whose
  parent is the supervisor target), supervisor target session. Only approved snapshots are considered (no drafts). The
  plan content is prepended to each evaluation prompt with explicit supersession framing. `--reload-from` takes an
  explicit file path (resolved relative to CWD, stored absolute). Cache key includes a `path:mtime_ns:size` fingerprint
  so in-place edits invalidate cached verdicts.
- `plan_override_path` on `SupervisorConfig` stores the override. It can be set while the supervisor is suspended
  (configure plan, then `--on`). Proxy routing is not re-seeded on `--on` — the preserved config is used as-is.
- Auto-reload may succeed even if the supervisor target session has been deleted (the current session or a related fork
  may still hold the plan). Status surfaces show `Target: <name>` when resolvable and omit it otherwise.

**Supervisor stuck playbook:** When the supervisor blocks because the plan evolved:

- `%guard supervise off` (suspend, config preserved)
- Make the approved changes
- `%guard supervise reload` (searches current session, forks, then target) or `%guard supervise reload <path>`
- `%guard supervise on` (resume with updated plan context)

**The underspecification problem (biggest failure mode):** Supervision catches explicit divergence (plan says X, agent
did Y, citations are clear). Underspecification is harder: the plan is silent, the model picks a plausible default, and
neither agent nor supervisor can cite against it — so the verdict may be "aligned." The real divergence is between
unwritten human expectations and model assumptions. Mitigation: (a) more explicit plans (write down the implicit), (b)
multi-model review (different defaults expose gaps), (c) the human reformulation loop (make assumptions explicit,
rerun).

**Operational reliability constraints (normative):**

- **Citations required**: Every **Divergent** finding MUST cite (quote) the specific plan/design section it violates.
- **Structured verdict**: The Supervisor response SHOULD be parseable (even if implemented as plain text initially):
  - `verdict`: `aligned | divergent`
  - `violations[]`: `{ severity, evidence, suggested_fix, citations[] }`
- **Block only on high-confidence + cited rule**: Default behavior is **warn-only** unless the Supervisor provides a
  clear cited rule and a high-confidence violation.
- **Fail open vs fail closed**: Policies MUST define failure behavior per severity (e.g., CLI failure, proxy down,
  timeout). Default to **fail-open (warn-only)** for most checks. Fail-open for policy evaluations is a system-boundary
  rule (LLM output is external data), not an exception to coding-standards §5. See coding-standards.md §5 (boundary
  framework) for the general framework.
- **Throttling + caching**: Supervisor checks SHOULD be throttled (e.g., every N turns, only on Write/Edit, only for
  configured path prefixes) and MAY cache the last verdict for identical diffs.

**On-demand invocation (planned):** Every policy — including the supervisor — should be callable manually without
installing hooks:

```bash
forge guard check supervisor --file src/forge/session/store.py
forge guard check tdd --diff HEAD~1
```

The same evaluation function runs in both modes (hook-triggered and CLI-triggered).

**Primary use case: problem reformulation.** When a policy stops the agent, the cause is flawed problem representation
(too broad/contradictory/ambiguous), genuine agent failure, or an overzealous policy. On-demand checks are diagnostics:
citations and evidence show *what* failed. Reformulate, then re-check before resuming. The supervisor's `--resume`
context keeps the original plan in view.

**Reactive Patterns (Shared Library)**

Several components react to hook events via external processing: semantic supervisor (`guard/semantic/supervisor.py`),
handoff agent (`session/handoff_agent.py`), deterministic policies (`guard/deterministic/`), and the planned workflow
policy. The shared pattern: take hook context, classify/evaluate, return a decision or side-effect. Three node types
cover current and planned use cases:

| Node type      | Execution                         | Examples                                  | Cost        |
| -------------- | --------------------------------- | ----------------------------------------- | ----------- |
| Code           | Deterministic Python function     | TDD enforcement, path gating, file checks | Free        |
| LLM call       | Stateless API call via `core.llm` | Tagger (classification), checker          | ~$0.001     |
| Claude session | `claude -p [--resume]` subprocess | Supervisor, handoff agent                 | ~$0.01-0.05 |

**Library, not framework**: Utilities live in a shared Python library (`core/reactive/`). Hook handlers are plain Python
functions that import what they need. No YAML workflow engine, no declarative config layer — the same developers who
would write YAML can write Python with less indirection and better debuggability.

The shared library provides utilities extracted from existing implementations: session runner, proxy resolution,
throttle cache, structured output parsing, tagger, env builder, fan-out runner, and adversarial runner. A developer
adding a new policy imports these utilities and writes a class.

> Shared library API table and example policy code in [design_appendix.md §D](design_appendix.md#d-policy-internals).

**WorkflowPolicy (tagger → branch → checker → reviewer)**: Plugs into PolicyEngine via existing
`Policy + StatefulPolicy` protocols (zero changes to the engine). Composes library utilities into a branching pipeline:
a shared tagger classifies the action, branches match by tags (first match wins), each branch has optional filter →
checker → reviewer stages. The tagger is called once per event and its tags route to all matching downstream checks —
avoiding redundant classification.

**Team extension**: The same library works for team hooks (`TeammateIdle`, `TaskCompleted`) by subscribing to different
events. See [team_design.md](proposals/team_orchestration.md) §3.

#### 4.1.3 Verification Policy (Feedback Loop)

Verification policies check **outcomes** rather than **actions**. They run at the **Stop boundary** and can block
session exit until goals are achieved.

**The Ralph-Wiggum Pattern:**

Instead of external bash loops, verification uses the Stop hook to create a self-referential feedback loop:

1. User starts session with a completion promise
2. Claude works toward the goal
3. Stop hook checks: "Did Claude output the completion signal?"
4. If no → block exit, re-inject prompt, continue
5. If yes → allow exit

The prompt never changes between iterations, but Claude's previous work persists in files. Each iteration sees modified
files and git history, enabling autonomous improvement.

**Configuration:**

```yaml
# In session intent
verification:
  type: completion_promise    # or: test_suite, custom_command
  promise: "<done>COMPLETE</done>"
  max_iterations: 50          # safety limit
  on_incomplete: re_inject    # or: warn, allow
  re_inject_prompt: |
    Continue working. Output <done>COMPLETE</done> when all requirements met.
```

**Verification types:**

| Type                 | Verification method        | Use case          |
| -------------------- | -------------------------- | ----------------- |
| `completion_promise` | Look for text in output    | Goal-driven tasks |
| `test_suite`         | Run tests, check exit code | Code changes      |
| `custom_command`     | Run any command            | Domain-specific   |

**Completion promise correctness:**

To avoid false positives (promise appearing in quoted files, code examples, or earlier failed iterations):

1. **Check only the last assistant message** — ignore tool results and conversation history
2. **Require standalone line** — promise must appear on its own line, not embedded in prose

The re-inject prompt should instruct Claude accordingly:

```yaml
re_inject_prompt: |
  Continue working. When ALL requirements are met, output this on a standalone line:
  <done>COMPLETE</done>
```

This prevents false matches from `print("<done>COMPLETE</done>")` in code or discussion like "I'll output
`<done>COMPLETE</done>` when done."

**Escape hatches:** `%cancel-verification` direct command, `max_iterations` auto-bypass, `max_minutes` wall-clock limit,
or `forge session set verification.bypass true` from another terminal. The bypass is a session parameter (discoverable,
auditable). Both time limits matter: `max_iterations` catches fast-failing loops; `max_minutes` catches slow token burn.

**Why verification is policy, not a separate concept:**

- All three policy types share the same structure: boundary + check + action
- Verification just fires at a different boundary (Stop vs PreToolUse)
- Keeps the design unified under "Policy"

#### 4.1.4 Action context

Policies operate on a normalized view of what Claude Code is doing, for example:

- hook event (`PreToolUse.Write`, `PreToolUse.Edit`, …)
- tool arguments (target path, content/diff metadata)
- repository/worktree path
- effective session config (intent + overrides)

#### 4.1.5 Policy composition

Multiple policies may run for a single action:

- **Any deny** in enforce mode blocks the action
- warnings accumulate
- results can be logged for audit/debug

**Deny message format** (three-tier, shown to the model):

```
Policy violation(s):
  [rule_id] violation message
    Intent: why the policy exists
    Fix: suggested fix (if available)
    Note: This policy was configured by the project owner. First try a
    compliant approach that satisfies the intent above. If the user's
    request cannot be fulfilled without violating the intent, explain
    the conflict and ask how to proceed. Do not attempt bypasses that
    pass the check but defeat the goal.
```

The `Intent:` line appears once per denying policy (not per violation). The `Note:` uses project-owner framing so models
treat it as a constraint to respect, not an obstacle to circumvent.

#### 4.1.6 Policy state and ownership

Policy has two aspects with different ownership: **definition** (configuration — who sets the rules) and **state**
(runtime — what happened). Supervisor model and throttling are proxy-owned (routing decisions). TDD mode, policy
enabled/disabled, and verification config are session-owned (workflow decisions). All enforcement results are
hook-written to `confirmed.policy`.

**Policy provenance:** `confirmed.policy` records `forge_version`, `bundles`, `rules_active`, and `decisions` for
audit/debugging ("why did this block?").

**Ownership rationale:** Supervisor model = routing decision → proxy. TDD mode = workflow decision → session.
Enforcement results = observed facts → hook-written `confirmed`. Stateful policies (e.g., "tests touched") write only to
`confirmed.policy`.

> Full policy definition and state ownership tables in [design_appendix.md §D](design_appendix.md#d-policy-internals).

## 5. Extensions install model

Claude Code extensions live in this repo and are installed via `forge extension enable`. Forge follows Claude Code's
scope model (`--user` / `--project` / `--local`) and provides modular installation via profiles (`minimal` / `standard`
/ `full`). Six installable modules (commands, agents, skills, hooks, status-line, permissions) are combined into
profiles. Settings merge is additive (hooks append + dedupe, permissions union). `~/.forge/installed.json` tracks what
was installed for clean update/uninstall. Project/local enablement requires a `.claude/` anchor at the target directory
(created if missing); user-level install (`--user`) goes to `~/.claude/` and does not require a project anchor. This
establishes the Forge project per the identity model (§3).

> Scope model, module inventory, merge rules, and tracking file details in
> [design_appendix.md §E](design_appendix.md#e-install-model-reference). Multi-scope installation behavior (dual user +
> project) is documented in [§E.5](design_appendix.md#e5-multi-scope-installation-55----skill-resolution).

### 5.5 Skills architecture

Skills are Forge's **scripting layer**: they teach Claude to compose Forge capabilities into workflows. Game engines
have Lua; editors have VimScript; Forge has skills. The `forge` CLI is the engine (proxy routing, session management,
`core.llm`). Skills are the instructions; the agent orchestrates.

Skills don't add tools—Claude already has Read/Write/Bash. Skills add the playbook for composing them with Forge
(multi-proxy routing, session forking, policy checks).

#### 5.5.1 Reflective architecture

Forge installs skills about itself: `forge extension enable` deploys CLI commands (capabilities) and skills (how to use
them). The system teaches the agent about itself.

- Coherent upgrades: `forge extension sync` updates CLI + skills atomically
- No version drift between "what the tool can do" and "what the agent thinks the tool can do"
- Agents can modify skills (markdown files on disk)
- Matches hooks/status-line pattern: `forge` is the engine; extensions are instructions

#### 5.5.2 Why skills over MCP

Forge uses skills (not MCP tools) for agent workflows. MCP servers remain useful for external data access (APIs,
databases, OAuth), but aren't the right abstraction for workflow orchestration.

| Aspect            | Skills                                    | MCP Tools                             |
| ----------------- | ----------------------------------------- | ------------------------------------- |
| Token cost        | Typically ~100 tokens metadata at startup | Often 3K-10K+ for tool definitions    |
| Context pollution | Full instructions load only when invoked  | Tool schemas persist in context       |
| Architecture      | Reflective — skills reference own install | External — separate server process    |
| Context passing   | Fork session (full context preserved)     | Summarize and send (information loss) |
| Determinism       | Agent interprets instructions each time   | Structured JSON-RPC interface         |

**Fork advantage:** Skills can fork the current session (`claude -p --resume <uuid>` on another proxy), giving reviewers
the **full conversation context** (files, decisions, rationale). MCP tools only see what the agent summarizes into tool
parameters.

#### 5.5.3 Execution modes

Skills that invoke multi-model workflows support two **context modes**; the caller chooses based on the situation —
skills present options, not prescriptions.

**Resume context mode:** `claude -p --resume <session-uuid>` on another proxy; inherits full session context. Best when
conversation history matters. Requires Claude Code >= 2.1.80 for reliable parallel tool result handling (`--resume`
dropped parallel tool results in earlier versions).

**Blind context mode:** Fresh `claude -p` (no `--resume`); rely on the prompt + filesystem reads. Cheaper and
independent. Best for isolated reviews and quick checks.

**CLI contract:** Workflow CLIs expose this axis explicitly:

- `--context resume:<session-uuid>` → pass `--resume <id>` to each worker
- `--context blind` → do not pass `--resume` (workers are independent)

**CLI surface:** `forge workflow <workflow>` (e.g. panel/analyze/debate). Default is human-readable; `--check` produces
a policy-grade verdict (JSON + exit code).

#### 5.5.4 Skill execution types

Skills vary in what they execute. Four types cover all current and planned cases:

| Type            | Execution                                   | Examples                                   |
| --------------- | ------------------------------------------- | ------------------------------------------ |
| Pure Python     | Deterministic function                      | TDD guard, pattern matching, `run_tests()` |
| Single LLM      | `core.llm` API call                         | Tagger, checker                            |
| Claude session  | `claude -p [--bare]` subprocess (has tools) | Supervisor, reviewer, handoff agent        |
| Pure text (.md) | Markdown instructions sent to `claude -p`   | Review resources, analyze, debate prompts  |

Claude session subprocesses use `--bare` when `ANTHROPIC_API_KEY` is in the environment (skips hooks, LSP, plugin sync,
skill walks for faster startup). `--bare` disables OAuth/keychain auth, so it is only safe when an explicit API key is
available. They are full Claude Code agents (Read/Grep/Bash/Write) but cannot invoke skills that spawn more subprocesses
(`FORGE_DEPTH` prevents recursion at depth >= 2 as defense-in-depth). Pure text (.md) is a markdown prompt run in that
environment.

Skills can declare `effort: high|medium|low` in their SKILL.md frontmatter (Claude Code 2.1.80+). This overrides the
model effort level when the skill is invoked -- useful for deep-analysis skills (`analyze`, `debate`) that benefit from
maximum reasoning. This is orthogonal to proxy-level `reasoning_effort` hyperparameters, which control the routed
model's behavior.

This maps to the three node types in §4.1.2 (Code, LLM call, Claude session). "Pure text" is a specialization: no Python
runtime deps, so the prompt is portable across models/runners (the execution environment still has tools).

#### 5.5.5 Workflow runners

Multiple skills compose smaller skills into orchestrated loops. Forge recognizes a small set of **fundamental workflow
runners**: reusable Python functions in `core/reactive/` that each implement one loop pattern.

**Three-layer architecture:**

| Layer | What                 | Lives in                  | Examples                                         |
| ----- | -------------------- | ------------------------- | ------------------------------------------------ |
| 1     | Abstract runners     | `core/reactive/`          | Fan-out, adversarial, linear, actor/critic       |
| 2     | Skill resources      | `src/skills/*/resources/` | Review resource .md, analyze prompt, tagger      |
| 3     | Concrete invocations | `src/skills/*/SKILL.md`   | `/forge:panel`, `/forge:debate`, `/forge:review` |

Layer 3 entry points wire a runner (Layer 1) to specific resources (Layer 2). The same runner/resource can be combined
differently by different entry points.

**Four fundamental runners (conservative set):**

| Runner         | Loop pattern                                 | Status            | Current implementation                 |
| -------------- | -------------------------------------------- | ----------------- | -------------------------------------- |
| Linear         | A → B → C (sequential)                       | Exists            | WorkflowPolicy pipeline, Stop pipeline |
| Fan-out/Fan-in | N workers parallel → collect → synthesize    | Exists, enhancing | `run_multi_review()`                   |
| Adversarial    | N workers with stances, blinded → synthesize | Exists            | `run_adversarial()`                    |
| Actor/Critic   | Generate → critique → iterate                | Pattern exists    | Ralph-wiggum verification loop         |

**Design principles:**

- Python, not YAML — continues the "library, not framework" approach
- Each runner takes skills, returns structured output
- Callable from skills (on-demand) and policies (automatic)
- Conservative set: fundamental patterns only

The **fan-out runner** (`run_multi_review()`) spawns N workers in parallel via `ThreadPoolExecutor`, each with its own
model/proxy and optional per-worker prompt. The **adversarial runner** constrains workers to review/eval skills with
stance injection (`{stance_prompt}`), mandatory blinding (no peer outputs), and evidence-weighted synthesis.

#### 5.5.6 Relationship to policies (workflow unification)

Skills and policies are the **same building blocks with different triggers**:

|          | Policies                              | Skills                              |
| -------- | ------------------------------------- | ----------------------------------- |
| Trigger  | Hook event (automatic, on Write/Edit) | Agent/user invocation (on demand)   |
| Output   | Allow/deny decision                   | Information for agent to synthesize |
| Latency  | Adds overhead to every action         | Zero overhead until invoked         |
| Use case | Continuous enforcement                | Deliberate checks                   |

Both compose from the same primitives: `core/reactive/`, `core.llm`, `run_claude_session()`. Shared code is imported by
both CLI commands and policy classes—no workflow registry, no declarative config layer. Library, not framework.

**CLI surfaces (normative):** Forge uses two related command surfaces:

1. **Guard** — deterministic/semantic policies evaluated against an action context.

   - Hook surface: `forge hook …` invokes Guard policies automatically.
   - Manual surface: `forge guard check …` runs Guard policies on demand (after a hook blocks you, or in CI).

   **Stuck playbook (target UX):** When a PreToolUse policy blocks repeatedly, give the human an escape hatch without
   uninstalling hooks.

   - **Disable enforcement in-session:** `%guard disable` (hook becomes a no-op for this session)
   - **Fix the issue:** work with the agent or edit manually while enforcement is disabled
   - **Confirm you're unblocked (optional):**
     - `%guard check` (planned): defaults to `git diff` (unstaged). Supports `--staged`.
     - Terminal fallback: `git diff | forge guard check --bundle tdd --bundle coding_standards --diff`
   - **Re-enable enforcement:**
     - `%guard enable` (planned): with no bundles, restores the session's configured bundles from intent.
     - `%guard enable tdd coding_standards`: explicitly sets bundles for the session.

   `forge guard check` (and `%guard check`) are diagnostics; you're unstuck once enforcement is re-enabled and the next
   Write/Edit passes the hook.

2. **Run** -- multi-step workflow runners (fan-out, debate, etc.).

   - Default: `forge workflow <workflow>` returns a human-readable result.
   - Gate mode: `forge workflow <workflow> --check` forces a policy-grade verdict contract (structured JSON + exit
     code).

**No auto-promotion:** A workflow does not automatically appear in Guard. If a Guard policy wants to use a workflow, it
invokes the workflow's `--check` surface explicitly.

**Workflow runners unify skills and policies.** The same runner is usable from:

- Skills (agent/user invoked)
- Hooks/policies (automatic gate via `--check`)
- CLI manual runs (human debugging)

#### 5.5.7 Panel (fan-out reference skill)

Panel is the reference invocation of the fan-out runner. It fans out a review task to N models via different proxies and
collects independent findings for synthesis. Each reviewer is a full Claude Code agent (can read files, investigate,
find issues with real file:line evidence). The main agent synthesizes all N reviews -- identifying consensus findings,
unique insights, and conflicts -- with full project context to investigate disputes.

**Dual use:** The panel serves as both a skill (`/forge:panel src/session/ --code`) and a policy (automatic multi-model
gate before committing). Same `run_multi_review()` function, two callers -- the programmer wires both. `/forge:analyze`
is a degenerate fan-out (N=1) with an analyze-specific resource.

#### 5.5.8 Debate (adversarial reference skill)

Debate is the reference invocation of the adversarial runner. It assigns stances (for/against/neutral) to workers,
blinds them from each other (separate `claude -p` processes, no `--resume`), and synthesizes by weighing agreement
against disagreement. Stances influence the evaluative lens, not honesty -- all stances include ethical guardrails. Only
review/evaluation skills are adversarial-compatible (runner checks for `{stance_prompt}` marker).

> Detailed runner configs, debate protocol, and operational constraints (recursion guard, JSON output contract, child
> process lifecycle, script dependency tiers) in
> [design_appendix.md SF](design_appendix.md#f-workflow-runner-and-skill-details).

### 5.6 Designated memory docs

Cross-session continuity via designated markdown files that sessions keep updated—no knowledge graphs or async
synthesis.

The simplest memory system is:

1. Designated markdown files with templates
2. Sessions read them at start (via CLAUDE.md references)
3. Sessions update them before ending
4. Next session gets current state

#### 5.6.1 Handoff agent (automated doc maintenance)

A headless agent runs at session end to fill gaps automatically:

```
Stop hook → spawn headless agent → agent reads transcript + current docs → updates
```

The agent runs `claude -p` (headless prompt mode) on the full session transcript. It operates **retrospectively**,
selecting what mattered with full-session hindsight (higher signal than incremental capture).

```yaml
# In session intent or project config
memory:
  auto_update:
    enabled: true
    mode: augment              # augment (add missing) | review-only (dry run)
    proxy: litellm-haiku       # cheap model for summarization
    min_turns: 5               # skip for very short sessions
```

**Multi-agent workflow:** In parallel runs, each agent spawns its own handoff agent. `augment` mode stays additive (no
overwrites).

#### 5.6.2 Two operating modes

The handoff agent has two distinct modes for designated docs:

**Mode 1: Direct Update** — agent updates the doc in place per strategy. Used for project docs the agent is allowed to
maintain.

**Mode 2: Shadow/Propose** — the agent is the proposer, the human is the author. It reads transcript + official doc,
proposes additions to a shadow doc that aren't already in the official doc; the human reviews and merges at their own
pace.

The mode is determined by the `shadows` field on `DesignatedDoc`:

```python
@dataclass
class DesignatedDoc:
    path: str                       # Target file to write
    strategy: str = "generic"       # How to update it
    shadows: str | None = None      # If set: official doc this proposes changes for
```

When `shadows` is set, the prompt reads the official doc first and proposes only what's missing (avoids redundancy).

#### 5.6.3 Strategy registry

Per-doc strategies control how each file is updated. Strategies are a `str → str` dict (extend without code changes).

**No file creation.** Designated docs must already exist; missing files are skipped. Humans choose which docs to
maintain; the agent maintains them. This avoids the agent making structural choices (new files/templates) implicitly.
Seed files before configuring them.

Direct update strategies (Mode 1) include: `project-state`, `checklist`, `changelog`, `debugging`, `patterns`,
`generic`. Shadow strategy (Mode 2): `suggested` (propose additions as checkboxes with rationale).

The handoff agent resolves designated doc paths relative to `forge_root` (managed sessions always launch from
`forge_root`), so git-tracked docs target the correct branch in worktrees. Trackedness is controlled by path choice --
the agent doesn't distinguish.

**Relationship to Claude Code auto-memory:** Complementary, not competitive. Auto-memory captures during sessions
(incremental, free-form); the handoff agent synthesizes after sessions (retrospective, per-doc strategies). The handoff
agent deliberately does not read auto-memory — different targets, different information, occasional duplication is
cheaper than cross-format deduplication.

> Strategy tables, example config, worktree resolution details, and full auto-memory comparison in
> [design_appendix.md §G](design_appendix.md#g-memory-doc-reference).

### 5.7 Test Infrastructure (Docker-based)

**Runtime architecture (host-based)**: Proxy runs on host (`subprocess.Popen`), Claude Code runs on host. End users do
NOT need Docker.

**Test infrastructure (Docker-based)**: Integration tests run inside Docker containers (developers/CI only) to ensure:

- No Dockerfile/fixture drift (single source of truth)
- Tests catch real bugs (e.g., proxy startup failures)
- Deterministic test environment across machines

**Test workflow**:

```bash
# Unit tests (no Docker needed)
uv run pytest tests/src -m "not integration"

# Integration tests (Docker required for developers/CI only)
make test-integration  # Runs: docker build + docker run pytest
```

### 5.8 Interactive Manual Testing

Automated tests catch logic bugs but miss UX/latency/real-system failures. Previous manual testing found 5 real bugs
(including a macOS crash) that ~2,400 automated tests missed.

**Why checklist-driven.** Early versions let the agent improvise commands — producing invented CLI commands, interactive
prompts that hang the Bash tool, and leaked API keys. The fix: pre-written checklists where commands and assertions are
deterministic and the agent only interprets results. Checklist edits change tests without modifying skill instructions.

**Three skills** with escalating isolation, tied to install profiles:

| Skill                | Profile    | Isolation                                          | Audience          |
| -------------------- | ---------- | -------------------------------------------------- | ----------------- |
| `/forge:smoke-test`  | `standard` | Host, read-only probes                             | End users         |
| `/forge:walkthrough` | `standard` | Host, hermetic test repo (`--sidecar` adds Docker) | End users / demos |
| `/forge:qa`          | `full`     | Docker container                                   | Maintainers       |

**Shared pattern — checklist + wrapper + annotations.** Each skill reads a checklist, runs commands through a
mode-specific wrapper, and routes items by annotation. A three-window model (Session A runs the skill, Session B is the
subject under test, Terminal for raw CLI) enables interactive verification of things the agent can't see. Session A
prompts the user to open Terminal early. Session B is launched only when the checklist first needs interactive
verification.

**Key design decisions:**

- Share the pattern/convention, not the prompt — each skill is self-contained (no cross-mode confusion)
- Checklist is single source of truth — editing it changes tests without SKILL.md modifications
- `walkthrough-state.py` is the deterministic bookkeeper — agent classifies (pass/fail/skip), script counts
- No per-checklist-item scripts — wrapper + lifecycle scripts are enough
- `/forge:qa` tied to `full` install profile (Docker dependency)

> Annotation types, wrapper details, and per-skill specifications in
> [design_appendix.md §I](design_appendix.md#i-interactive-manual-testing). See also
> [testing-guidelines.md](developer/testing-guidelines.md) for the full testing reference.

## 6. Directory structure (monorepo)

```text
claude-forge/
├── src/
│   ├── forge/    # Python package
│   │   ├── core/        # Shared libraries
│   │   │   ├── llm/     # LLM client abstraction (see design_appendix.md §J)
│   │   │   ├── auth/    # Auth flows (LiteLLM, credential store)
│   │   │   ├── models/  # Model catalog (forge.models.yaml)
│   │   │   └── state/   # File-based state helpers
│   │   ├── session/     # Session manager
│   │   ├── install/     # Installer system
│   │   ├── proxy/       # Proxy - uses core.llm
│   │   ├── guard/       # Guard - uses core.llm
│   │   └── status/      # Status dashboard
│   │
│   ├── commands/        # Slash commands (installed to ~/.claude/commands)
│   ├── agents/          # Agents (installed to ~/.claude/agents)
│   └── skills/          # Skills (installed to ~/.claude/skills) — scripting layer (§5.5)
│
├── docs/
└── pyproject.toml
```

---

## 7. Isolation and Proxy Modes

| Concern                  | Solution                                     | Owner                                                                                             |
| ------------------------ | -------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Security isolation       | Seatbelt/bubblewrap per-command              | Claude Code native ([sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)) |
| Full container isolation | microVMs via `docker sandbox run`            | [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/claude-code/)                             |
| Proxy lifecycle coupling | `--sidecar` bundles proxy + Claude in Docker | Forge sidecar mode                                                                                |

**Sidecar mode** solves operational problems (not security): lifecycle coupling, port isolation, version consistency,
log isolation. Configurable via `~/.forge/config.yaml` (`proxy_mode: host|sidecar`), overrideable with `--sidecar` /
`--host-proxy`. Mounts `.claude/` and `.forge/` from host; does NOT mount `~/.forge` (UID issues, undermines port
isolation). Sidecar sessions also persist their launch mode, extra mounts, and image in `intent.launch` so
`forge session resume <name>` can replay the same runtime wiring later.

**Forge still owns:** Docker test infrastructure, runtime config. `src/forge/sidecar/` provides sidecar mode —
operational, not a security sandbox.
