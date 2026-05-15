# Forge Workflows -- Multi-Model Review & Analysis

Run structured analysis across multiple models. `forge workflow` provides four workflow runners that fan out prompts to
parallel `claude -p` subprocesses and collect results for synthesis.

- Canonical architecture: [`docs/design.md`](../design.md)
- Proxies (model routing): [`proxies.md`](proxies.md)
- Policies (automatic gating): [`policies.md`](policies.md)

---

## Quick start

```bash
# Deep analysis on a topic (single model, default: claude-opus)
forge workflow analyze "Should we use event sourcing for the audit log?"

# Multi-model code review (default worker set: gpt-5.5, gemini-3.1-pro-preview, claude-opus)
forge workflow panel src/forge/session/store.py --code

# Multi-model document review
forge workflow panel docs/design.md

# Multi-model review with custom prompt
forge workflow panel -p "Review the error handling in src/auth/"

# Adversarial debate (proposal evaluation)
forge workflow debate "Should we rewrite the core in Rust?"

# Adversarial code evaluation
forge workflow debate src/forge/cli/ --code

# Two-round consensus building
forge workflow consensus "Should we adopt gRPC for internal services?"
```

Unless you pass `-m`, the multi-model workflows use this built-in worker set:

- `gpt-5.5` -- OpenRouter (preferred proxy: `openrouter-openai`)
- `gemini-3.1-pro-preview` -- OpenRouter (preferred proxy: `openrouter-gemini`)
- `claude-opus` -- direct Anthropic, pinned to stable Claude Opus 4.6

Routing is **capability-based**: models declare what they are (family, provider refs), and Forge derives routes at
runtime from proxy templates and credentials. The preferred proxy is a catalog hint, not a hard requirement -- any
compatible proxy found in the registry will work.

Selectable direct Claude workers include `claude-opus-4.6`, `claude-opus-4.6-1m`, and `claude-opus-4.7`. Additional OSS
models include `deepseek-v4-pro`, `minimax-m2.7`, `qwen3.6-max-preview`, `kimi-k2.6`, and `glm-5.1`. Use `--proxy` to
route all workers through a specific proxy:

```bash
# Route all workers through one proxy (single OPENROUTER_API_KEY setup)
forge workflow panel src/ --code -m gpt-5.5,deepseek-v4-pro --proxy openrouter-openai

# Explicit direct Claude workers
forge workflow panel src/ --code -m claude-opus-4.6,claude-opus-4.7
```

Check which models are locally routable with `forge workflow list-models`. Models are grouped by primary credential and
show `[configured]` / `[not configured]` status. Models whose proxy isn't running or whose API key isn't configured show
as **unavailable**. Use `--available` to see only ready models, or `--json` for structured output.

---

## Workflows

### `forge workflow analyze`

Single-model deep analysis. Combines a structured analysis framework with your topic and sends it to one model.

```bash
forge workflow analyze "Should we split the session module?"
forge workflow analyze -p "Evaluate migration strategy" --json
forge workflow analyze "Architecture review" -m claude-opus --check
```

- First argument (or `-p`) -- topic to analyze
- `-m` -- model to use (default: `claude-opus`)
- `--check` -- gate mode: exit 0 if verdict passes, exit 1 if not

### `forge workflow panel`

Multi-model fan-out. Sends a review framework with your target to the built-in default worker set (or your explicit `-m`
selection) in parallel. Uses document review framework by default; `--code` switches to code review.

```bash
forge workflow panel docs/design.md                    # document review (default)
forge workflow panel src/forge/cli/run.py --code       # code review
forge workflow panel -p "Review the proxy architecture" # custom prompt
forge workflow panel src/ --code --roles security,architecture
forge workflow panel src/ --code --review-type security --severity high
```

- First argument -- file or directory to review (loads review framework automatically)
- `--code` -- use code review framework (default: document review)
- `-p` -- custom review prompt (overrides target+framework and --review-type)
- `--context` -- `blind` (default: fresh subprocess) or `resume:<uuid>` (fork session context)
- `-m` -- models to use (default: `gpt-5.5,gemini-3.1-pro-preview,claude-opus`)
- `--roles` -- comma-separated reviewer roles (security, performance, architecture, maintainability, correctness)
- `--review-type` -- review focus: `full` (default), `security`, `performance`, `quick` (security/performance need
  --code)
- `--severity` -- minimum severity to report: `high` or `critical`
- `--check` -- gate mode: exit 0 if all models pass, exit 1 if any fail

### `forge workflow debate`

Adversarial evaluation with stance injection. Each model receives an assigned stance (for/against/neutral) and evaluates
independently -- workers are **blinded** to each other's output. Uses proposal evaluation by default; `--code` switches
to code evaluation.

```bash
forge workflow debate "Should we use event sourcing?"                    # proposal evaluation (default)
forge workflow debate src/forge/session/ --code                          # code evaluation
forge workflow debate "Evaluate the auth module" --check
forge workflow debate --worker gpt-5.5:for --worker "claude-opus:Focus on security" "proposal"
```

- First argument -- subject to evaluate (proposal text, or file/directory path with `--code`)
- `--code` -- use code evaluation framework (default: proposal evaluation)
- `-p` -- custom prompt (overrides subject+framework)
- `-m` -- models to use (stances assigned cyclically: for, against, neutral)
- `--worker` -- explicit worker spec: `model:stance` or `model:"custom prompt"` (repeatable, mutually exclusive with
  `-m`)
- `--check` -- gate mode: any REJECT verdict exits 1

The CLI builds the evaluation resource internally. Proposal mode uses a 7-point evaluation framework (feasibility,
correctness, trade-offs, risks, completeness, alternatives, recommendation). Code mode uses a 5-point code evaluation
framework (quality, security, performance, architecture, risks).

### `forge workflow consensus`

Two-round multi-model convergence. Role-assigned models evaluate independently in round 1, then reconcile toward a
shared recommendation in round 2. Uses proposal evaluation by default; `--code` switches to code evaluation.

```bash
forge workflow consensus "Should we adopt gRPC for internal services?"      # proposal evaluation
forge workflow consensus src/forge/proxy/ --code                             # code evaluation
forge workflow consensus "Evaluate the caching strategy" --check
forge workflow consensus --worker gpt-5.5:architect --worker claude-opus:security "proposal"
```

- First argument -- subject to evaluate (proposal text, or file/directory path with `--code`)
- `--code` -- use code evaluation framework (default: proposal evaluation)
- `-p` -- custom prompt (overrides subject+framework)
- `-m` -- models to use (roles assigned cyclically)
- `--worker` -- explicit worker spec: `model:role` (repeatable, mutually exclusive with `-m`)
- `--check` -- gate mode: exit 0 if consensus reached, exit 1 if not

---

## Shared flags

All `forge workflow` subcommands support:

| Flag      | Description                                                                                           |
| --------- | ----------------------------------------------------------------------------------------------------- |
| `--json`  | Structured JSON output (model responses, durations, success/fail)                                     |
| `--check` | Gate mode: exit 0 if passed, exit 1 if failed (fail-closed)                                           |
| `-m`      | Comma-separated model names (e.g., `claude-opus,gemini-3.1-pro-preview`)                              |
| `--proxy` | Route proxy-backed workers through this proxy; direct workers (e.g., `claude-opus`) stay on Anthropic |
| `-t`      | Per-model timeout in seconds (default: 600)                                                           |
| `--cwd`   | Working directory for subprocesses                                                                    |

---

## `--check` mode (CI gating)

`--check` evaluates each worker's output for a structured verdict and returns a policy-grade exit code:

- **Exit 0**: all workers succeeded AND emitted accepting verdicts
- **Exit 1**: at least one worker failed or emitted a rejecting verdict

Fail-closed: a worker that succeeds but emits no parseable verdict counts as a failure. This prevents silent
pass-through when models return unstructured output.

Accepted verdict values: `ACCEPT`, `ACCEPT_WITH_CONDITIONS`, `PASS`, `PASSED`, `TRUE`.

```bash
# Use in CI or as a policy gate
forge workflow panel src/critical.py --code --check && echo "Passed" || echo "Failed"
```

---

## Context modes (`panel` only)

`forge workflow panel` supports two context modes for worker subprocesses:

| Mode                      | What workers see                      | Use case                              |
| ------------------------- | ------------------------------------- | ------------------------------------- |
| `--context blind`         | Fresh subprocess, prompt + filesystem | Isolated reviews, cheap, default      |
| `--context resume:<uuid>` | Fork of session with full context     | Architecture reviews, complex changes |

Other subcommands (`analyze`, `debate`) always run blinded -- no session context is passed.

---

## Workflows and supervision

Review workflows and the semantic supervisor (see [`policies.md`](policies.md)) answer different questions about the
same code:

| Signal         | Question                    | Perspective    |
| -------------- | --------------------------- | -------------- |
| Supervisor     | "Does this match the plan?" | Plan alignment |
| Review / panel | "Is this code good?"        | Code quality   |

These can have opposite answers — code can be plan-aligned but suboptimal, or plan-divergent but better. Having both
signals lets you make an informed call. A typical pattern:

1. Executor implements with supervision enabled (drift is blocked)
2. `forge workflow panel` or `forge workflow consensus` reviews the implemented code
3. Reviewers recommend improvements that weren't in the plan
4. You suspend the supervisor (`%guard supervise off`), apply the improvement, then reload an updated plan if needed

The review provides the evidence ("frozen dataclasses would be better here"), the supervisor ensures the deviation is
your decision rather than the executor freelancing.

---

## Troubleshooting

### "No active proxy found" or a worker fails immediately

Workflow routing is capability-based: Forge looks for a running proxy whose template matches the model's provider. The
default models prefer `openrouter-openai` and `openrouter-gemini`, but any compatible proxy will work.

```bash
# See which models are ready vs unavailable (grouped by credential)
forge workflow list-models

# Create the default proxies
forge proxy create openrouter-openai
forge proxy create openrouter-gemini

# Or route everything through one proxy
forge workflow panel src/ --code --proxy openrouter-openai

# Filter to only ready models (useful for scripting)
forge workflow list-models --available
forge workflow list-models --available --json
```

Unknown model names are rejected before execution. Models without a compatible running proxy are flagged by the
preflight check with an actionable suggestion (which proxy to create or start).

### "--check failed but output looks fine"

`--check` requires structured JSON output from each worker with a `passed` or `verdict` field. If the model wrote a
plain-text review without JSON, the check fails (no parseable verdict = failure).

### "Worker timed out"

Default timeout is 600 seconds (10 minutes). Increase with `-t`:

```bash
forge workflow analyze "Deep analysis" -t 900
```

### "Worker fails with `--bare: unknown option`"

Workflow subprocesses use `claude -p --bare` for faster startup when `ANTHROPIC_API_KEY` is available. `--bare` requires
Claude Code >= 2.1.81. Upgrade Claude Code to resolve this.

### "debate rejects my proposal"

Debate builds its evaluation resource internally. If you need a custom evaluation framework, use `panel` with a custom
prompt (`-p`) instead.
