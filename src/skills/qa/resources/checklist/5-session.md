<!-- prereq: 0.3 -->

## 5. Session Management

### 5.1 Start a Session

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

# Clean up from previous runs
forge session delete test-session-1 --force 2>/dev/null || true

# Start a new session
forge session start test-session-1 --no-launch

# Verify session created
ls -la .forge/sessions/
cat .forge/sessions/test-session-1/forge.session.json | jq '.'
```

- [ ] Session directory created at `.forge/sessions/test-session-1/`
- [ ] `forge.session.json` contains `intent` section
- [ ] `--no-launch` prevents Claude from opening (useful for testing)

### 5.2 List Sessions

<!-- auto -->

```bash
# List all sessions
forge session list
```

- [ ] Shows `test-session-1` with status
- [ ] Shows session directory and last-used timestamp

### 5.3 Show Session Details

<!-- auto -->

```bash
# Show session details
forge session show test-session-1
```

- [ ] Shows intent, overrides, confirmed sections
- [ ] Shows proxy info if running with proxy

### 5.4 Set Session Overrides

<!-- auto -->

```bash
# Set a mid-session override
forge session set memory.auto_update.enabled true --session test-session-1

# Verify override applied
cat .forge/sessions/test-session-1/forge.session.json | jq '.overrides'
```

- [ ] Override written to `overrides` section
- [ ] Original intent unchanged

### 5.5 Reset Overrides

<!-- auto -->

```bash
# Reset overrides to intent
forge session reset --session test-session-1

# Verify reset
cat .forge/sessions/test-session-1/forge.session.json | jq '.overrides'
```

- [ ] Overrides section cleared or empty

### 5.6 Fork a Session (default, same directory)

<!-- prereq: 2.4, 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, start the parent session routed through the proxy provisioned in 4.2 (Claude will launch --
interact briefly, then exit with `/exit`). Then fork it **without `--worktree`** (the default). The fork stays in the
same directory, so Claude's `--resume --fork-session` finds the parent conversation and carries it over. Ask "where were
we?" to confirm the conversation context carried over, then exit (`/exit`).

```
# Clean up from previous runs
forge session delete test-session-parent --force 2>/dev/null || true
forge session delete test-session-forked --force 2>/dev/null || true

# Start the parent session through the proxy provisioned in 4.2.
# Interact briefly ("hello"), then exit (/exit).
forge session start test-session-parent --proxy litellm-openai

# Fork the parent session (default: same directory, no worktree).
# Claude should resume the conversation via --fork-session.
# Disable auto-memory so "where were we?" tests Forge handoff, not CC memory.
# Ask "where were we?" to confirm, then exit (/exit).
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 forge session fork test-session-parent --name test-session-forked

# Verify fork lives in the same directory as parent
forge session show test-session-forked
jq '{is_fork, parent_session, worktree: (.worktree | {path, is_worktree}), confirmed: (.confirmed | {claude_session_id})}' \
  /workspace/.forge/sessions/test-session-forked/forge.session.json
```

- [ ] Forked session created in same directory (`/workspace`)
- [ ] `forge session show` reports type as Fork
- [ ] Claude conversation carries over (asking "where were we?" reflects parent context)
- [ ] No `Worktree:` line in fork output (no git worktree created)
- [ ] Manifest at `/workspace/.forge/sessions/test-session-forked/` (not a separate worktree dir)
- [ ] Manifest has `is_fork: true`, `parent_session` pointing to parent, `is_worktree: false`
- [ ] `confirmed.claude_session_id` is populated after fork

### 5.7 Fork a Session with Worktree (`--worktree`)

<!-- prereq: 2.4, 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

Fork the parent session again, this time with `--worktree` for code isolation. The fork gets its own git worktree and
branch. Because conversations are project-scoped, the fork starts a fresh Claude session in the new worktree and
automatically injects a parent handoff context file. Ask "where were we?" to confirm the parent context is present, then
exit (`/exit`).

```
# Clean up from previous runs
forge session delete test-session-forked-wt --force 2>/dev/null || true
git worktree remove /workspace-test-session-forked-wt --force 2>/dev/null || true
git branch -D test-session-forked-wt 2>/dev/null || true

# Fork with --worktree (creates isolated worktree + branch).
# Starts fresh Claude with parent handoff context (no --resume attempt).
# Disable auto-memory so "where were we?" tests Forge handoff, not CC memory.
# Ask "where were we?" to confirm parent context, then exit (/exit).
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 forge session fork test-session-parent --name test-session-forked-wt --worktree --extensions

# Verify fork
forge session show test-session-forked-wt
jq '{is_fork, parent_session, worktree: (.worktree | {path, is_worktree}), confirmed: (.confirmed | {claude_session_id})}' \
  /workspace-test-session-forked-wt/.forge/sessions/test-session-forked-wt/forge.session.json
cat /workspace-test-session-forked-wt/.forge/prev_sessions/test-session-parent.md
```

- [ ] Worktree fork created at `/workspace-test-session-forked-wt`
- [ ] `forge session show` reports type as Fork with worktree info
- [ ] Fork output shows `Extensions:` line confirming auto-install in worktree
- [ ] Fork output shows `Context:` line with parent handoff file
- [ ] Asking "where were we?" reflects parent context
- [ ] Manifest has `is_fork: true`, `parent_session`, `is_worktree: true`
- [ ] `confirmed.claude_session_id` is populated
- [ ] Parent handoff file exists at `/workspace-test-session-forked-wt/.forge/prev_sessions/test-session-parent.md`

### 5.8 Incognito Session

<!-- requires: api_key -->

<!-- human:guided -->

Incognito sessions auto-delete on exit, so `--incognito` requires launching Claude (`--no-launch` is mutually
exclusive). In the **container shell**, launch an incognito session, interact briefly, then exit.

```
# Clean up from previous runs
forge session delete test-incognito --force 2>/dev/null || true

# Launch an incognito session (auto-deletes on exit).
# Say "hello", then exit with /exit.
forge session incognito test-incognito

# After exiting Claude, verify auto-cleanup removed the session
forge session list
# Expected: test-incognito should NOT appear (auto-deleted on exit)
```

- [ ] Incognito session launches successfully
- [ ] Session auto-deleted after exit (not in `forge session list`)
- [ ] No `.forge/sessions/test-incognito/` directory remains

### 5.9 Delete a Session

<!-- auto -->

```bash
# Clean up from previous runs
forge session delete test-session-delete-me --force 2>/dev/null || true

# Create a disposable session to delete
forge session start test-session-delete-me --no-launch

# Delete a test session (non-interactive)
forge session delete test-session-delete-me --force

# Verify deletion
forge session list
```

- [ ] Session removed from listing
- [ ] Session directory removed

Ref-count delete guard: verify that deleting a co-resident session preserves the shared worktree.

```bash
# Create a worktree session (owns the worktree)
forge session delete test-refcount-owner --force 2>/dev/null || true
forge session delete test-refcount-guest --force 2>/dev/null || true
git worktree remove /workspace-test-refcount-owner --force 2>/dev/null || true
git branch -D test-refcount-owner 2>/dev/null || true

forge session start test-refcount-owner --worktree --no-launch
WORKTREE_PATH=$(jq -r '.sessions["test-refcount-owner"].worktree_path' ~/.forge/sessions/index.json)

# Seed a fake UUID so fork's confirmed.claude_session_id guard passes
OWNER_JSON="$WORKTREE_PATH/.forge/sessions/test-refcount-owner/forge.session.json"
jq '.confirmed.claude_session_id = "fixture-refcount"' "$OWNER_JSON" > /tmp/rc.json && mv /tmp/rc.json "$OWNER_JSON"

# Fork into the same worktree (guest, does not own)
forge session fork test-refcount-owner --name test-refcount-guest --into "$WORKTREE_PATH" --no-launch

# Delete the guest — worktree must be preserved
forge session delete test-refcount-guest --force

# Verify worktree still exists
test -d "$WORKTREE_PATH" && echo "WORKTREE_PRESERVED=true" || echo "WORKTREE_PRESERVED=false"
forge session list | grep test-refcount-owner
```

- [ ] Guest session deleted successfully
- [ ] Worktree directory preserved (owner session still holds a reference)
- [ ] Owner session still listed and functional

### 5.10 Worktree Session (Isolation)

<!-- auto -->

```bash
# Clean up from previous runs
forge session delete test-session-worktree --force 2>/dev/null || true

# Create a session with a git worktree (no Claude launch)
forge session start test-session-worktree --worktree --no-launch

# Worktree sessions store manifests in the worktree dir, not the main workspace.
# Read the worktree path from the global index.
WORKTREE_PATH=$(jq -r '.sessions["test-session-worktree"].worktree_path' ~/.forge/sessions/index.json)
MANIFEST="$WORKTREE_PATH/.forge/sessions/test-session-worktree/forge.session.json"

# Verify worktree recorded in manifest
cat "$MANIFEST" | jq '.worktree'

# Verify the worktree path exists on disk
test -d "$WORKTREE_PATH" && echo "WORKTREE_EXISTS=true" || echo "WORKTREE_EXISTS=false"

# Verify it is marked as a worktree session
cat "$MANIFEST" | jq '.worktree.is_worktree'
```

- [ ] Worktree session created
- [ ] Manifest contains worktree path + branch
- [ ] Worktree path exists on disk
- [ ] `worktree.is_worktree` is `true`

### 5.11 System Prompt Generation

<!-- requires: api_key -->

<!-- human:guided -->

System prompts are injected at launch time (`--system-prompt` is mutually exclusive with `--no-launch`). In the
**container shell**, launch a session with a custom system prompt, verify the generated file, then exit.

```
# Clean up from previous runs
forge session delete test-session-system-prompt --force 2>/dev/null || true

# Launch a session with an inline system prompt.
# Say "hello", then exit with /exit.
forge session start test-session-system-prompt --system-prompt "FORGE_MANUAL_TEST_SYSTEM_PROMPT"

# After exiting Claude, verify the generated file
test -f .claude/forge.system-prompt.generated.md && echo "FILE_EXISTS=true" || echo "FILE_EXISTS=false"
grep -c "FORGE_MANUAL_TEST_SYSTEM_PROMPT" .claude/forge.system-prompt.generated.md
```

- [ ] Generated system prompt file created at `.claude/forge.system-prompt.generated.md`
- [ ] Generated file contains the provided prompt text

### 5.12 Session Show

<!-- auto -->

```bash
# Show session by name
forge session show test-session-1

# Show session via FORGE_SESSION env var
FORGE_SESSION=test-session-1 forge session show

# No name and no env var -> guidance message
forge session show
```

- [ ] `forge session show <name>` displays session details
- [ ] `forge session show` without name or env var shows guidance message

### 5.13 Fork with `--strategy` (Context Assembly)

<!-- prereq: 5.1 -->

<!-- auto -->

Verify that `--strategy` controls handoff content density on worktree forks.

```bash
# Setup: create parent with a mock transcript for handoff generation
forge session delete test-strat-parent --force 2>/dev/null || true
forge session delete test-fork-strat-min --force 2>/dev/null || true
forge session delete test-fork-strat-struct --force 2>/dev/null || true
git worktree remove /workspace-test-fork-strat-min --force 2>/dev/null || true
git worktree remove /workspace-test-fork-strat-struct --force 2>/dev/null || true
git branch -D test-fork-strat-min 2>/dev/null || true
git branch -D test-fork-strat-struct 2>/dev/null || true

forge session start test-strat-parent --no-launch

# Inject a fixture transcript so handoff has content to assemble
PARENT_JSON=".forge/sessions/test-strat-parent/forge.session.json"
TDIR=".forge/artifacts/test-strat-parent/transcripts"
mkdir -p "$TDIR"
cat > "$TDIR/fixture.jsonl" << 'JSONL'
{"requestId":"r1","timestamp":"2026-01-01T00:00:00Z","message":{"role":"user","content":[{"type":"text","text":"Create a hello function"}]}}
{"requestId":"r1","timestamp":"2026-01-01T00:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"I will create a hello function."},{"type":"tool_use","id":"t1","name":"Write","input":{"file_path":"hello.py","content":"def hello(): return 'hi'"}}]}}
{"requestId":"r1","timestamp":"2026-01-01T00:00:02Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"OK"}]}}
JSONL
jq --arg tp "$PWD/$TDIR/fixture.jsonl" \
  '.confirmed.transcript_path = $tp | .confirmed.claude_session_id = "fixture-strat"' \
  "$PARENT_JSON" > /tmp/s.json && mv /tmp/s.json "$PARENT_JSON"

# Fork with --strategy minimal
forge session fork test-strat-parent --name test-fork-strat-min --worktree --strategy minimal --no-launch
HANDOFF_MIN="/workspace-test-fork-strat-min/.forge/prev_sessions/test-strat-parent.md"
test -f "$HANDOFF_MIN" && echo "MIN_HANDOFF=true" || echo "MIN_HANDOFF=false"
wc -l < "$HANDOFF_MIN"

# Fork with --strategy structured
forge session fork test-strat-parent --name test-fork-strat-struct --worktree --strategy structured --no-launch
HANDOFF_STRUCT="/workspace-test-fork-strat-struct/.forge/prev_sessions/test-strat-parent.md"
test -f "$HANDOFF_STRUCT" && echo "STRUCT_HANDOFF=true" || echo "STRUCT_HANDOFF=false"
wc -l < "$HANDOFF_STRUCT"
```

- [ ] Minimal handoff file created at expected path
- [ ] Structured handoff file created at expected path
- [ ] Structured handoff contains more content than minimal (higher line count)

### 5.14 Fork with `--inline-plan`

<!-- prereq: 5.1 -->

<!-- auto -->

Verify that `--inline-plan` inlines approved plan content in the handoff context file.

```bash
# Setup: create parent with a mock plan via confirmed.latest_plan_path
forge session delete test-plan-parent --force 2>/dev/null || true
forge session delete test-fork-plan --force 2>/dev/null || true
git worktree remove /workspace-test-fork-plan --force 2>/dev/null || true
git branch -D test-fork-plan 2>/dev/null || true

forge session start test-plan-parent --no-launch

# Create a mock plan file and wire it into the manifest
mkdir -p .claude/plans
cat > .claude/plans/test-plan.md << 'PLAN'
# Approved Plan

1. Create `src/demo.py` with a greet function
2. Add unit test in `tests/test_demo.py`
3. Run tests to verify
PLAN

PARENT_JSON=".forge/sessions/test-plan-parent/forge.session.json"
jq '.confirmed.latest_plan_path = ".claude/plans/test-plan.md" | .confirmed.claude_session_id = "fixture-plan"' \
  "$PARENT_JSON" > /tmp/p.json && mv /tmp/p.json "$PARENT_JSON"

# Fork with --inline-plan (plan content should appear in handoff)
forge session fork test-plan-parent --name test-fork-plan --worktree --inline-plan --no-launch

HANDOFF="/workspace-test-fork-plan/.forge/prev_sessions/test-plan-parent.md"
test -f "$HANDOFF" && echo "HANDOFF_EXISTS=true" || echo "HANDOFF_EXISTS=false"
grep -c "Approved Plan" "$HANDOFF"
grep -c "greet function" "$HANDOFF"
```

- [ ] Handoff file created in worktree fork
- [ ] Handoff contains plan heading ("Approved Plan")
- [ ] Handoff contains plan details ("greet function")

### 5.15 Fork `--into` (Existing Worktree)

<!-- prereq: 2.4, 4.2, 5.6 -->

<!-- requires: api_key -->

<!-- human:guided -->

Fork a session into an existing non-main worktree using `--into`. Unlike `--worktree` (which creates a new worktree),
`--into` reuses an existing one and marks the session as non-owning — the worktree is preserved when the session is
deleted. In the **container shell**, create a target worktree, fork into it, and interact briefly with Claude to confirm
parent context, then exit (`/exit`).

```
# Clean up from previous runs
forge session delete test-fork-into --force 2>/dev/null || true
git worktree remove /workspace-test-into-target --force 2>/dev/null || true
git branch -D test-into-target 2>/dev/null || true

# Create a target worktree (simulating an existing feature branch)
git worktree add /workspace-test-into-target -b test-into-target

# Fork the parent session into the existing worktree.
# Claude will launch with parent handoff context.
# Disable auto-memory so "where were we?" tests Forge handoff, not CC memory.
# Ask "where were we?" to confirm parent context, then exit (/exit).
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 forge session fork test-session-parent --name test-fork-into --into /workspace-test-into-target

# Verify fork
forge session show test-fork-into
jq '{is_fork, parent_session, worktree: (.worktree | {path, is_worktree, owns_worktree})}' \
  /workspace-test-into-target/.forge/sessions/test-fork-into/forge.session.json
```

- [ ] Fork created in existing worktree at `/workspace-test-into-target`
- [ ] `forge session show` reports type as Fork with worktree info
- [ ] Manifest has `is_fork: true`, `is_worktree: true`, `owns_worktree: false`
- [ ] Parent handoff context file present in target worktree
- [ ] Asking "where were we?" reflects parent context from 5.6

### 5.16 Subprocess Proxy (Direct + Proxied Subprocesses)

<!-- prereq: 2.4, 4.2 -->

<!-- auto -->

```bash
# Clean up from previous runs
forge session delete test-subprocess-proxy --force 2>/dev/null || true

# Create a session with --subprocess-proxy (direct main, proxied subprocesses)
forge session start test-subprocess-proxy --subprocess-proxy litellm-gemini --no-launch

# Verify intent recorded in session manifest
jq '.intent.subprocess_proxy' .forge/sessions/test-subprocess-proxy/forge.session.json

# Verify session is direct mode (no proxy routing for main session)
jq '{proxy: .intent.proxy, started_with_proxy: .confirmed.started_with_proxy}' \
  .forge/sessions/test-subprocess-proxy/forge.session.json
```

- [ ] Session created with `--subprocess-proxy` flag (exit 0)
- [ ] `intent.subprocess_proxy` is `"litellm-gemini"` in session manifest
- [ ] `intent.proxy` is null (main session is direct mode)
- [ ] `confirmed.started_with_proxy` is null (no proxy for main session)

### 5.17 Subprocess Proxy Mutual Exclusivity

<!-- auto -->

```bash
# Try combining --subprocess-proxy with --proxy (should error)
forge session start test-invalid-subproxy \
  --subprocess-proxy litellm-gemini --proxy litellm-openai --no-launch 2>&1
echo "EXIT=$?"
```

- [ ] Error message about mutual exclusivity of `--subprocess-proxy` and `--proxy`
- [ ] Exit code is non-zero

### 5.18 Subprocess Proxy Inheritance (Fork)

<!-- prereq: 5.16 -->

<!-- auto -->

```bash
# Seed confirmed.claude_session_id so fork guard passes
PARENT_JSON=".forge/sessions/test-subprocess-proxy/forge.session.json"
jq '.confirmed.claude_session_id = "fixture-subproxy"' "$PARENT_JSON" > /tmp/sp.json \
  && mv /tmp/sp.json "$PARENT_JSON"

# Fork the session
forge session delete test-fork-subproxy --force 2>/dev/null || true
forge session fork test-subprocess-proxy --name test-fork-subproxy --no-launch

# Verify forked session inherits subprocess_proxy
jq '.intent.subprocess_proxy' .forge/sessions/test-fork-subproxy/forge.session.json

# Clean up
forge session delete test-subprocess-proxy --force 2>/dev/null || true
forge session delete test-fork-subproxy --force 2>/dev/null || true
```

- [ ] Forked session inherits `subprocess_proxy` from parent
- [ ] Child `intent.subprocess_proxy` is `"litellm-gemini"`
- [ ] Both test sessions cleaned up

---
