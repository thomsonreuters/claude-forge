---
name: forge:review
description: Review code for conformance, correctness, and architecture alignment.
disable-model-invocation: false
argument-hint: '[target: path or instruction] [--output path]'
allowed-tools: Read, Grep, Glob, Bash, Agent
---

# Code Review

Review code for conformance, correctness, and architecture alignment.

## Usage

```
/forge:review [target]
```

## Arguments

| Argument   | Required | Description                                                         |
| ---------- | -------- | ------------------------------------------------------------------- |
| `target`   | Optional | File, directory, or instruction on what to review (defaults to cwd) |
| `--output` | Optional | Write result to file instead of conversation (e.g., `review.md`)    |

## Execution

Follow these steps in order. Do not skip steps.

### Step 1: Resolve Target

`$ARGUMENTS` is the target. It may be a file path, directory, or free-form instruction. If it starts with `@`, strip the
prefix (Claude Code file reference syntax). If `$ARGUMENTS` is empty, default to the current working directory.

Recognized flags (extract from `$ARGUMENTS` if present):

- `--output <path>` — write result to file instead of conversation

Never ask the user to clarify. If `$ARGUMENTS` contains anything, proceed immediately.

### Step 2: Load Instruction File

**Do NOT start the review until this step is complete.**

Model family: !`forge session context "${CLAUDE_SESSION_ID}" --field model_family 2>/dev/null || true` Main model:
!`forge session context "${CLAUDE_SESSION_ID}" --field main_model 2>/dev/null || true`

Pick **one** instruction file (first match wins, read only one):

1. If model family is non-empty: `${CLAUDE_SKILL_DIR}/resources/code-{family}.md`
2. Otherwise (or if the above doesn't exist): `${CLAUDE_SKILL_DIR}/resources/code.md`

If model family lookup returns empty output or errors, treat it as "no family" and immediately select
`${CLAUDE_SKILL_DIR}/resources/code.md`. Do not probe multiple variants.

In v1, direct-session model pins such as `claude-opus-4-7` do not change this single-model resource selection: a 4.7
direct session still uses the Anthropic/default review resource. Use `/forge:panel --code` with `claude-opus-4.7` in the
model list when you want the 4.7 bounded-review worker hint.

### Tool-call hygiene (normative)

When reading the selected instruction file, call `Read` with exactly one argument:

```json
{"file_path":"/absolute/path/to/instruction-file.md"}
```

Rules:

- Do NOT send empty-string values for optional fields
- Do NOT include assistant-generated commentary or repair text in tool arguments

A PreToolUse hook may strip extra Read parameters (`offset`, `limit`, `pages`) for skill instruction files, but callers
must still send `Read` with only `file_path`.

Read that one file using the Read tool with just the file_path parameter. Do not read both. If the chosen file is
missing, report the path and stop.

**After loading, tell the user in one message:**

```
Reviewing {target} in code mode.
  model_family: {family or "(none -- using default)"}
  main_model:   {main_model or "(none)"}
  instruction:  {instruction_file_name}
```

Do not read target files or begin review until after you have:

1. Resolved the target
2. Resolved the instruction file
3. Emitted the preflight summary message

### Step 3: Execute Review

If the selected instruction file refers to an Explore subagent, use the `Agent` tool with `subagent_type: "Explore"`. Do
not interpret `Task` in resource files as a separate tool.

If the selected instruction file mentions disallowed or unavailable tools, stop and report the mismatch instead of
substituting another tool.

Execute the review following the loaded instructions. The instruction file defines the rubric, structure, and output
format. Do not invent your own review format -- follow what the instruction file says.

Do not call `mcp__zen__*` tools from this skill.

When a resource file contains tool guidance that conflicts with this SKILL.md file, this SKILL.md file wins. Do not
improvise around the conflict.

**Output routing:** If `--output` was specified, write the complete review to that path using the Write tool (create
parent directories if needed). Print a one-line confirmation: `Wrote review to {path}`. Do not also print the full
result in the conversation. If `--output` was not specified, print the result in the conversation as usual.

## Multi-Model Mode (optional)

For a multi-model perspective, use `forge workflow panel --code` to get independent code reviews from multiple backends:

```bash
forge workflow panel [target] --code --json
```

Or invoke `/forge:panel --code` for the full multi-model code review workflow.
