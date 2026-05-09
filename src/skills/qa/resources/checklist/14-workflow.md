<!-- prereq: 0.3, 2.1, 4.2 -->

## 14. Multi-Model Review (`forge workflow`)

Validates workflow runners + skill architecture.

- `forge workflow panel` is the fan-out runner CLI (supports `--code`, `--context`, and `--check`).
- `/forge:analyze` is a skill that calls `forge workflow analyze` (N=1 model).
- `/forge:debate` is a skill that calls `forge workflow debate` (supports `--code` for code evaluation).
- This section uses the named proxies created in 4.2: `litellm-openai` and `litellm-gemini`.
- Omitting `--models` uses all configured defaults (from `forge workflow list-models`).

### 14.1 List Available Workflow Models

<!-- auto -->

```bash
forge workflow list-models
forge workflow list-models --json
forge workflow list-models --available
```

- [ ] Shows configured models with proxy and description
- [ ] Shows status column (ready/unavailable/error)
- [ ] `--json` outputs structured JSON array with status field
- [ ] `--available` filters to ready models only
- [ ] `--available` with no ready models shows explanatory message (table), `[]` (JSON)
- [ ] Proxy in registry but not running shows "unavailable" (not "ready")

### 14.2 `forge workflow panel`

<!-- auto -->

```bash
forge workflow panel docs/ --models gpt-5.5,gemini-3.1-pro-preview --json
```

Context mode examples (display-only -- resume needs a real session UUID):

```
# Fork current Claude session context into workers
forge workflow panel -p "Continue the review" --context resume:<session-uuid> --json

# Explicit blind mode (default -- no --resume passed to workers)
forge workflow panel docs/ --context blind --json
```

- [ ] Returns structured JSON output
- [ ] `--context blind` is the default (no --resume passed to workers)
- [ ] `--context resume:<uuid>` passes --resume to workers

### 14.3 `forge workflow panel --check`

<!-- auto -->

```bash
# Policy gate mode (structured verdict + exit code)
forge workflow panel -p "Check for security issues" --models gpt-5.5,gemini-3.1-pro-preview --check
echo "Exit code: $?"
```

- [ ] Returns structured JSON verdict
- [ ] Exit code 0 = pass, 1 = findings

### 14.4 `forge workflow panel --code`

<!-- auto -->

```bash
# Multi-model code review (uses bundled codereview resource)
forge workflow panel src/ --code --models gpt-5.5,gemini-3.1-pro-preview --json

# With --check mode
forge workflow panel src/ --code --models gpt-5.5,gemini-3.1-pro-preview --check
echo "Exit code: $?"
```

- [ ] Spawns multiple workers with codereview resource prompt
- [ ] Returns structured JSON output per worker
- [ ] `--check` mode: fail-closed -- every worker must succeed AND emit parseable verdict

### 14.5 `forge workflow analyze`

<!-- auto -->

```bash
# Single-model deep analysis (N=1 fan-out with bundled thinkdeep resource)
forge workflow analyze -p "Analyze the architecture of this project" --models gpt-5.5 --json

# With --check mode (exit 0=pass, 1=findings)
forge workflow analyze -p "Check for security issues" --models gpt-5.5 --check
echo "Exit code: $?"
```

- [ ] Spawns single worker with analysis resource prompt
- [ ] Returns structured JSON output
- [ ] `--check` mode returns exit code 0/1 with verdict

### 14.6 `forge workflow debate`

<!-- auto -->

```bash
# Adversarial debate with positional proposal
forge workflow debate "Should we rewrite the core in Rust?" --models gpt-5.5,gemini-3.1-pro-preview --json

# Gate mode (exit 0=pass, 1=fail). Debate is fail-closed: success without a parseable verdict = failure.
forge workflow debate "Should we adopt microservices?" --models gpt-5.5,gemini-3.1-pro-preview --check
```

- [ ] Spawns workers with stance injection (for/against/neutral)
- [ ] Mandatory blinding (workers don't see each other's output)
- [ ] Returns structured output with agreement/disagreement areas

### 14.7 `forge workflow debate --code`

<!-- auto -->

```bash
# Adversarial code evaluation (uses bundled code evaluation resource)
forge workflow debate src/ --code --models gpt-5.5,gemini-3.1-pro-preview --json

# With --check mode
forge workflow debate src/ --code --models gpt-5.5,gemini-3.1-pro-preview --check
echo "Exit code: $?"
```

- [ ] Spawns workers with code evaluation resource + stance injection
- [ ] Returns structured JSON output with code-specific findings per worker
- [ ] `--check` mode: fail-closed -- every worker must succeed AND emit parseable verdict

### 14.8 `forge workflow consensus`

<!-- auto -->

```bash
# Two-round consensus with role-assigned workers (proposal mode)
forge workflow consensus "Should we adopt a microservices architecture?" --models gpt-5.5,gemini-3.1-pro-preview --json

# Gate mode (requires 'position' field, not 'verdict')
forge workflow consensus "Should we adopt event sourcing?" --models gpt-5.5,gemini-3.1-pro-preview --check
echo "Exit code: $?"
```

- [ ] Spawns workers with role injection (architecture/security/correctness)
- [ ] Two rounds: independent positions then reconciliation
- [ ] Mandatory blinding both rounds (no --resume passed to workers)
- [ ] JSON includes `round1`, `round2`, `roles`, `role_map`, `reconciliation_brief`
- [ ] `--check` mode: requires `position` field (rejects legacy `passed`/`verdict`)

### 14.9 `forge workflow consensus --code`

<!-- auto -->

```bash
# Two-round code consensus (code mode uses architecture/security/maintainability)
forge workflow consensus src/ --code --models gpt-5.5,gemini-3.1-pro-preview --json

# With --check mode
forge workflow consensus src/ --code --models gpt-5.5,gemini-3.1-pro-preview --check
echo "Exit code: $?"
```

- [ ] Spawns workers with code-mode role cycle (architecture/security/maintainability)
- [ ] Returns structured JSON output with code-specific findings per worker per round
- [ ] `--check` mode: schema-strict -- only SUPPORT/SUPPORT_WITH_CONDITIONS pass

### 14.10 `/forge:debate` (Live Session)

<!-- human:guided -->

<!-- requires: api_key -->

Validate the real Claude-facing `/forge:debate` path, not a terminal fallback. This step passes only if Claude Code
accepts the slash command and actually executes the adversarial runner end to end.

If Session B is not already open, start Claude Code in the container shell first:

```
cd /workspace
claude
```

Then, at the Claude prompt in Session B, type exactly:

```
/forge:debate A startup with 5 developers has a working Python monolith serving 10k req/sec. They're hitting scaling issues. Should they rewrite the core in Rust?
```

Wait for Claude to finish. Do not replace this with `forge workflow debate` in the shell; that CLI surface is already
covered by `14.6`. If Claude only says `Command completed`, echoes the skill instructions back, or asks you to run the
commands manually, treat this step as a failure.

- [ ] Slash command accepted in Claude Code (no unknown-skill or parsing error)
- [ ] Claude executes the skill itself (not just instruction injection / "Command completed")
- [ ] Workers spawned with different stances (for/against/neutral)
- [ ] Synthesis produced with points of agreement AND disagreement
- [ ] Different perspectives visible in the final response

### 14.11 Workflow `--models` Filter

<!-- requires: api_key -->

<!-- auto -->

```bash
# Single model filter -- should limit to that model only
forge workflow panel docs/ --models gemini-3.1-pro-preview --json 2>&1 | jq '.results | keys | length'

echo "---"

# Multiple model filter (comma-separated)
forge workflow panel docs/ --models gpt-5.5,gemini-3.1-pro-preview --json 2>&1 | jq '{results: (.results | keys), successful: .successful, failed: .failed}'

echo "---"

# Verify result keys match the requested models
forge workflow panel docs/ --models gemini-3.1-pro-preview --json 2>&1 | jq '.results | keys'
```

- [ ] Single `--models` value produces 1 result key in `.results`
- [ ] Comma-separated `--models` produces one result per specified model (`.successful` count matches)
- [ ] Result keys in `.results` correspond to the requested model names

---
