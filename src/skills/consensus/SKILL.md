---
name: forge:consensus
description: Multi-model consensus workflow. Role-assigned models converge toward a shared recommendation through two rounds of evaluation and reconciliation.
disable-model-invocation: true
argument-hint: '[subject: path or proposal or instruction] [--output path] [--code] [--models m1,m2] [--worker model:role]'
context: fork
effort: high
allowed-tools: Bash, Read
---

# Consensus Workflow

Run a multi-model consensus workflow where role-assigned models build a shared recommendation through two rounds of
evaluation and reconciliation.

When invoked from Claude Code, execute the workflow now. Do not just restate these instructions, say "Command
completed", or ask the user to run the commands manually unless a real prerequisite is missing.

## Usage

```
/forge:consensus [subject] [--code] [--models model1,model2] [--worker model:role]
```

## Arguments

| Argument   | Required | Description                                                                        |
| ---------- | -------- | ---------------------------------------------------------------------------------- |
| `subject`  | Optional | File, directory, proposal, or instruction on what to evaluate (defaults to cwd)    |
| `--code`   | Optional | Switch: use code evaluation framework (default: proposal)                          |
| `--models` | Optional | Comma-separated model list (default: Forge workflow defaults)                      |
| `--worker` | Optional | Repeatable: model:role or model:"custom prompt" (mutually exclusive with --models) |
| `--output` | Optional | Write result to file instead of conversation (e.g., `consensus.md`)                |

**Available models:** !`forge workflow list-models`

Only use models with status **ready** in the table above. If the default set includes unavailable models, pass
`--models <ready models>` explicitly. If the user explicitly requested an unavailable model, stop and tell them what
proxy or credential is missing rather than silently substituting. If no models are ready, tell the user what's missing
and stop.

---

## Execution

### Step 1: Resolve Subject and Flags

Parse `$ARGUMENTS` into a positional subject and optional flags. The subject is everything that is not a recognized flag
(file path, directory, proposal text, or free-form instruction). Strip any leading `@` prefix on the subject. If no
subject is found, default to the current working directory.

Recognized flags (extract from `$ARGUMENTS` if present):

- `--code` -- switch
- `--models <value>` -- comma-separated model list (mutually exclusive with --worker)
- `--worker <value>` -- repeatable: model:role or model:custom prompt
- `--output <path>` -- write result to file instead of conversation

Never ask the user to clarify. If `$ARGUMENTS` contains anything, proceed immediately.

### Step 2: Run Consensus Workflow

```bash
forge workflow consensus "<subject>" [--code] [--models <models>] [--worker <spec>]... --json
```

Omit any flag the user didn't specify. Do not pass both `--models` and `--worker`.

Parse the JSON output. The workflow runs two rounds:

- **Round 1**: Each model independently evaluates the subject from their assigned role
- **Round 2**: Each model receives all Round 1 positions and produces a reconciled recommendation

If the command fails, surface the real error and stop; do not claim success.

### Step 3: Synthesize

Read `${CLAUDE_SKILL_DIR}/resources/synthesis.md` for synthesis instructions.

Apply the synthesis rules to produce a unified consensus report from both rounds of results.

**Output routing:** If `--output` was specified, write the complete synthesis to that path using the Write tool (create
parent directories if needed). Print a one-line confirmation: `Wrote synthesis to {path}`. Do not also print the full
result in the conversation. If `--output` was not specified, print the result in the conversation as usual.

---

## Models and Roles

Models are assigned roles cyclically. Default roles differ by mode:

**Proposal mode** (default):

| Order | Default Model          | Role         | Focus                                        |
| ----- | ---------------------- | ------------ | -------------------------------------------- |
| 1st   | gpt-5.5                | architecture | Structural alignment, coupling, abstractions |
| 2nd   | gemini-3.1-pro-preview | security     | Vulnerabilities, trust boundaries, risks     |
| 3rd   | claude-opus            | correctness  | Logic errors, edge cases, invariants         |

**Code mode** (`--code`):

| Order | Default Model          | Role            | Focus                                  |
| ----- | ---------------------- | --------------- | -------------------------------------- |
| 1st   | gpt-5.5                | architecture    | Component boundaries, dependency flow  |
| 2nd   | gemini-3.1-pro-preview | security        | Injection, auth, secrets, trust        |
| 3rd   | claude-opus            | maintainability | Readability, complexity, test coverage |

Use `--models` to control which models participate. Use `--worker` for explicit model:role mapping.

**Available named roles:** architecture, security, correctness, maintainability, performance

## Requirements

- **Forge CLI**: `forge` must be on PATH
- **Proxies**: GPT-5.5 and Gemini require active proxies (`forge proxy create openrouter-openai`)
