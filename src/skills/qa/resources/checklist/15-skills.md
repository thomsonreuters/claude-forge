<!-- prereq: 0.3, 2.1, 4.2, 5.1 -->

## 15. Skills (`/forge:review`, `/forge:understand`, `/forge:panel`, `/forge:consensus`)

Validates the user-facing skill invocation UX. Section 14 tested the underlying `forge workflow` CLI engine; this
section tests the skills that wrap it with auto-detection and model-aware resource selection.

The workflow-backed skills in this section depend on the workflow proxy aliases created in 4.2. In partial runs such as
`--from 15`, prereq auto-resolution should create those aliases before `/forge:panel` or `/forge:consensus` is checked.

### 15.1 `forge session context` CLI

<!-- auto -->

```bash
# Verify the session context command works (requires an active session)
forge session context test-session-1 --json

# Extract model family field
forge session context test-session-1 --field model_family
```

- [ ] Returns valid JSON with session_name, proxy, model_family, models, policy
- [ ] `--field model_family` returns a raw string (openai, gemini, or anthropic)
- [ ] Direct (no-proxy) session returns `model_family: "anthropic"`

### 15.2 `forge session context` with UUID

<!-- auto -->

<!-- prereq: 5.1 -->

```bash
# Get the Claude session UUID from the session manifest
UUID=$(cat .forge/sessions/test-session-1/forge.session.json | jq -r '.confirmed.claude_session_id // empty')

# If UUID exists, verify UUID-based resolution
if [ -n "$UUID" ]; then
  forge session context "$UUID" --field session_name
  echo "UUID_RESOLVED=true"
else
  echo "UUID_RESOLVED=skip (no confirmed UUID yet)"
fi
```

- [ ] UUID resolves to the correct session name (or skips if no UUID confirmed yet)

### 15.3 `/forge:review` (Live Session)

<!-- human:guided -->

<!-- requires: api_key -->

In Session B (or a live Claude session in the container), invoke the review skill to verify resource selection.

1. In the container shell, launch Claude and invoke the review skill:

```
/forge:review src/
```

2. Verify that Claude:
   - Loads a code review resource from `~/.claude/skills/review/resources/`
   - Produces a structured code review with findings

Expected:

- [ ] Skill invocation accepted by Claude Code (no "skill not found" error)
- [ ] Review output includes file:line references and severity ratings

### 15.4 `/forge:understand` (Live Session)

<!-- human:guided -->

<!-- requires: api_key -->

In the same live Claude session, invoke the understand skill.

1. Invoke the understand skill:

```
/forge:understand src/main.py --depth quick
```

2. Verify that Claude:
   - Reads the target file
   - Produces a structured explanation

Expected:

- [ ] Skill invocation accepted
- [ ] Output includes Summary and Key Components sections
- [ ] Quick depth produces concise output (\<500 words)

### 15.5 `/forge:panel` (Live Session)

<!-- human:guided -->

<!-- requires: api_key -->

In the same live Claude session, invoke the panel skill for a multi-model review.

1. Invoke the panel skill:

```
/forge:panel src/ --code
```

2. This fans out to multiple models. Verify that Claude:
   - Calls `forge workflow panel` under the hood
   - Collects results from multiple models
   - Synthesizes findings

Expected:

- [ ] Skill invocation accepted (runs as forked subagent)
- [ ] Multi-model fan-out executes through configured workflow proxies
- [ ] Output names the actual resolved model ref and proxy/template used for each worker
- [ ] Synthesis includes consensus findings and unique insights

Failure cue: if output says the default model set is unusable because OpenRouter proxies are missing, or falls back to a
Claude-only panel, mark this step failed. That means 4.2 did not create the workflow proxy aliases for this run.

### 15.6 `/forge:consensus` (Live Session)

<!-- human:guided -->

<!-- requires: api_key -->

In the same live Claude session, invoke the consensus skill for a multi-model recommendation.

1. Invoke the consensus skill:

```
/forge:consensus Should we split the session manager into separate read and write modules?
```

2. This runs two rounds across multiple models. Verify that Claude:
   - Calls `forge workflow consensus` under the hood
   - Round 1 collects independent positions from role-assigned workers
   - Round 2 produces reconciled recommendations
   - Synthesizes into agreed/disputed/no-consensus sections

Expected:

- [ ] Skill invocation accepted (runs as forked subagent)
- [ ] Two-round execution visible in output (Round 1 positions + Round 2 reconciliation)
- [ ] Output names the actual resolved model ref and proxy/template used for each worker
- [ ] Synthesis distinguishes agreed recommendations from unresolved disagreements
- [ ] Roles visible in output (architecture, security, correctness)

Failure cue: if output says the default model set is unusable because OpenRouter proxies are missing, or falls back to
Claude-only workers, mark this step failed. That means 4.2 did not create the workflow proxy aliases for this run.

---
