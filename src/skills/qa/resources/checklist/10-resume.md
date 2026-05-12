<!-- prereq: 0.3, 2.1, 5.1 -->

## 10. Session Resume (Phase 10 Feature)

### 10.1 Create Parent Session Artifacts

<!-- auto -->

```bash
# Create a mock transcript artifact for testing resume
SESSION_JSON=".forge/sessions/test-session-1/forge.session.json"
SESSION_ID=$(jq -r '.confirmed.claude_session_id // "fixture-transcript"' "$SESSION_JSON")
TRANSCRIPT_REL=".forge/artifacts/test-session-1/transcripts/${SESSION_ID}.jsonl"
TRANSCRIPT_ABS="/workspace/${TRANSCRIPT_REL}"

mkdir -p "$(dirname "$TRANSCRIPT_ABS")"
cat > "$TRANSCRIPT_ABS" << 'EOF'
{"requestId":"req-1","timestamp":"2026-03-16T00:00:00Z","message":{"role":"user","content":[{"type":"text","text":"Create a hello world function"}]}}
{"requestId":"req-1","timestamp":"2026-03-16T00:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"I'll create a simple hello world function for you."},{"type":"tool_use","id":"tool-1","name":"Write","input":{"file_path":"src/hello.py","content":"def hello():\n    return 'Hello, World!'"}}]}}
{"requestId":"req-1","timestamp":"2026-03-16T00:00:02Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool-1","content":"File written successfully"}]}}
{"requestId":"req-2","timestamp":"2026-03-16T00:00:03Z","message":{"role":"user","content":[{"type":"text","text":"Now add a test"}]}}
{"requestId":"req-2","timestamp":"2026-03-16T00:00:04Z","message":{"role":"assistant","content":[{"type":"text","text":"I'll add a test for the hello function."},{"type":"tool_use","id":"tool-2","name":"Write","input":{"file_path":"tests/test_hello.py","content":"from src.hello import hello\n\ndef test_hello():\n    assert hello() == 'Hello, World!'"}}]}}
{"requestId":"req-2","timestamp":"2026-03-16T00:00:05Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool-2","content":"File written successfully"}]}}
EOF

# Update session file with a realistic transcript artifact entry
jq \
  --arg transcript_abs "$TRANSCRIPT_ABS" \
  --arg transcript_rel "$TRANSCRIPT_REL" \
  --arg session_id "$SESSION_ID" \
  '
  .confirmed.transcript_path = $transcript_abs
  | .confirmed.artifacts = ((.confirmed.artifacts // {}) + {
      transcripts: [{
        captured_at: "2026-03-16T00:00:00Z",
        reason: "stop",
        source_path: $transcript_abs,
        session_id: $session_id,
        copied_path: $transcript_rel,
        copied: true
      }]
    })
  ' "$SESSION_JSON" > /tmp/session.json && mv /tmp/session.json "$SESSION_JSON"
```

- [ ] Transcript artifact created under `.forge/artifacts/test-session-1/transcripts/`
- [ ] Session file updated with transcript path and `confirmed.artifacts.transcripts[]`

### 10.2 Resume with Minimal Strategy

<!-- prereq: 10.1 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, resume with `--strategy minimal`. Claude will launch — verify the new session was created,
then exit.

```
# Resume with minimal strategy (just lineage pointer)
forge session resume test-session-1 --fresh --strategy minimal --child-name test-resumed-minimal

# Check derived session
cat .forge/sessions/test-resumed-minimal/forge.session.json | jq '.confirmed.derivation'
```

- [ ] New session created
- [ ] Derivation shows parent, strategy, and transcript artifact path

### 10.3 Resume with Structured Strategy

<!-- prereq: 10.1 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, resume with `--strategy structured`. Claude will launch — verify the handoff file was
created, then exit.

```
# Resume with structured strategy (conversation skeleton)
forge session resume test-session-1 --fresh --strategy structured --child-name test-resumed-structured

# Check processed handoff
cat .forge/prev_sessions/test-session-1.md
```

- [ ] Handoff file created in `.forge/prev_sessions/`
- [ ] Contains conversation skeleton with truncated tool results

### 10.4 Resume with Full Strategy

<!-- prereq: 10.1 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, resume with `--strategy full`. This includes the complete transcript — the budget check may
fail if the transcript is too large for the proxy context window.

```
# Resume with full strategy (complete transcript)
forge session resume test-session-1 --fresh --strategy full --child-name test-resumed-full

# Check the handoff
cat .forge/prev_sessions/test-session-1.md
```

- [ ] Full transcript included
- [ ] Budget check passed (or error if too large)

### 10.5 Resume with AI-Curated Strategy

<!-- prereq: 10.1 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, resume with `--strategy ai-curated`. This uses OpenRouter directly to select highlights from
the parent transcript, then launches the child session. Expect a security warning about external API access.

```
# Resume with AI-curated strategy (LLM-selected highlights)
# NOTE: Requires OPENROUTER_API_KEY in the default QA provider profile.
forge session delete test-resumed-ai --yes --force 2>/dev/null || true
forge session resume test-session-1 --fresh --strategy ai-curated --child-name test-resumed-ai

# Check the curated output or fallback output
cat .forge/prev_sessions/test-session-1.md
```

- [ ] Parent transcript fixture from 10.1 exists
- [ ] Security warning shown about sending transcript content to OpenRouter
- [ ] Default OpenRouter QA profile: handoff shows `Strategy: ai-curated` and LLM-selected highlights
- [ ] If OpenRouter auth is unavailable, fallback to structured is acceptable and the warning explains the auth failure
- [ ] No warning about missing remote LiteLLM infrastructure in the default OpenRouter QA profile
- [ ] No `No transcript available; using minimal strategy` warning

---
