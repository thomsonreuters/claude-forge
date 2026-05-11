<!-- prereq: 0.3, 2.1, 5.1 -->

## 6. Hooks Testing

Note: Install hooks via `forge extension enable` (full) or `forge hook enable` (hooks-only; writes to
`settings.local.json`).

### 6.1 Verify Hook Configuration

<!-- auto -->

```bash
# Check hooks in whichever settings file was used during enable.
# `forge extension enable` writes to the scope's settings file;
# `forge hook enable --local` always writes to settings.local.json.
cat $CLAUDE_HOME/settings.local.json | jq '.hooks' 2>/dev/null || \
  cat $CLAUDE_HOME/settings.json | jq '.hooks'
```

- [ ] `PreToolUse` hooks configured (policy-check)
- [ ] `PostToolUse` hooks configured (plan-write)
- [ ] `Stop` hook configured
- [ ] `UserPromptSubmit` hook configured
- [ ] `SessionStart` hook configured

### 6.2 Install Hooks Only (Optional)

<!-- auto -->

```bash
# Install hooks only (no commands/skills)
forge hook enable --user
forge hook enable --local
```

- [ ] Hooks-only install works (writes to settings.local.json)

### 6.3 Test Hook Manually

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

# Test the status-line command with the real stdin contract.
BASE_URL=$(jq -r '.intent.proxy.base_url // empty' .forge/sessions/test-session-1/forge.session.json)
mkdir -p .forge/walkthrough
cat > .forge/walkthrough/status-line-transcript.jsonl <<'EOF'
{"requestId":"req-001","message":{"role":"user","content":[{"type":"text","text":"Read the config file."}]}}
{"requestId":"req-001","message":{"role":"assistant","content":[{"type":"text","text":"I'll inspect it."},{"type":"tool_use","id":"tool-001","name":"Read","input":{"file_path":"/workspace/config.yaml"}}]}}
{"requestId":"req-001","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool-001","content":"timeout: 10"}]}}
{"requestId":"req-002","message":{"role":"user","content":[{"type":"text","text":"Update the timeout and run tests."}]}}
{"requestId":"req-002","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool-002","name":"Edit","input":{"file_path":"/workspace/config.yaml"}},{"type":"tool_use","id":"tool-003","name":"Bash","input":{"command":"uv run pytest"}}]}}
EOF
STATUS_INPUT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  --arg transcript "$FORGE_TEST_REPO/.forge/walkthrough/status-line-transcript.jsonl" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Opus 4.6"},
    transcript_path: $transcript
  }')

echo "$STATUS_INPUT" | FORGE_SESSION=test-session-1 ANTHROPIC_BASE_URL="$BASE_URL" forge status-line

# Test user-prompt-submit with a %help command
echo '{"prompt": "%help"}' | FORGE_SESSION=test-session-1 forge hook user-prompt-submit
```

- [ ] Status line outputs session/model info (and proxy info if available)
- [ ] `%help` returns help text (or decision payload)

### 6.4 Smoke Test SessionStart Hook

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

# Use the candidate UUID already stored in the session manifest
SESSION_ID=$(cat .forge/sessions/test-session-1/forge.session.json | jq -r '.confirmed.claude_session_id')

echo "{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\".forge/walkthrough/mock-transcript.jsonl\",\"source\":\"startup\"}" | FORGE_SESSION=test-session-1 forge hook session-start

# Verify manifest updated
cat .forge/sessions/test-session-1/forge.session.json | jq '.confirmed.transcript_path'
```

- [ ] Hook returns JSON success
- [ ] Manifest has `confirmed.transcript_path` set to the provided value

### 6.5 Smoke Test plan-write Hook (Plan Path Recorded)

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

SESSION_ID=$(cat .forge/sessions/test-session-1/forge.session.json | jq -r '.confirmed.claude_session_id')

mkdir -p .claude/plans
echo "# Test Plan" > .claude/plans/test-plan.md

echo "{\"hook_event_name\":\"PostToolUse\",\"tool_input\":{\"file_path\":\".claude/plans/test-plan.md\"},\"session_id\":\"$SESSION_ID\"}" | FORGE_SESSION=test-session-1 forge hook plan-write

# Verify manifest recorded latest plan path
cat .forge/sessions/test-session-1/forge.session.json | jq '.confirmed.latest_plan_path'
```

- [ ] Hook returns `action: recorded`
- [ ] Manifest has `confirmed.latest_plan_path` pointing to `.claude/plans/test-plan.md`

### 6.6 Smoke Test exit-plan-mode Hook (Approved Snapshot)

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

SESSION_ID=$(cat .forge/sessions/test-session-1/forge.session.json | jq -r '.confirmed.claude_session_id')

echo "{\"hook_event_name\":\"PreToolUse\",\"session_id\":\"$SESSION_ID\"}" | FORGE_SESSION=test-session-1 forge hook exit-plan-mode

# Verify snapshot exists
ls -la .forge/artifacts/test-session-1/plans/ | head -50
```

- [ ] Hook returns `action: snapshotted`
- [ ] Snapshot file created under `.forge/artifacts/test-session-1/plans/`

### 6.7 Smoke Test Stop Hook (Transcript Copy + Queue Markers)

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

SESSION_ID=$(cat .forge/sessions/test-session-1/forge.session.json | jq -r '.confirmed.claude_session_id')

cat > .forge/walkthrough/mock-stop-transcript.jsonl << 'EOF'
{"type":"assistant","message":{"content":"(mock transcript)"}}
EOF

echo "{\"hook_event_name\":\"Stop\",\"session_id\":\"$SESSION_ID\",\"transcript_path\":\".forge/walkthrough/mock-stop-transcript.jsonl\"}" | FORGE_SESSION=test-session-1 forge hook stop

# Verify transcript snapshot copied into artifacts
ls -la .forge/artifacts/test-session-1/transcripts/ | head -50
test -f ".forge/artifacts/test-session-1/transcripts/${SESSION_ID}.jsonl"
```

- [ ] Hook returns JSON success
- [ ] Transcript copied to `.forge/artifacts/test-session-1/transcripts/<session_id>.jsonl`

### 6.8 Smoke Test pre-compact Hook (Transcript Capture)

<!-- auto -->

```bash
# pre-compact captures transcript before compaction (always exit 0)
echo '{"session_id":"test-uuid","transcript_path":"/tmp/test.jsonl","cwd":"/workspace"}' | FORGE_SESSION=test-session-1 forge hook pre-compact
echo "exit=$?"
```

- [ ] Exit code is 0

### 6.9 Smoke Test policy-check Hook (Fail-Open)

<!-- auto -->

```bash
# Default session has policy disabled, so this should allow (exit 0)
echo '{"hook_event_name":"PreToolUse","tool_name":"Write","tool_input":{"file_path":"src/example.py","content":"x"}}' | FORGE_SESSION=test-session-1 forge hook policy-check
echo "exit=$?"
```

- [ ] Exit code is 0 (allowed)

### 6.10 End-to-End Stop Hook (Real Session Exit)

<!-- prereq: 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

Start a real Claude session via Forge, perform a small action, then exit. Verify that hooks actually fired and wrote to
the session manifest and artifacts — this catches "hooks wired but not firing" regressions that manual `forge hook ...`
invocations (steps 6.3–6.9) cannot detect.

In the **container shell**, clean up and start a session:

```
forge session delete hook-e2e-test --force 2>/dev/null || true
forge session start hook-e2e-test --proxy litellm-openai
```

Inside the launched Claude session, do a small action (e.g., "write hello to /tmp/test.txt" or "read
/workspace/README.md and tell me the title"), then exit Claude (Ctrl+C or `/exit`).

After Claude exits, verify:

```bash
# Confirmed fields written by Stop hook
cat .forge/sessions/hook-e2e-test/forge.session.json | jq '.confirmed | {claude_session_id, transcript_path, confirmed_by, confirmed_at}'

# Transcript artifact copied
ls .forge/artifacts/hook-e2e-test/transcripts/

# Stop hook log exists
ls ~/.forge/logs/hooks/stop.*.log | tail -1
```

- [ ] Claude session starts and runs with hooks active
- [ ] After exit, `confirmed.transcript_path` is set in session manifest
- [ ] After exit, `confirmed.claude_session_id` is set (reconciled from actual session)
- [ ] Transcript artifact copied to `.forge/artifacts/hook-e2e-test/transcripts/`
- [ ] Stop hook ran automatically (check `confirmed_by` = "hook:stop")

### 6.11 WorktreeCreate Hook (Claude-Native Worktree)

<!-- prereq: 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

Verify that Claude Code's native worktree creation (via `--worktree` or the Agent tool with `isolation: "worktree"`)
triggers Forge's WorktreeCreate hook, which creates the worktree and auto-installs extensions.

In the **container shell**, clean up and start a worktree session:

```
forge session delete wt-hook-test --yes --force 2>/dev/null || true
git worktree remove /workspace-wt-hook-test --force 2>/dev/null || true
git branch -D wt-hook-test 2>/dev/null || true
forge session start wt-hook-test --worktree --proxy litellm-openai
```

Inside the launched Claude session, verify the status line is visible and type `%help` (should list Forge direct
commands), then exit Claude (`/exit`).

After Claude exits, verify:

```bash
# Worktree was created
ls -d /workspace-wt-hook-test 2>/dev/null || echo "worktree not found"
git worktree list | grep wt-hook-test

# Forge extensions installed in the worktree
cat /workspace-wt-hook-test/.claude/settings.local.json 2>/dev/null | jq '.hooks | keys'

# Cleanup
forge session delete wt-hook-test --yes --force
git worktree list | grep wt-hook-test && echo "FAIL: worktree not removed" || echo "OK: worktree cleaned up"
```

- [ ] Worktree created by Forge's WorktreeCreate hook (not Claude Code's default)
- [ ] Forge extensions installed in the worktree (hooks in settings.local.json)
- [ ] Status line visible in the worktree session
- [ ] Worktree cleaned up after session delete

---
