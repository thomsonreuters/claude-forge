<!-- prereq: 0.3, 2.1, 4.2 -->

## 14. Multi-Model Review (`forge workflow`)

Validates workflow runners + skill architecture.

- `forge workflow panel` is the fan-out runner CLI (supports `--code`, `--context`, and `--check`).
- `/forge:analyze` is a skill that calls `forge workflow analyze` (N=1 model).
- `/forge:debate` is a skill that calls `forge workflow debate` (supports `--code` for code evaluation).
- This section uses `$FORGE_QA_WORKFLOW_MODELS` (set by `start-container.sh` per provider profile). Workflow proxy
  aliases are created in 4.2.
- Omitting `--models` uses all configured defaults (from `forge workflow list-models`).

### 14.1 List Available Workflow Models

<!-- auto -->

```bash
forge workflow list-models
forge workflow list-models --json
forge workflow list-models --available

# Verify structured model metadata used by routing/preflight.
forge workflow list-models --json \
  | jq -e 'map(has("name") and has("model_id") and has("family") and has("provider_refs") and has("preferred_proxy") and has("status") and has("reason")) | all'

# `--available` JSON should include only ready models.
forge workflow list-models --available --json \
  | jq -e 'all(.status == "ready")'
```

- [ ] Groups models by primary credential and shows `[configured]` / `[not configured]`
- [ ] Shows model name, description, and status (`ready`/`unavailable`/`error`)
- [ ] `--json` outputs a structured JSON array with `name`, `model_id`, `family`, `provider_refs`, `preferred_proxy`,
  `status`, and `reason`
- [ ] `--available` filters to ready models only
- [ ] `--available` with no ready models shows explanatory message (table), `[]` (JSON)
- [ ] Proxy in registry but not running shows "unavailable" (not "ready")

### 14.2 `forge workflow panel`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
forge workflow panel docs/ --models $FORGE_QA_WORKFLOW_MODELS --json
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

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Policy gate mode (structured verdict + exit code)
forge workflow panel -p "Check for security issues" --models $FORGE_QA_WORKFLOW_MODELS --check
echo "Exit code: $?"
```

- [ ] Returns structured JSON verdict
- [ ] Exit code 0 = pass, 1 = findings

### 14.4 `forge workflow panel --code`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Multi-model code review (uses bundled codereview resource)
forge workflow panel src/ --code --models $FORGE_QA_WORKFLOW_MODELS --json

# With --check mode
forge workflow panel src/ --code --models $FORGE_QA_WORKFLOW_MODELS --check
echo "Exit code: $?"
```

- [ ] Spawns multiple workers with codereview resource prompt
- [ ] Returns structured JSON output per worker
- [ ] `--check` mode: fail-closed -- every worker must succeed AND emit parseable verdict

### 14.5 `forge workflow analyze`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Single-model deep analysis (N=1 fan-out with bundled thinkdeep resource)
forge workflow analyze -p "Analyze the architecture of this project" --models $FORGE_QA_WORKFLOW_MODEL_A --json

# With --check mode (exit 0=pass, 1=findings)
forge workflow analyze -p "Check for security issues" --models $FORGE_QA_WORKFLOW_MODEL_A --check
echo "Exit code: $?"
```

- [ ] Spawns single worker with analysis resource prompt
- [ ] Returns structured JSON output
- [ ] `--check` mode returns exit code 0/1 with verdict

### 14.6 `forge workflow debate`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Adversarial debate with positional proposal
forge workflow debate "Should we rewrite the core in Rust?" --models $FORGE_QA_WORKFLOW_MODELS --json

# Gate mode (exit 0=pass, 1=fail). Debate is fail-closed: success without a parseable verdict = failure.
forge workflow debate "Should we adopt microservices?" --models $FORGE_QA_WORKFLOW_MODELS --check
```

- [ ] Spawns workers with stance injection (for/against/neutral)
- [ ] Mandatory blinding (workers don't see each other's output)
- [ ] Returns structured output with agreement/disagreement areas

### 14.7 `forge workflow debate --code`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Adversarial code evaluation (uses bundled code evaluation resource)
forge workflow debate src/ --code --models $FORGE_QA_WORKFLOW_MODELS --json

# With --check mode
forge workflow debate src/ --code --models $FORGE_QA_WORKFLOW_MODELS --check
echo "Exit code: $?"
```

- [ ] Spawns workers with code evaluation resource + stance injection
- [ ] Returns structured JSON output with code-specific findings per worker
- [ ] `--check` mode: fail-closed -- every worker must succeed AND emit parseable verdict

### 14.8 `forge workflow consensus`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Two-round consensus with role-assigned workers (proposal mode)
forge workflow consensus "Should we adopt a microservices architecture?" --models $FORGE_QA_WORKFLOW_MODELS --json

# Gate mode (requires 'position' field, not 'verdict')
forge workflow consensus "Should we adopt event sourcing?" --models $FORGE_QA_WORKFLOW_MODELS --check
echo "Exit code: $?"
```

- [ ] Spawns workers with role injection (architecture/security/correctness)
- [ ] Two rounds: independent positions then reconciliation
- [ ] Mandatory blinding both rounds (no --resume passed to workers)
- [ ] JSON includes `round1`, `round2`, `roles`, `role_map`, `reconciliation_brief`
- [ ] `--check` mode: requires `position` field (rejects legacy `passed`/`verdict`)

### 14.9 `forge workflow consensus --code`

<!-- prereq: 14.1 -->

<!-- auto -->

```bash
# Two-round code consensus (code mode uses architecture/security/maintainability)
forge workflow consensus src/ --code --models $FORGE_QA_WORKFLOW_MODELS --json

# With --check mode
forge workflow consensus src/ --code --models $FORGE_QA_WORKFLOW_MODELS --check
echo "Exit code: $?"
```

- [ ] Spawns workers with code-mode role cycle (architecture/security/maintainability)
- [ ] Returns structured JSON output with code-specific findings per worker per round
- [ ] `--check` mode: schema-strict -- only SUPPORT/SUPPORT_WITH_CONDITIONS pass

### 14.10 `/forge:debate` (Live Session)

<!-- prereq: 14.1 -->

<!-- human:guided -->

<!-- requires: api_key -->

Validate the real Claude-facing `/forge:debate` path, not a terminal fallback. This step passes only if Claude Code
accepts the slash command and actually executes the adversarial runner end to end.

If Session B is not already open, start Claude Code in the container shell first:

```
cd /workspace
claude
```

Read `$FORGE_QA_WORKFLOW_MODELS` from the container environment and construct the fully expanded command for the user to
type in Session B:

```
/forge:debate --models <expanded FORGE_QA_WORKFLOW_MODELS> A startup with 5 developers has a working Python monolith serving 10k req/sec. They're hitting scaling issues. Should they rewrite the core in Rust?
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

<!-- prereq: 14.1 -->

<!-- requires: api_key -->

<!-- auto -->

```bash
# Single model filter -- should limit to that model only
forge workflow panel docs/ --models $FORGE_QA_WORKFLOW_MODEL_B --json 2>&1 | jq '.results | keys | length'

echo "---"

# Multiple model filter (comma-separated)
forge workflow panel docs/ --models $FORGE_QA_WORKFLOW_MODELS --json 2>&1 | jq '{results: (.results | keys), successful: .successful, failed: .failed}'

echo "---"

# Verify result keys match the requested models
forge workflow panel docs/ --models $FORGE_QA_WORKFLOW_MODEL_B --json 2>&1 | jq '.results | keys'
```

- [ ] Single `--models` value produces 1 result key in `.results`
- [ ] Comma-separated `--models` produces one result per specified model (`.successful` count matches)
- [ ] Result keys in `.results` correspond to the requested model names

### 14.12 Workflow Routing, `--via`, and Preflight

<!-- prereq: 4.2, 14.1 -->

<!-- requires: api_key -->

<!-- auto -->

```bash
# Explicit proxy routing: one selected proxy handles this worker.
FORGE_DEBUG=1 forge workflow panel docs/ \
  --models "$FORGE_QA_WORKFLOW_MODEL_A" \
  --via "$FORGE_QA_OPENAI_PROXY" \
  --json > /tmp/forge-workflow-via.json

jq '{results: (.results | keys), successful, failed}' /tmp/forge-workflow-via.json

echo "---"

# Human output should surface non-blocking routing advisories when they apply.
forge workflow analyze -p "Reply with READY only." \
  --models "$FORGE_QA_WORKFLOW_MODEL_A" \
  --via "$FORGE_QA_OPENAI_PROXY" 2>&1 | tee /tmp/forge-workflow-via-warning.txt

grep -E "Routing warning|tier overrides|Proxy tier mappings" /tmp/forge-workflow-via-warning.txt || true

echo "---"

# Routing decisions are logged for observability when logging is enabled.
latest_log="$(ls -t "$FORGE_HOME"/logs/cli/workflow.*.log 2>/dev/null | head -n 1)"
test -n "$latest_log" && grep "Routing decision: model=$FORGE_QA_WORKFLOW_MODEL_A" "$latest_log"

echo "---"

# Direct Anthropic workers fail fast when no Anthropic credential is available.
tmp_home="$(mktemp -d)"
env -u ANTHROPIC_API_KEY FORGE_HOME="$tmp_home" \
  forge workflow analyze -p "This should not call the model." \
    --models claude-opus-4.6 \
    --json 2>&1 | tee /tmp/forge-workflow-direct-preflight.json
rm -rf "$tmp_home"

jq -e '.preflight_errors[0] | test("ANTHROPIC_API_KEY|anthropic"; "i")' \
  /tmp/forge-workflow-direct-preflight.json
```

- [ ] `--via` resolves a compatible selected proxy and the JSON output remains parseable
- [ ] Non-JSON workflow output prints a `Routing warning:` when `--via` selects a cross-family or live-advisory route
  (same-family routes may have no warning)
- [ ] The latest CLI workflow log contains a consolidated `Routing decision:` line with model, source, proxy/template,
  and model ref
- [ ] Direct Anthropic workflow workers fail during preflight with an actionable credential error when
  `ANTHROPIC_API_KEY` is absent

---
