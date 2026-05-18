# Forge Hooks — Lifecycle + Artifacts Guide

**Status:** Implemented (hooks run as `forge hook <name>`).

Hooks are Forge’s “glue” layer: they observe Claude Code lifecycle events and write **confirmed facts** and
**artifacts** so sessions are inspectable and auditable.

- Canonical architecture: [`docs/design.md`](../design.md)
- Sessions (unit of work): [`sessions.md`](sessions.md)
- Proxies (proxy endpoints): [`proxies.md`](proxies.md)
- Configuration: [`configs.md`](configs.md)
- Policies (guard commands): [`policies.md`](policies.md)
- Workflows (forge workflow): [`workflows.md`](workflows.md)

---

## What are Forge hooks?

Forge hooks are handlers that Claude Code invokes on lifecycle boundaries (SessionStart, PreToolUse, PostToolUse, Stop,
etc.).

Forge’s deployment model is:

- hooks are configured in Claude Code settings
- hooks execute the Forge CLI: `forge hook <name>`
- Forge does **not** install ad-hoc scripts into `.claude/`

### Why this model

- one upgrade surface (upgrade Forge once)
- no per-project dependency ambiguity (Python/venv drift)
- hooks remain testable Python entrypoints

---

## Hook session resolution

Hooks need to identify the current session to read/write confirmed facts. The resolution order is:

1. **`FORGE_FORK_NAME` env var** — set during fork registration (including relaunches)
2. **`FORGE_SESSION` env var** — set by `forge session start` / `forge session resume`
3. **IndexStore UUID lookup** — matches the Claude session UUID against the global index
   (`~/.forge/sessions/index.json`), searching `claude_session_id` only (no previous-ID history)

No CWD-based directory scanning — `FORGE_SESSION` is the authoritative source. Under the 1:1 model, each session has at
most one `claude_session_id`. On `/compact` or `/clear`, the UUID is **overwritten** (not accumulated). The env var
chain typically resolves in step 1 or 2; step 3 is a fallback for edge cases where env vars are not propagated.

---

## Ownership rules (normative)

Hooks are intentionally restricted.

### Hooks CAN do

- write **confirmed facts** into the session file (under `confirmed.*`)
  - Session located via hook resolution: `FORGE_FORK_NAME` -> `FORGE_SESSION` -> UUID lookup
- capture **artifacts** (approved plans, transcripts) into `.forge/artifacts/...`
- apply **session overrides** through direct `%` commands handled by `UserPromptSubmit` (for example `%guard ...`,
  `%cancel-verification`)
- emit machine-readable output for debugging

### Hooks CANNOT do

- mutate session `intent` from lifecycle hooks
- change proxy routing, model selection, or LLM defaults (proxy-owned)
- invent “runtime truth” (runtime truth comes from live proxy introspection in proxy mode)

If you remember one thing: lifecycle hooks are **observers + recorders**. Direct `%` commands are the narrow exception
that may update session overrides.

---

## Installing hooks

Hooks are installed as part of `forge extension enable`, or can be managed separately:

**Recommended:** Use the full installer, which handles hooks along with everything else:

```bash
forge extension enable                           # Auto-detect scope
forge extension enable --scope user              # Personal install → ~/.claude/settings.json
forge extension enable --scope local             # Local install → .claude/settings.local.json
```

**Advanced:** Install hooks only (without commands, agents, etc.):

```bash
forge hook enable --user     # Install hooks to ~/.claude/settings.local.json
forge hook enable --local    # Install hooks to .claude/settings.local.json
forge hook disable --user   # Remove hooks
forge hook disable --local  # Remove hooks
```

> **Note:** `forge hook enable` always writes to `settings.local.json`. `forge extension enable` uses the scope's main
> settings file, which may be `settings.json` or `settings.local.json` depending on scope. Both approaches work — the
> installer is recommended for most users.

---

## Core hooks (what they do)

Forge provides these hook handlers (invoked as `forge hook <name>`):

### session-start

Purpose: establish confirmed runtime context for the session.

Typical responsibilities:

- record `confirmed.claude_session_id`
- record `confirmed.transcript_path`
- record `confirmed.started_with_proxy` for that Claude launch:
  - `{ base_url, proxy_id?, template?, port? }`

> Note: `proxy_id` is a same-machine convenience; `base_url` is the main runtime truth, and `template` is best-effort
> metadata.

> Note: this does not change routing. It records which proxy the session started under.

### plan-write (PostToolUse:Write)

Purpose: detect plan file writes and keep a pointer to the latest plan draft.

- if a file under `.claude/plans/` is written, update `confirmed.latest_plan_path`

### exit-plan-mode (PreToolUse:ExitPlanMode)

Purpose: capture an **approved** plan snapshot on the approval boundary.

- snapshot the approved plan into:
  - `.forge/artifacts/{session_name}/plans/`
- append an entry to `confirmed.artifacts.plans[]`

### stop (Stop)

Purpose: persist a transcript copy at stable boundaries and enqueue deferred work.

- copy the transcript into:
  - `.forge/artifacts/{session_name}/transcripts/{session_id}.jsonl`
- append an entry to `confirmed.artifacts.transcripts[]`
- enqueue search indexing work for `.forge/search-index/`
- enqueue handoff agent marker (if `memory.auto_update.enabled`). See [`handoff.md`](handoff.md).

### pre-compact (PreCompact)

Purpose: capture the full, uncompacted transcript before compaction.

- copies the transcript to `.forge/artifacts/{session_name}/transcripts/{session_id}_pre-compact_{timestamp}.jsonl`
- records the snapshot in `confirmed.compaction.transcript_snapshots[]`
- increments `confirmed.compaction.compact_count`
- always exits 0 (never blocks compaction; `CLAUDE_CODE_AUTO_COMPACT_WINDOW` handles compaction window sizing)

This is the canonical compaction snapshot. The SessionStart rollover (`source="compact"`) serves as fallback for
`/clear` events and defense-in-depth.

### post-compact (PostCompact)

Purpose: record compaction metadata after compaction completes.

- updates `confirmed.compaction.last_compact_at` and `last_compact_type`
- side-effect only (cannot block compaction)

### worktree-create (WorktreeCreate)

Purpose: replace Claude Code's default worktree creation with auto-install of Forge extensions.

- creates a git worktree via `git worktree add`
- best-effort installs Forge extensions (hooks, status line, skills) in the new worktree
- prints the absolute worktree path to stdout (Claude Code reads this)
- exits 1 on failure (worktree creation fails)

**Note:** Once installed, this hook replaces Claude Code's default git worktree behavior and `.worktreeinclude`
handling.

### subagent-stop (SubagentStop)

Purpose: track subagent activity in session confirmed state.

- records `agent_type`, `agent_id`, `agent_transcript_path`, and a truncated `last_assistant_message` preview
- increments `confirmed.subagents.total_count` and `by_type` counters
- observe-only (phase 1) — always exits 0

### policy-check (PreToolUse:Write/Edit)

Purpose: evaluate TDD/Guard policies before file writes.

- enforces policy bundles (TDD, coding standards) when enabled via `forge guard enable`

### read-hygiene (PreToolUse:Read)

Purpose: silently fix Read calls to skill instruction files that include extra parameters.

Models sometimes add `offset`, `limit`, or `pages` when reading skill instruction files, violating the "file_path only"
contract in SKILL.md. This hook detects these calls and uses Claude Code's `updatedInput` capability to strip the extra
parameters before the Read executes — zero token cost, no retry needed.

**Scope:** Only targets instruction files matching `{mode}.md` or `{mode}-{family}.md` (e.g., `code.md`,
`docs-openai.md`). Does not affect QA checklists, report templates, or other skill resources.

### user-prompt-submit (UserPromptSubmit)

Purpose: dispatch direct user commands (`%` commands). See [In-session commands](#in-session-commands--commands) below
for the full list.

---

## In-session commands (% commands)

Type these directly in the Claude prompt to interact with Forge without switching to a terminal. Commands starting with
`%` are intercepted by the `UserPromptSubmit` hook and handled by Forge.

| Command                                     | Effect                                                    |
| ------------------------------------------- | --------------------------------------------------------- |
| `%h` / `%help`                              | Show command help                                         |
| `%config`                                   | Show effective runtime config (read-only)                 |
| `%session list`                             | List sessions                                             |
| `%plan`                                     | Show the current session's recorded plan file path        |
| `%proxy list`                               | List proxies (read-only)                                  |
| `%proxy show <id>`                          | Show proxy details (read-only)                            |
| `%guard status`                             | Show policy config and state                              |
| `%guard enable --bundle tdd [--permissive]` | Enable policy enforcement                                 |
| `%guard disable`                            | Disable all policies                                      |
| `%guard check [--staged] [--bundle <name>]` | Evaluate git diff against policies (diagnostic, not gate) |
| `%cancel-verification`                      | Bypass active verification loop                           |

> **Note:** `%guard enable/disable` applies session overrides that persist until changed or reset. The CLI
> `forge guard enable/disable` mutates session intent. `%guard check` is read-only — it evaluates but doesn't change
> enforcement state.

> **Note:** `%` commands only work in interactive Claude sessions. They do NOT fire in `claude --print` mode.

---

## Artifacts: where they go

Artifacts are stored under the **repo root** (not the worktree root) so they remain consolidated:

- `.forge/artifacts/{session_name}/plans/`
- `.forge/artifacts/{session_name}/transcripts/`

Paths stored in the session file should be repo-root-relative for portability.

---

## Debugging hooks

### “Hooks aren’t firing”

Checklist:

- confirm hooks are installed in the scope you’re using
- confirm `forge` is on PATH in the environment Claude Code uses to run hooks
- check Claude Code hook logs (or Forge’s emitted JSON output)

### "Hooks fired but session file didn't update"

- hooks only write `confirmed.*`
- confirm `FORGE_SESSION` env var is set (should be set by `forge session start` / `resume`)
- if env var is missing, confirm the session exists in the IndexStore (`~/.forge/sessions/index.json`)
- confirm the session manifest exists at `.forge/sessions/<name>/forge.session.json`

### "Hooks changed my model / routing"

They shouldn't. If this appears to happen:

- verify you didn't change `ANTHROPIC_BASE_URL` / proxy base URL between runs
- verify which proxy the session started under (`confirmed.started_with_proxy`)
- in proxy mode, compare against live runtime truth (`GET /`)

---

## Advanced

### Hook resolution mechanism

See [Hook session resolution](#hook-session-resolution) for the four-step resolution chain (`FORGE_FORK_NAME` ->
`FORGE_SESSION` -> UUID lookup -> dir scan).

### Hook command group

All hooks are under `forge hook ...` (group name `hook`, not `hooks`):

```bash
forge hook session-start   # SessionStart handler
forge hook stop            # Stop handler
forge hook policy-check    # PreToolUse:Write/Edit handler
forge hook enable --local # Install to .claude/settings.local.json
```

### Files to inspect (debugging)

| File                                        | Purpose                                                      |
| ------------------------------------------- | ------------------------------------------------------------ |
| `.forge/sessions/<name>/forge.session.json` | Session manifest with `confirmed.*` facts                    |
| `~/.forge/sessions/index.json`              | Global session index (UUID lookup)                           |
| Claude settings file for your scope         | Hook registration (`settings.json` or `settings.local.json`) |
| `.forge/artifacts/`                         | Captured plans and transcripts                               |

### Gotchas

| Trap                    | Explanation                                                                                                    |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- |
| "FORGE_SESSION not set" | Hooks fall back through `FORGE_FORK_NAME` and UUID lookup; check `~/.forge/sessions/index.json`                |
| "Hooks not firing"      | Verify `forge` is on PATH in Claude Code's environment                                                         |
| "Wrong settings file"   | `forge hook enable` targets `settings.local.json`; `forge extension enable` uses scope-specific settings files |
