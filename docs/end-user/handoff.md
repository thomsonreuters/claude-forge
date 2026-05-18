# Forge Handoff Agent — Automatic Memory Docs Guide

Shadow/propose mode and topic strategies available.

The handoff agent is queued automatically when a session ends and runs on the next Forge CLI startup to update
designated project documents based on what happened in the session. It reads the session transcript and writes updates
to pre-existing files.

- Canonical architecture: [`docs/design.md` §5.6](../design.md)
- Sessions (unit of work): [`sessions.md`](sessions.md)
- Hooks (lifecycle events): [`hooks.md`](hooks.md)

---

## What the handoff agent does

After a session stops, the Stop hook enqueues a work marker. On next CLI startup, Forge spawns a headless `claude -p`
subprocess that:

1. Reads the session transcript
2. Reads each designated document
3. Applies per-doc strategy instructions (add completed tasks, record errors, propose changes)
4. Writes minimal updates to each file

The agent is **retrospective** — it sees the full session before deciding what to capture. This produces higher
signal-to-noise than incremental note-taking during a session.

---

## Two operating modes

### Mode 1: Direct update (agent is author)

The agent edits designated docs in-place. Use for operational documents the agent has authority to maintain.

```yaml
designated_docs:
  - path: docs/checklist.md
    strategy: checklist
  - path: docs/changelog.md
    strategy: changelog
```

### Mode 2: Shadow/propose (agent is advisor)

The agent writes suggestions to a **shadow file** for human review, reading the official document first to avoid
redundant proposals. Use for standards and guidelines where human curation matters.

```yaml
designated_docs:
  - path: .forge/memory/suggested_standards.md
    strategy: suggested
    shadows: docs/developer/coding-standards.md
```

The shadow file contains `- [ ]` checkboxes with rationale. The human reviews and merges what's valuable into the
official doc. Already-merged items are self-pruned on the next run.

---

## Configuration

The handoff agent is configured in the session manifest under `intent.memory`:

```yaml
# In forge.session.json (intent section)
memory:
  auto_update:
    enabled: true
    mode: augment           # "augment" (write updates) or "review-only" (dry run)
    min_turns: 5            # Skip short sessions (below this threshold)
    proxy: null             # Optional: route agent through specific proxy
  designated_docs:
    - path: docs/checklist.md
      strategy: checklist
    - path: docs/changelog.md
      strategy: changelog
    - path: .forge/memory/debugging.md
      strategy: debugging
    - path: .forge/memory/patterns.md
      strategy: patterns
    - path: .forge/memory/suggested_standards.md
      strategy: suggested
      shadows: docs/developer/coding-standards.md
```

### Setting up via CLI

```bash
# Enable handoff agent
forge session set memory.auto_update.enabled true

# Configure min_turns threshold
forge session set memory.auto_update.min_turns 5

# Use review-only mode (prints suggestions, doesn't modify files)
forge session set memory.auto_update.mode review-only
```

> `designated_docs` is a list. The `forge session set` CLI accepts the full JSON array, but list overrides replace the
> entire list:
>
> `forge session set memory.designated_docs '[{"path":"docs/checklist.md","strategy":"checklist"}]'`

---

## Strategies

Each designated doc has a strategy that controls how the agent updates it.

### Direct update strategies (Mode 1)

| Strategy        | What the agent does                                              |
| --------------- | ---------------------------------------------------------------- |
| `project-state` | Update current focus, active work, decisions, handoff notes      |
| `checklist`     | Mark completed tasks `[x]`, add newly discovered tasks           |
| `changelog`     | Add accomplishments not already recorded, follow existing format |
| `debugging`     | Record error causes, solutions, and workarounds grouped by topic |
| `patterns`      | Record architecture patterns, conventions, and code idioms       |
| `generic`       | Add any new information missing from the file (default fallback) |

All direct strategies are **additive** — the agent does not remove, rewrite, or restructure existing content.

### Shadow strategy (Mode 2)

| Strategy    | What the agent does                                                             |
| ----------- | ------------------------------------------------------------------------------- |
| `suggested` | Propose additions as `- [ ]` checkboxes with rationale; self-prune merged items |

The `suggested` strategy **requires** the `shadows` field (path to the official document). The agent reads the official
doc first, then proposes only what's missing.

---

## File requirements

**All designated docs must already exist.** The agent does not create files — it only updates existing ones.

Before enabling the handoff agent, seed the files you want maintained:

```bash
# Direct update docs
echo "# Implementation Checklist" > docs/checklist.md
echo "# Change Log" > docs/changelog.md
mkdir -p .forge/memory
echo "# Debugging Notes" > .forge/memory/debugging.md
echo "# Architecture Patterns" > .forge/memory/patterns.md

# Shadow docs (both the shadow AND official must exist)
echo "# Suggested Standards" > .forge/memory/suggested_standards.md
# docs/developer/coding-standards.md should already exist
```

Missing files are silently skipped. For shadow docs, both the shadow file and the official doc must exist.

---

## Path resolution

Designated doc paths are **forge-root-relative**. When working in a git worktree, the agent edits the correct branch's
content.

Transcript paths are stored as **forge-root-relative** paths and resolved against `forge_root` at runtime. Transcripts
are artifacts stored at `<forge_root>/.forge/artifacts/`.

| Path type        | Resolves against | Why                                |
| ---------------- | ---------------- | ---------------------------------- |
| `doc.path`       | `forge_root`     | Edits branch-specific content      |
| `doc.shadows`    | `forge_root`     | Reads branch-specific official doc |
| `transcript_rel` | `forge_root`     | Artifacts scoped to Forge project  |

The `claude -p` subprocess runs with `cwd=forge_root`.

---

## Execution flow

```
Session stops
  → Stop hook captures transcript to .forge/artifacts/
  → Stop hook enqueues "handoff" work marker
  → (session ends)

Next CLI startup (any forge command)
  → Work queue processes pending markers
  → Handoff handler spawns detached background process:
      forge handoff run --session-name <name> --worktree-path <path> --transcript-rel <rel>

Background process:
  → Reads session manifest → compute effective intent
  → Checks: enabled? min_turns met? claude available? mode valid?
  → Validates designated_docs (path safety, strategy consistency, file existence)
  → Builds multi-doc prompt with per-doc strategy instructions
  → Runs: claude -p (stdin=prompt, cwd=forge_root, timeout=5min)
```

### Proxy routing

The agent inherits the session's proxy by default (same model routing). Override with `proxy`:

```yaml
auto_update:
  enabled: true
  proxy: openrouter-gemini-flash   # Use a cheaper proxy for summarization
```

Priority chain: `proxy` -> `confirmed.started_with_proxy` -> `ANTHROPIC_BASE_URL` env -> Anthropic direct.

---

## Validation rules

The agent validates designated docs before processing:

| Rule              | Rejected if                                           |
| ----------------- | ----------------------------------------------------- |
| Absolute path     | `doc.path` or `doc.shadows` is absolute               |
| Path traversal    | `../` escapes worktree directory                      |
| Unsafe characters | Backticks, newlines, control chars (prompt injection) |
| Strategy mismatch | `strategy=suggested` without `shadows`                |
| Reverse mismatch  | `shadows` set with non-`suggested` strategy           |
| Self-shadowing    | `doc.path == doc.shadows`                             |
| Empty shadows     | `shadows=""` (must be non-empty or null)              |

Invalid docs are skipped with a log warning. If all docs are invalid or missing, the agent exits cleanly (not an error).

The transcript path is also validated (same safety checks) since it comes from CLI args / work queue markers.

---

## Troubleshooting

### "Handoff agent didn't run"

Checklist:

- `memory.auto_update.enabled` must be `true` in effective intent
- Session must have ≥ `min_turns` conversation turns (default: 5)
- `claude` CLI must be on PATH
- `designated_docs` must be non-empty
- At least one doc must exist on disk

### "File wasn't updated"

- The file must exist before the agent runs (no file creation)
- For shadow docs, the official doc (`shadows` target) must also exist
- Check the strategy — does it match what you expect the agent to do?
- Try `mode: review-only` to see what the agent would change without modifying files

### "Wrong file was updated" (path issues)

- Designated docs resolve against `forge_root`
- If working in a worktree, verify the file exists at the Forge project root (not just the main repo)
- Check `forge session show <name>` to see the forge_root path

### "Agent timed out"

Default timeout is 5 minutes. For large transcripts or many docs, the agent may need more time. Set via CLI:

```bash
forge handoff run --session-name <name> --worktree-path <path> --transcript-rel <rel> --timeout 600
```

---

## Files to inspect (debugging)

| File                                        | Purpose                                         |
| ------------------------------------------- | ----------------------------------------------- |
| `.forge/sessions/<name>/forge.session.json` | Session manifest (`intent.memory` config)       |
| `.forge/artifacts/<name>/transcripts/`      | Captured transcripts (agent input)              |
| `~/.forge/pending-work/`                    | Work queue markers (handoff-\<session_id>.json) |
| `~/.forge/pending-work/failed/`             | Poison markers (exceeded retry limit)           |

### Gotchas

| Trap                                  | Explanation                                                               |
| ------------------------------------- | ------------------------------------------------------------------------- |
| "Handoff enabled but nothing happens" | `designated_docs` is empty — agent has nothing to update                  |
| "Shadow doc not updating"             | Official doc (`shadows` target) must exist on disk                        |
| "Agent uses wrong model"              | Inherits session proxy by default; set `proxy` for explicit routing       |
| "File created by agent"               | Agent never creates files — seed them first                               |
| "Stale suggestions in shadow doc"     | Agent self-prunes merged items; run again after merging into official doc |
