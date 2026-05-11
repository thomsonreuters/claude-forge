# Forge Policies — Code Quality Gates

Policies enforce coding rules at Write/Edit boundaries. When Claude Code is about to write or edit a file, Forge
evaluates registered policies and blocks or warns based on the result.

- Canonical architecture: [`docs/design.md` §4.1](../design.md)
- Sessions (policy is session-owned): [`sessions.md`](sessions.md)
- Hooks (enforcement mechanism): [`hooks.md`](hooks.md)
- Workflows (multi-model gating via `--check`): [`workflows.md`](workflows.md)

---

## Quick start

```bash
# Enable TDD enforcement for the current session
forge guard enable --bundle tdd

# Check what's active
forge guard status

# Disable all policies
forge guard disable
```

Or from within a Claude Code session (no terminal needed):

```
%guard enable --bundle tdd
%guard status
%guard disable
```

---

## How policies work

Policies run inside the `PreToolUse` hook, which fires before every Write or Edit tool call:

```
Claude calls Write or Edit
  → PreToolUse hook fires
  → PolicyEngine evaluates all applicable policies
  → deny  → tool call blocked (stderr feedback to Claude)
  → warn  → tool call proceeds (warning printed)
  → needs_review → semantic supervisor resolves it; unresolved requests block
  → allow → tool call proceeds silently
```

Policies are **session-scoped** — enabling policies in one session doesn't affect others. State (like which test files
have been touched) persists in the session manifest between hook invocations.

---

## Available bundles

### `tdd` — Test-driven development

| Policy ID               | What it checks                                          |
| ----------------------- | ------------------------------------------------------- |
| `tdd.tests-before-impl` | Must write to `tests/` before writing to `src/`         |
| `tdd.no-skip-tests`     | Blocks `pytest.skip`, `@pytest.mark.skip`, and variants |

Enable with permissive mode to warn instead of block:

```bash
forge guard enable --bundle tdd --permissive
```

### `coding_standards` — Code conventions

| Policy ID                             | What it checks                                      |
| ------------------------------------- | --------------------------------------------------- |
| `coding_standards.no-type-checking`   | Blocks `if TYPE_CHECKING:` imports                  |
| `coding_standards.no-backward-compat` | Blocks backward-compatibility wrappers and adapters |

### `workflow` — LLM-based review pipelines (advanced)

Config-driven pipelines that classify code changes via a cheap LLM tagger, then route through filter → checker →
reviewer stages. Only actions flagged as "architectural" or "migration" reach the expensive reviewer.

> **Note:** The `workflow` bundle is not available via `forge guard enable`. Enable it by setting `policy.bundles` and
> `policy.bundle_config` in the session manifest (e.g., via `forge session set`). See [`design.md` §4.1.2](../design.md)
> for the configuration schema.

---

## CLI reference

### `forge guard enable`

```bash
forge guard enable --bundle <name> [--bundle <name>] [--fail-mode open|closed] [--permissive]
```

- `--bundle` / `-b` — bundle to enable (repeatable). Values: `tdd`, `coding_standards`
- `--fail-mode` — `open` (default: allow on engine errors) or `closed` (deny on engine errors)
- `--permissive` — TDD permissive mode: warn instead of deny (`bundle_config.tdd.strict=false`)

### `forge guard disable`

```bash
forge guard disable
```

Disables all policy enforcement for the current session.

### `forge guard status`

```bash
forge guard status
```

Shows: enabled/disabled, active bundles, fail mode, active rules, and per-policy state (e.g., which test files have been
touched for TDD).

### `forge guard check`

Evaluate policies on demand against a file or git diff. Unlike hook-triggered checks, this runs explicitly and defaults
to fail-mode=closed.

```bash
forge guard check --bundle <name> --file <path>
forge guard check --bundle <name> --bundle <name> -f src/foo.py --json
git diff | forge guard check --bundle coding_standards --diff
```

- `--bundle` / `-b` — bundle to evaluate (repeatable, required)
- `--file` / `-f` — file to evaluate against
- `--diff` — read git diff from stdin instead of a file
- `--fail-mode` — `closed` (default) or `open`
- `--json` — structured JSON output

Exit codes: 0 (passed or warnings only), 1 (policy violation), 2 (usage error or engine failure).

### `forge guard supervisor`

Evaluate a file against an approved plan via the semantic supervisor. Fail-closed with 3-way exit codes.

```bash
forge guard supervisor -f src/foo.py -r <session-uuid>
forge guard supervisor -f src/foo.py -r <session-uuid> --proxy openrouter-openai --json
```

- `--file` / `-f` — file to evaluate (required)
- `--resume-id` / `-r` — Claude session UUID of the planning session (required)
- `--proxy` — proxy for supervisor LLM calls (optional)
- `--timeout` / `-t` — supervisor timeout in seconds (default: 45)
- `--json` — structured JSON output

Exit codes: 0 (aligned), 1 (divergent), 2 (could not evaluate — infra failure, timeout, or parse error).

---

## In-session commands

These work inside Claude Code without switching to a terminal:

| Command                                     | Effect                                                    |
| ------------------------------------------- | --------------------------------------------------------- |
| `%guard status`                             | Show policy config and state                              |
| `%guard enable --bundle tdd`                | Enable TDD enforcement                                    |
| `%guard enable --bundle tdd --permissive`   | Enable TDD in warn-only mode                              |
| `%guard disable`                            | Disable all policies                                      |
| `%guard check [--staged] [--bundle <name>]` | Evaluate git diff against policies (diagnostic, not gate) |

`%guard check` runs `git diff` (or `git diff --staged` with `--staged`), splits per file, evaluates each file against
the specified bundles (or session-configured bundles if omitted), and reports pass/fail with violations. It reads
session config even when enforcement is disabled — useful for verifying fixes before re-enabling.

> **Note:** `%guard enable/disable` applies session overrides that persist until changed or reset. The CLI command
> `forge guard enable/disable` mutates the session intent.

For the full list of `%` commands, see [`hooks.md`](hooks.md#in-session-commands--commands).

---

## Configuration

### Fail modes

| Mode     | On engine error     | On policy evaluation error |
| -------- | ------------------- | -------------------------- |
| `open`   | Allow the tool call | Allow the tool call        |
| `closed` | Block the tool call | Block the tool call        |

Default is `open`. Use `closed` for high-stakes sessions where you'd rather block on uncertainty than risk a bad write.

### Permissive mode (TDD)

`--permissive` sets `bundle_config.tdd.strict=false`. The `tdd.tests-before-impl` policy emits a warning instead of
blocking. The `tdd.no-skip-tests` policy is unaffected (always blocks skip patterns).

### Semantic supervisor (advanced)

The semantic supervisor is an LLM session that validates Write/Edit actions against an approved plan. It uses
`claude -p --resume <session_id>` to continue a planning session in a read-only advisory role.

Configured in the session manifest under `policy.supervisor`:

- `resume_id` — Claude session UUID of the planning session
- `proxy` — proxy for supervisor LLM calls (optional, defaults to session proxy)
- `timeout_seconds` — max wait for supervisor response (default: 45s)
- `throttle_seconds` — cache window for repeated checks (default: 30s)

The supervisor only blocks when the verdict is "divergent" with **high confidence (≥0.8) and citations** referencing the
plan. Low confidence or missing citations produce a warning instead. Timeouts, errors, and unparseable responses also
result in a warning, not a block.

### Why supervision matters (beyond TDD)

Deterministic policies like `tdd` enforce **process** — tests before implementation. The semantic supervisor enforces
**intent** — does this change match what was agreed?

The difference matters for subtle drift. An executor might make a reasonable design decision (say, making a dataclass
frozen) that isn't in the approved plan. Tests pass, the code is correct, deterministic policies are satisfied. But the
plan didn't call for it — it's an unreviewed design judgment that compounds over a long implementation session.

The supervisor catches this because it has the full planning conversation in its `--resume` context. It can cite the
specific plan section and explain the divergence, giving the executor enough information to self-correct.

**Surfacing plan gaps.** Supervision works bidirectionally. When the executor hits a supervisor block and the plan
genuinely didn't account for something (a dependency, an interface constraint), the executor stops and surfaces the
conflict. This forces **explicit plan evolution** via `%guard supervise reload` instead of silent improvisation. Each
reload is an auditable moment where the plan's authority changed.

**Explicit deviation.** When a multi-model review (see [`workflows.md`](workflows.md)) recommends an improvement that
wasn't in the plan, you can turn the supervisor off (`%guard supervise off`), apply the change, and optionally reload an
updated plan. The deviation goes through *you* — not silently absorbed by the executor.

---

## Stuck playbook (when policies block repeatedly)

When a policy blocks the agent repeatedly and you need to unblock:

```
1. Disable enforcement   →  %guard disable
2. Fix the issue         →  (work with agent or edit manually)
3. Verify fix passes     →  %guard check                      (optional)
4. Re-enable enforcement →  %guard enable --bundle tdd
```

Step 3 is diagnostic — it evaluates without gating. If the check passes, re-enabling enforcement (step 4) lets the next
Write/Edit proceed.

**From a terminal** (alternative to `%` commands):

```bash
# Disable
forge guard disable

# Check a specific file
forge guard check --bundle tdd --file src/foo.py

# Check all unstaged changes
git diff | forge guard check --bundle tdd --diff

# Re-enable
forge guard enable --bundle tdd
```

---

## What happens when a policy blocks

When a policy returns `deny`, the PreToolUse hook exits with code 2 and prints the violation to stderr. Claude Code sees
the error and adjusts its approach.

Example stderr output when TDD blocks a write to `src/` without tests:

```
Policy violation(s):
  [tdd.tests-before-impl] Implementation changes require test changes first
    Fix: Write or update tests in tests/ directory before modifying src/ code
```

**To unblock:**

- Write tests first (the TDD way)
- Switch to permissive mode: `%guard enable --bundle tdd --permissive`
- Disable policies entirely: `%guard disable`

---

## Troubleshooting

### Policies not evaluating

- Check that policies are enabled: `forge guard status`
- Policies only evaluate on `Write` and `Edit` tool calls — `Bash`, `Read`, etc. are not checked
- Verify the hook is installed: check your settings file for `PreToolUse` entries with `forge hook policy-check` (see
  [`hooks.md`](hooks.md) for which settings file applies to your scope)

### Blocked but tests were written

The TDD policy tracks state across hook invocations. If you wrote tests in a *previous* session, the current session
doesn't know about it (state is session-scoped).

- Check state: `%guard status` shows `tests_touched` set
- If starting fresh: write at least one test file in the current session before `src/` files

### Supervisor timeout

The semantic supervisor has a 45s default timeout. If it exceeds this:

- The action is allowed with a warning (fail-open)
- Check proxy connectivity: is the supervisor's proxy running?
- Reduce supervisor response time: use a faster model via `proxy`

---

## Inspecting policy decisions

`forge guard status` shows the current policy config and evaluation counts. For the full decision audit trail (verdicts,
violations, citations, timestamps), use:

```bash
forge session show <name> --field confirmed.policy
forge session show <name> --json | jq '.confirmed.policy.decisions'
```

The human-readable `forge session show <name>` includes a "Policy Evals:" summary line under Confirmed State.

To silence the post-evaluation summary lines printed after each Write/Edit check:

```bash
forge config set policy_summary_feedback=off
```

This suppresses the `[forge] Policy: checked ...` summary and `additionalContext`. Deny messages and substantive
warnings stay visible regardless.

## Files to inspect (debugging)

| File                                        | Purpose                                      |
| ------------------------------------------- | -------------------------------------------- |
| `.forge/sessions/<name>/forge.session.json` | Session manifest (policy config + state)     |
| Claude settings file for your scope         | Hook config (`PreToolUse` -> `policy-check`) |
| `~/.forge/logs/`                            | Proxy logs (if supervisor uses a proxy)      |
