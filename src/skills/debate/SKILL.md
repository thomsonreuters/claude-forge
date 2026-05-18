---
name: forge:debate
description: Adversarial multi-model evaluation. Models argue for, against, and neutrally about a subject.
disable-model-invocation: true
argument-hint: '[subject: path or proposal or instruction] [--output path] [--code] [--models m1,m2] [--worker model:stance]'
context: fork
effort: high
allowed-tools: Bash, Read
---

# Debate Evaluation

Run an adversarial multi-model evaluation where models argue for, against, and neutrally about a subject.

When invoked from Claude Code, execute the workflow now. Do not just restate these instructions, say "Command
completed", or ask the user to run the commands manually unless a real prerequisite is missing.

## Usage

```
/forge:debate [subject] [--code] [--models model1,model2]
```

## Arguments

| Argument   | Required | Description                                                                          |
| ---------- | -------- | ------------------------------------------------------------------------------------ |
| `subject`  | Optional | File, directory, proposal, or instruction on what to evaluate (defaults to cwd)      |
| `--code`   | Optional | Switch: use code evaluation framework (default: proposal)                            |
| `--models` | Optional | Comma-separated model list (default: Forge workflow defaults)                        |
| `--worker` | Optional | Repeatable: model:stance or model:"custom prompt" (mutually exclusive with --models) |
| `--output` | Optional | Write result to file instead of conversation (e.g., `debate.md`)                     |

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

- `--code` — switch
- `--models <value>` — comma-separated model list (mutually exclusive with --worker)
- `--worker <value>` — repeatable: model:stance or model:custom prompt
- `--output <path>` — write result to file instead of conversation

Never ask the user to clarify. If `$ARGUMENTS` contains anything, proceed immediately.

### Step 2: Run Adversarial Evaluation

```bash
forge workflow debate "<subject>" [--code] [--models <models>] [--worker <spec>]... --json
```

Omit any flag the user didn't specify. Do not pass both `--models` and `--worker`.

Parse the JSON output. Each model receives a different stance (for/against/neutral) and evaluates the subject from that
perspective. If the command fails, surface the real error and stop; do not claim success.

### Step 3: Synthesize

Combine the perspectives:

1. **Points of agreement** across all stances
2. **Key disagreements** and which stance has stronger evidence
3. **Risk assessment** from the critic's perspective
4. **Viability assessment** from the supporter's perspective
5. **Overall recommendation** with confidence level

Make it clear which parts came from agreement across stances versus which parts remain disputed.

**Output routing:** If `--output` was specified, write the complete synthesis to that path using the Write tool (create
parent directories if needed). Print a one-line confirmation: `Wrote synthesis to {path}`. Do not also print the full
result in the conversation. If `--output` was not specified, print the result in the conversation as usual.

---

## Models and Roles

Models are assigned stances cyclically. Default models:

| Order | Default Model          | Stance  | Role                     |
| ----- | ---------------------- | ------- | ------------------------ |
| 1st   | gpt-5.5                | FOR     | Supporter -- strengths   |
| 2nd   | gemini-3.1-pro-preview | AGAINST | Critic -- risks          |
| 3rd   | claude-opus            | NEUTRAL | Analyst -- balanced view |

Use `--models` to control which models participate. Stances cycle through for/against/neutral in order.

## Code Mode

When `--code` is specified, models evaluate the target code from adversarial perspectives:

- **FOR** stance: Identifies good design, correct implementations, production readiness
- **AGAINST** stance: Identifies bugs, security issues, performance problems, architectural flaws
- **NEUTRAL** stance: Balanced assessment of code quality with file:line evidence

## Requirements

- **Forge CLI**: `forge` must be on PATH
- **Proxies**: GPT-5.5 and Gemini require active proxies (`forge proxy create openrouter-openai`)
