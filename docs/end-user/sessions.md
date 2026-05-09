# Forge Sessions — Session Manager Guide

**Status:** Implemented for session management (naming, worktrees, artifacts). Updated here to match the **Session vs
Proxy** regime in `docs/design.md`.

- Canonical architecture: [`docs/design.md`](../design.md)
- Proxies (proxy endpoints): [`proxies.md`](proxies.md)
- Configuration system: [`configs.md`](configs.md)

---

## What a session is (and is not)

A **session** is a human unit of work with a **1:1 relationship** to a Claude process invocation:

- named session identity (portable name)
- worktree association (optional for parallel work — multiple sessions can also run in the same directory)
- session manifest (`.forge/sessions/<name>/forge.session.json`) storing intent/overrides/confirmed facts, including
  relaunch preferences
- artifacts (approved plans, transcripts)
- exactly one `claude_session_id` (set by the SessionStart hook when Claude starts, not pre-seeded)

**1:1 invariant:** `claude_session_id = None` means the session was never launched. A non-null value means it has been
used. Relaunching a used session creates a **child session** (a fork with lineage), not a reuse of the same session.
Related sessions are grouped by lineage (`parent_session`), not by UUID accumulation.

A session is **not** a proxy routing identity.

- Proxy routing defaults are **proxy-owned**.
- Sessions cannot override proxy-owned routing/hyperparams.

---

## Session state: what files exist

- Session manifest (per Forge project): `<forge_root>/.forge/sessions/<name>/forge.session.json`
- Global session index: `~/.forge/sessions/index.json` (name, forge_root, project_root, last-used-at, UUID)
- Active-session registry: `~/.forge/sessions/active.json` (runtime-only live launches; self-heals stale entries)

> **Session identity:** Hooks use Forge launch env vars only. Resolution order is: `FORGE_FORK_NAME` -> `FORGE_SESSION`
> -> IndexStore UUID lookup. No CWD-based directory scan.

Multiple sessions can coexist in the same Forge project, each with its own directory under `.forge/sessions/`.

The session file includes hook-confirmed facts such as:

- `confirmed.claude_session_id` (launch-owned: set by SessionStart hook, `None` until Claude starts)
- `confirmed.transcript_path`
- `confirmed.started_with_proxy` (snapshot from the SessionStart hook; `{base_url, proxy_id?, template?, port?}`)

> `proxy_id` is a same-machine convenience; `base_url` is the primary runtime truth, and `template` is best-effort
> metadata.

---

## Launch through Forge (recommended)

Always launch Claude through Forge to get session tracking:

**Two launch paths exist:**

**Session-managed launch** (`forge session start`, `forge session resume`) — full lifecycle tracking:

```bash
forge session start                                            # Auto-named, direct to Anthropic
forge session start my-feature                                 # Named, direct to Anthropic
forge session start my-feature --proxy litellm-gemini-local    # Named + proxy routing
```

This gives you: named session with manifest, hook-driven plan snapshots, transcript capture, status line, session
resume, search, and handoff agent. Requires `forge extension enable` first (creates `.forge/`).

**Bare launch** (`forge claude start`) — proxy routing only, no session state:

```bash
forge claude start --proxy litellm-gemini-local
forge claude start --no-proxy
```

No `FORGE_SESSION` set, no session manifest, no artifacts. Session-specific hooks and status line are no-ops. Does not
require `.forge/`. Use `forge session start` for managed sessions.

Running `claude` directly bypasses both paths.

---

## Core commands (cheat sheet)

> **Alias:** `forge sess` is a shorthand for `forge session`.

### CLI Reference

```bash
# Bare launch (proxy routing only, no session state)
forge claude start --proxy <proxy_id>
forge claude start --no-proxy

# Create/start managed session (full lifecycle tracking)
forge session start [name] \
  [--proxy <proxy_id>] [--no-proxy] \
  [--worktree/-w] [--branch/-b <branch>] \
  [--incognito/-i] \
  [--system-prompt/-s <text>] \
  [--system-prompt-file/-S <path>] \
  [--sidecar|--host-proxy] [--mount <host:container>] [--image <name>] \
  [--no-launch]

# Resume an existing session (default: reattach; --fresh: context assembly)
forge session resume <name>
forge session resume <name> --fresh

# Derive a fresh child session (PARENT optional; interactive picker)
forge session resume [parent] --fresh \
  [--child-name/-n <child_name>] \
  [--strategy/-s minimal|structured|full|ai-curated] \
  [--depth/-d <n>] \
  [--resume-mode native|handoff] \
  [--proxy <template>]

# Show / list
forge session show            # Current session (from $FORGE_SESSION)
forge session show <name>     # Named session details
forge session list            # Sessions in current repo (default: --scope repo)
forge session list --scope project  # Sessions in current Forge project only
forge session list --scope all      # All sessions globally

# Fork (conversation branching)
forge session fork <parent> [--name <name>] [--incognito] [--branch <branch>] [--worktree] [--into <path>] [--supervise] [--supervisor-proxy <id>] [--no-supervisor-proxy] [--no-launch]

# Delete
forge session delete <name> [--keep-worktree] [--delete-branch] [--force] [--keep-transcripts]

# Clean (age-based bulk delete)
forge session clean --older-than DAYS [--dry-run] [--force] [--keep-transcripts] [--delete-worktree] [--delete-branch]

# Incognito (same options as start, auto-deletes on exit)
forge session incognito [name] [--proxy <proxy_id>] [--no-proxy]
  [--worktree/-w] [--branch/-b] [--system-prompt/-s] [--system-prompt-file/-S]
  [--sidecar|--host-proxy] [--mount] [--image] [--extensions/--no-extensions]

# Mid-session toggles (session-local only)
forge session set <key> <value> [--session <name>]
forge session reset [key] [--all] [--session <name>]

# Sandboxed session shell
forge session shell [name]
```

If Forge still sees a live launch in `~/.forge/sessions/active.json`, `forge session delete` warns before removing the
session. `--yes` skips the confirmation prompt; `--force` overrides dirty-worktree and corruption guards.

### Session cleanup

Clean up old sessions by age:

```bash
forge session clean --older-than 30           # Delete sessions > 30 days old
forge session clean --older-than 30 --dry-run # Preview what would be cleaned
forge session list --older-than 30            # List old sessions before cleaning
```

Active sessions are always skipped. Worktrees and branches are preserved by default. Claude transcript files
(`~/.claude/projects/*.jsonl`) are deleted; Forge artifact snapshots (`.forge/artifacts/`) are not.

For automatic cleanup, set `session_retention_days` in `~/.forge/config.yaml`:

```bash
forge config set session_retention_days=90    # Auto-clean sessions > 90 days on CLI startup
```

Auto-cleanup runs opportunistically on each `forge` command (same pattern as log retention). It never deletes worktrees
or branches automatically.

---

## Prerequisites

Sessions require a **Forge project** — a directory with `.forge/` (and `.claude/`), created by `forge extension enable`:

```bash
cd my-repo
forge extension enable --local    # Creates .claude/ and .forge/ if needed
forge session start my-feature    # Now works
```

Without `.forge/`, `forge session start` fails with a clear error. The bare launcher (`forge claude start`) does not
require `.forge/`.

---

## Session scoping (`forge_root`)

All session state (manifests, artifacts, search index, handoff files) is scoped to the **Forge project root**
(`forge_root`) — the directory containing `.forge/`. In most setups this is your repo root. In monorepos with nested
Forge projects, each project has its own session namespace.

### Which commands resolve cross-project?

Most session commands resolve sessions **repo-wide** — if `list` shows a session, you can interact with it regardless of
which Forge project you're currently in (within the same git repo):

| Command                   | Scope                | Notes                                             |
| :------------------------ | :------------------- | :------------------------------------------------ |
| `session list`            | Repo (default)       | `--scope project` / `--scope all`                 |
| `session show`            | Repo-wide            | Prefers current project; shows cross-project note |
| `session delete` (named)  | Repo-wide            | Prefers current project; shows cross-project note |
| `session delete --all`    | Current project only | Requires being inside a Forge project             |
| `session set` / `reset`   | Repo-wide            | Via `--session` flag                              |
| `session resume` / `fork` | Current project only | CWD-dependent (Claude Code constraint)            |
| `session clean`           | Global               | All projects regardless of CWD                    |

When the same session name exists in multiple Forge projects within the repo, the current project wins. If you're not in
any of them, you'll see an error listing the locations.

When forking `--into` another worktree, the child session lands at the **equivalent position** — if the parent was at
`monorepo/packages/app`, the child lands at `target-worktree/packages/app`. The target must have Forge enabled at that
path.

---

## Workflows

### Start a session

```bash
forge session start                   # Auto-named (e.g., "happy-fox")
forge session start auth-refactor     # Explicit name
```

Typical effects:

- creates/updates the session manifest: `<forge_root>/.forge/sessions/auth-refactor/forge.session.json`
- updates the global index: `~/.forge/sessions/index.json` (including last-used time)
- registers a runtime live-session entry: `~/.forge/sessions/active.json` (cleared when the launch exits)
- sets `FORGE_SESSION=auth-refactor` env var
- launches Claude Code

### Start a session in a worktree (optional for filesystem isolation)

```bash
forge session start auth-refactor --worktree
```

Why use a worktree:

- isolates **filesystem changes** (no cross-talk between sessions editing files)
- useful when sessions will be modifying code concurrently

> Worktrees add **filesystem** isolation so multiple sessions can modify files concurrently without conflicts. Sessions
> can also coexist in the same worktree (see [Session state](#session-state-what-files-exist)).

### Start a sidecar session (Docker isolation)

```bash
forge session start auth-refactor --sidecar
```

Why use sidecar mode:

- bundles proxy + Claude Code inside a Docker container (lifecycle coupling, port isolation)
- project directory is mounted at `/workspace`
- optional extra mounts: `--mount /data:/mnt/data:ro`
- custom image: `--image my-dev-image:latest`
- Forge records sidecar mode, extra mounts, and image in the session manifest so `forge session resume <name>` can
  replay them later

To open a shell inside a running sidecar session:

```bash
forge session shell auth-refactor
```

### Resume an existing session

```bash
forge session resume auth-refactor
```

Default behavior: **reattach** — resumes the **same** Claude conversation in the **same** Forge session. This reopens
the existing conversation in place after the previous launch has ended.

Behavior depends on whether the session was previously used (1:1 invariant):

- **Never launched** (`claude_session_id` is None): launches Claude in-place, bound to this session. The SessionStart
  hook sets `claude_session_id` on first start.
- **Previously used** (`claude_session_id` is set): creates a **child session** (a fork) and resumes the parent's Claude
  conversation via `--resume --fork-session`. Claude gets a distinct new UUID in the child.
- If the session was created in sidecar mode, Forge relaunches it in sidecar mode again using the recorded image and
  extra mounts.

**Gates** (hard-fail, not warn):

- Session must have **resumable evidence**. Hook-confirmed sessions work, and transcript-backed sessions also work if
  the SessionStart hook missed confirmation. Pre-seeded UUIDs by themselves are not enough.
- Session must **not be currently active**. Fails if another launcher is still running for this session.

### Derive a fresh session from an existing one

```bash
forge session resume auth-refactor --fresh
# or: interactive pick to choose a parent
forge session resume --fresh
```

`forge session resume --fresh` creates a new child session derived from the parent. By default it uses assembled handoff
context; `--resume-mode native` carries the full Claude conversation instead.

**Resume modes** (`--resume-mode`):

| Mode                | Mechanism                                           | Trade-off                       |
| ------------------- | --------------------------------------------------- | ------------------------------- |
| `handoff` (default) | Assembled context via `--append-system-prompt-file` | Lossy but survives `/compact`   |
| `native`            | `--resume --fork-session` (full conversation)       | Lossless but lost on `/compact` |

```bash
# Default: assembled context (handoff)
forge session resume auth-refactor --fresh

# Lossless: carry full conversation history
forge session resume auth-refactor --fresh --resume-mode native
```

Native mode requires the parent to have a confirmed Claude session ID (i.e., the session must have been launched at
least once). `--strategy` and `--depth` are ignored in native mode.

Resume and fork-recovery launches inject the generated handoff file directly with `--append-system-prompt-file`. If you
customize `CLAUDE.md`, do not also add manual references to `.forge/prev_sessions/...` there, or you may duplicate the
same handoff context.

### Fork a session (branch the conversation)

```bash
forge session fork auth-refactor --name auth-refactor-alt
```

A fork creates a new named session that branches the parent's Claude conversation. By default the fork stays in the same
directory, so Claude's `--resume --fork-session` finds the parent conversation and carries it over.

**What gets copied:**

- Session file (`intent`, `overrides`, `confirmed`) -> new session's location
- `confirmed.latest_plan_path` -> forked session inherits the same plan
- Claude Code conversation context -> carried over via `--fork-session` (same directory)

**With `--worktree` (code isolation):**

```bash
forge session fork auth-refactor --name auth-refactor-alt --worktree
```

Creates a git worktree for the fork. `--branch` implies `--worktree`. Because Claude conversations are project-scoped,
the fork starts a fresh Claude session in the new worktree and automatically injects a parent handoff context file
(`.forge/prev_sessions/<parent>.md`). Claude knows where the parent left off, but the old visible chat history is not
replayed.

**With `--into` (existing worktree):**

```bash
forge session fork planner-session --into /path/to/executor-worktree
```

Forks into an **existing** non-main worktree. The fork gets the parent's conversation context (via handoff file) but
lands in the target worktree's code. The target must be part of the same git repository (validated via
`git-common-dir`). The main checkout is rejected — use a same-directory fork instead.

Key differences from `--worktree`:

- No git worktree creation (target already exists)
- No `.env`/`.mcp.json` copying (target already has them)
- Auto-install of extensions is skipped if Forge already has a tracked local install for the target worktree
- The session does NOT own the worktree (`owns_worktree=False`): deleting it never removes the worktree, and if the
  owning session was deleted earlier, final worktree cleanup is left to you

**Handoff options:**

| Flag             | Purpose                                                                    | Default      |
| ---------------- | -------------------------------------------------------------------------- | ------------ |
| `--strategy <s>` | Context assembly strategy (`minimal`/`structured`/`full`/`ai-curated`)     | `structured` |
| `--inline-plan`  | Embed the approved plan content in the handoff (not just a path reference) | off          |

These flags apply to `--worktree` and `--into` forks (file-based handoff). Same-directory forks use native
`--resume --fork-session` and ignore these flags.

**Use case: Plan -> Execute -> Review workflow:**

```bash
# 1. Plan
forge session start planner
# ... plan, approve plan, /exit

# 2. Execute in worktree with plan supervision
forge session fork planner --worktree --supervise
# ... implement; supervisor auto-checks every Write/Edit against the plan

# 3. Review: fork planner into executor's worktree with plan inlined
forge session fork planner --into /path/to/executor-worktree --inline-plan
# Reviewer sees: planner context + approved plan + executor's code
```

The `--supervise` flag wires the parent as a semantic supervisor. Every code change is checked against the approved plan
at `PreToolUse` time. Supervisor config persists through `forge session resume`. You can also wire supervision on
existing sessions with `forge guard supervise <session>` or `%guard supervise <session>` in-session.

**Supervisor routing:** By default, the supervisor inherits the planner's proxy. Use `--supervisor-proxy` or
`--no-supervisor-proxy` to override:

```bash
# Fork with supervisor on a different model (e.g., Gemini for checking, Opus for coding)
forge session fork planner --worktree --supervise --supervisor-proxy litellm-gemini --no-proxy

# Fork with supervisor going direct to Anthropic
forge session fork planner --worktree --supervise --no-supervisor-proxy

# Same flags work on session start
forge session start executor --supervise planner --supervisor-proxy litellm-gemini

# Or change supervisor routing on an existing session
forge guard supervise planner --supervisor-proxy litellm-gemini
```

**Supervisor lifecycle controls:**

```bash
# Suspend supervision (preserves config — resume_id, proxy, timeouts)
forge guard supervise --off
%guard supervise off

# Resume suspended supervisor
forge guard supervise --on
%guard supervise on

# Remove supervisor entirely
forge guard supervise --remove
%guard supervise remove

# Reload plan when it evolves (searches current session, forks, target)
forge guard supervise --reload
%guard supervise reload

# Reload from explicit file
forge guard supervise --reload-from ~/.claude/plans/updated-plan.md
%guard supervise reload /path/to/plan.md
```

The planner session stays intact throughout — it can be forked multiple times for different executors or reviewers.

---

## Using sessions with proxies (proxy endpoints)

Sessions can record which proxy they started with, but they do **not** control routing.

**Key principle:** Proxies own routing. Sessions own workflow. See [proxies.md](proxies.md) for routing configuration.

### Launch Claude with a proxy

```bash
forge claude start --proxy <proxy_id>
```

This resolves the proxy, healthchecks it, sets `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, and launches
Claude.

### Start a session with a proxy

```bash
forge session start my-session --proxy litellm-openai
```

`--proxy` sets the session's initial proxy intent. It accepts a proxy ID or template name. Without `--proxy`, sessions
default to direct mode (Anthropic API).

The invariant: choosing a proxy chooses routing defaults (model family, context limit).

### Pin a Claude model (`--model`)

```bash
forge session start review-pass --model claude-opus-4-7
forge session start long-sonnet --model claude-sonnet-4-6[1m]
forge session start review-pass --proxy openrouter-anthropic --model claude-opus-4-7
```

`--model` behavior depends on the session routing mode:

| Mode                                    | What `--model` does                                                 | `[1m]` support                 |
| --------------------------------------- | ------------------------------------------------------------------- | ------------------------------ |
| Direct (no `--proxy`)                   | Pins Claude Code's `ANTHROPIC_MODEL` directly                       | Yes                            |
| Proxy + alternative configured          | Selects a `model_alternatives` entry; proxy routes to backend model | Yes (stripped at proxy lookup) |
| Proxy + no alternative                  | Errors: "does not configure model alternative for ..."              | N/A                            |
| Subprocess proxy (`--subprocess-proxy`) | Pins Claude Code env vars (main is direct; subprocesses inherit)    | Yes                            |

Rejected with `--sidecar` or `--host-proxy`.

Forge stores the normalized model pin in the session intent and relaunches resume/fork children with the same
`ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_*_MODEL` environment variables. The stable `claude-opus`/`opus` aliases point
at Claude Opus 4.6; use `claude-opus-4-7` explicitly for Opus 4.7.

For proxy-mode `model_alternatives` configuration, see [proxies.md](proxies.md#model-alternatives).

### Resume with a routing override

```bash
forge session resume parent-session --fresh --proxy litellm-gemini-local
```

`--proxy` performs full proxy resolution (exact proxy_id match or active template lookup) with a healthcheck, then
routes the child session through the resolved proxy. It accepts both proxy IDs and template names.

`--no-proxy` forces direct Anthropic routing, bypassing any inherited proxy.

### Route only subprocesses through a proxy

Use `--subprocess-proxy` when the main session should use Claude Code's direct Anthropic auth, but Forge-spawned
subprocesses such as supervisor, panel, or handoff jobs should use a proxy:

```bash
forge session start my-session --subprocess-proxy openrouter
```

This records `intent.subprocess_proxy` and sets `FORGE_SUBPROCESS_PROXY` for child jobs. It is mutually exclusive with
`--proxy`: use `--proxy` when the main session itself should route through the proxy.

---

## Mid-session toggles (`set` / `reset`)

These commands modify **overrides** in the session file without mutating baseline intent.

Examples:

```bash
forge session set memory.tags '["project:foo","component:auth"]'

# Reset one key
forge session reset memory.tags

# Reset all overrides
forge session reset --all
```

**Policy/TDD enforcement** is managed separately via the Guard CLI, not session set:

```bash
forge guard list                                   # Show available bundles and rules
forge guard enable --bundle tdd                    # Enable TDD enforcement
forge guard enable --bundle tdd --permissive       # Warn instead of block
forge guard enable --bundle coding_standards       # Enable coding standards
forge guard disable                                # Disable all policy
forge guard status                                 # Show current policy state
```

### Ownership boundaries (session vs proxy)

**Session-owned** (you CAN toggle):

- policy enforcement (`forge guard enable/disable`)
- memory behavior (`memory.*`) — see [`handoff.md`](handoff.md) for automatic doc updates
- artifact capture settings
- worktree association
- session metadata

**Proxy-owned** (you CANNOT toggle via session):

- tier→model mapping
- provider/base_url
- reasoning_effort
- thinking_budget_tokens
- temperature/max_tokens defaults

Attempting to set proxy-owned keys is rejected. To change routing defaults, use a different proxy or edit your proxy
overlay. See [proxies.md](proxies.md) for proxy configuration.

---

## Troubleshooting

### “I tried to change the model tier / LLM settings”

Sessions do not control routing or LLM defaults. Choose a different proxy or specify a tier explicitly in the request
model name.

### "I want multi-model A/B/C workflows without worktrees"

It works if sessions are run sequentially.

If you run sessions concurrently and both write code, use `--worktree` to avoid clobbering the working directory.

---

## Advanced

### Template vs Proxy ID

`--proxy` accepts both proxy IDs and template names. Resolution order:

1. Exact proxy_id match (any status)
2. Active template match (healthy/starting only; fails if ambiguous)

All commands (`start`, `resume`, `fork`, `claude start`) use the same `resolve_proxy()` function with full healthcheck.

`--template` and `--base-url` are deprecated hidden aliases for `--proxy` (warn on use).

### Sidecar specifics

- Sidecar sessions use a container-local proxy at `http://localhost:8085`
- `forge session shell [name]` only works for sessions started with `--sidecar`
- The project directory is mounted at `/workspace` inside the container

### Files to inspect (debugging)

| File                                        | Purpose                                     |
| ------------------------------------------- | ------------------------------------------- |
| `.forge/sessions/<name>/forge.session.json` | Session manifest (intent + confirmed state) |
| `~/.forge/sessions/index.json`              | Global session registry (with UUIDs)        |
| `~/.forge/sessions/active.json`             | Runtime live-session registry               |

### Gotchas

| Trap                              | Explanation                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------------- |
| "Session didn't pick up my proxy" | `--proxy` resolves by proxy_id first, then active template match. If ambiguous, use the exact proxy_id. |
| "Hooks lost session identity"     | Hooks resolve via `FORGE_FORK_NAME` -> `FORGE_SESSION` -> UUID lookup (no dir scanning)                 |
| "Can't shell into session"        | `forge session shell` only works for `--sidecar` sessions                                               |
