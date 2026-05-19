---
name: forge:analyze
description: Deep single-model analysis of a topic, question, or architectural decision.
disable-model-invocation: false
argument-hint: '[topic: path or question or instruction] [--output path] [--models model]'
context: fork
effort: high
allowed-tools: Bash, Read
---

# Deep Analysis

Deep analysis of a topic, question, or architectural decision using a dedicated model worker.

## Usage

```
/forge:analyze [topic] [--models model]
```

## Arguments

| Argument   | Required | Description                                                                            |
| ---------- | -------- | -------------------------------------------------------------------------------------- |
| `topic`    | Optional | Question, file path, directory, or instruction on what to analyze (defaults to asking) |
| `--models` | Optional | Comma-separated model list (default: claude-opus)                                      |
| `--output` | Optional | Write result to file instead of conversation (e.g., `analysis.md`)                     |

**Available models:** !`forge workflow list-models`

Only use models with status **ready** in the table above. If the default set includes unavailable models, pass
`--models <ready models>` explicitly. If the user explicitly requested an unavailable model, stop and tell them what
proxy or credential is missing rather than silently substituting. If no models are ready, tell the user what's missing
and stop.

---

## Execution

### Step 1: Resolve Topic and Flags

Parse `$ARGUMENTS` into a positional topic and optional flags. The topic is everything that is not a recognized flag
(question, file path, directory, or free-form instruction). Strip any leading `@` prefix on the topic. If no topic is
found, ask the user what they want to analyze.

Recognized flags (extract from `$ARGUMENTS` if present):

- `--models <value>` — comma-separated model list (default: claude-opus)
- `--output <path>` — write result to file instead of conversation

Never ask the user to clarify. If `$ARGUMENTS` contains anything, proceed immediately.

### Step 2: Run Deep Analysis

```bash
forge workflow analyze "the user's topic" [--models <models>] --json
```

Omit `--models` if the user didn't specify (defaults to claude-opus).

If the command exits with a non-zero code or returns invalid JSON, report the error to the user and stop. Do not attempt
to parse partial output or fabricate a response.

### Step 3: Present Analysis

Format the model's deep analysis as a structured response:

0. Resolved model used: from `resolved_models`, include requested model, resolved model ref, provider, proxy, and
   template
1. Problem decomposition
2. Key evidence and considerations
3. Analysis and trade-offs
4. Recommendations with rationale

If the model failed, report the error and suggest retrying.

**Output routing:** If `--output` was specified, write the complete analysis to that path using the Write tool (create
parent directories if needed). Print a one-line confirmation: `Wrote analysis to {path}`. Do not also print the full
result in the conversation. If `--output` was not specified, print the result in the conversation as usual.

---

## Requirements

- **Forge CLI**: `forge` must be on PATH
- **Claude CLI**: workflow workers run through local `claude -p`; `claude` must be on PATH in this Bash environment
- **Claude Opus**: Uses direct Anthropic (no proxy needed)
