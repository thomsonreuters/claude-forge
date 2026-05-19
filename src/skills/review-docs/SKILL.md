---
name: forge:review-docs
description: Review design documents, specs, and technical writing for completeness and consistency.
disable-model-invocation: false
argument-hint: '[target: path or instruction] [--output path]'
allowed-tools: Read, Grep, Glob, Bash, Agent
---

# Document Review

Review design documents, specs, and technical writing for completeness, consistency, clarity, and implementability.

## Usage

```
/forge:review-docs [target]
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

Model family: !`forge session context --field model_family 2>/dev/null || true` Main model:
!`forge session context --field main_model 2>/dev/null || true`

Resolve session context from `$FORGE_SESSION` or the local environment. Do not force `$CLAUDE_SESSION_ID`: unmanaged
direct Claude sessions are not in Forge's session index, but may still expose direct-model environment metadata.

Pick **one** instruction file (first match wins, read only one):

1. If model family is `openai` or `gemini`: `${CLAUDE_SKILL_DIR}/resources/docs-{family}.md`
2. Otherwise: `${CLAUDE_SKILL_DIR}/resources/docs.md`

If model family lookup returns empty output, `anthropic`, or errors, treat it as the default family and immediately
select `${CLAUDE_SKILL_DIR}/resources/docs.md`. Do not probe multiple variants.

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
Reviewing {target} in docs mode.
  model_family: {family or "anthropic"}
  model:        {main_model or "Claude Code default (exact model not exposed to Forge)"}
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

For a multi-model perspective, use `forge workflow panel` to get independent document reviews from multiple backends:

```bash
forge workflow panel [target] --json
```

Or invoke `/forge:panel` for the full multi-model document review workflow.
