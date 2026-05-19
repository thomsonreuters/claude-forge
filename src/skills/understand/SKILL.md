---
name: forge:understand
description: Explain code, documentation, or technical concepts. Auto-detects code vs docs mode.
disable-model-invocation: false
argument-hint: '[target: path or question or instruction] [--output path] [--mode code|docs] [--depth quick|detailed|deep]'
allowed-tools: Read, Grep, Glob, Bash, Agent
---

# Understand

Analyze code or documentation to extract clear explanations of structure, design, and behavior.

## Usage

```
/forge:understand [target] [--mode code|docs] [--depth quick|detailed|deep]
```

## Arguments

| Argument   | Required | Description                                                                    |
| ---------- | -------- | ------------------------------------------------------------------------------ |
| `target`   | Optional | File, directory, question, or instruction on what to explain (defaults to cwd) |
| `--mode`   | Optional | `code` or `docs` (default: auto-detected from target)                          |
| `--depth`  | Optional | `quick`, `detailed`, or `deep` (default: `detailed`)                           |
| `--output` | Optional | Write result to file instead of conversation (e.g., `explanation.md`)          |

## Execution

Follow these steps in order. Do not skip steps.

### Step 1: Resolve Target

`$ARGUMENTS` is the target. It may be a file path, directory, question, or free-form instruction. If it starts with `@`,
strip the prefix (Claude Code file reference syntax). If `$ARGUMENTS` is empty, default to the current working
directory.

Recognized flags (extract from `$ARGUMENTS` if present):

- `--mode <value>` — code or docs
- `--depth <value>` — quick, detailed, or deep
- `--output <path>` — write result to file instead of conversation

Never ask the user to clarify. If `$ARGUMENTS` contains anything, proceed immediately.

### Step 2: Detect Mode

If `--mode` was not specified, auto-detect from the target (first match wins):

| Pattern                                                                        | Mode |
| ------------------------------------------------------------------------------ | ---- |
| `*.md`, `*.rst`, `*.txt`                                                       | docs |
| `*.py`, `*.ts`, `*.js`, `*.go`, `*.rs`, `*.java`                               | code |
| Path starts with `docs/`, `design/`, `adr/`, `rfcs/`                           | docs |
| Path starts with `src/`, `lib/`, `pkg/`, `cmd/`                                | code |
| `README*`, `CLAUDE.md`, `CHANGELOG*`                                           | docs |
| Question contains "design", "architecture", "rationale", "ADR", "why we chose" | docs |
| Question contains "bug", "function", "class", "method", "how does"             | code |
| Default                                                                        | code |

Do not ask the user -- just apply the rules.

### Step 3: Load Instruction File

**Do NOT start the analysis until this step is complete.**

Model family: !`forge session context --field model_family 2>/dev/null || true` Main model:
!`forge session context --field main_model 2>/dev/null || true`

Resolve session context from `$FORGE_SESSION` or the local environment. Do not force `$CLAUDE_SESSION_ID`: unmanaged
direct Claude sessions are not in Forge's session index, but may still expose direct-model environment metadata.

Pick **one** instruction file (first match wins, read only one):

1. If model family is `openai` or `gemini`: `${CLAUDE_SKILL_DIR}/resources/{mode}-{family}.md`
2. Otherwise: `${CLAUDE_SKILL_DIR}/resources/{mode}.md`

If model family lookup returns empty output, `anthropic`, or errors, treat it as the default family and immediately
select `${CLAUDE_SKILL_DIR}/resources/{mode}.md`. Do not probe multiple variants.

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
Analyzing {target} in {mode} mode (depth: {depth}).
  model_family: {family or "anthropic"}
  model:        {main_model or "Claude Code default (exact model not exposed to Forge)"}
  instruction:  {instruction_file_name}
```

Do not read target files or begin analysis until after you have:

1. Resolved the target
2. Resolved the mode
3. Resolved the instruction file
4. Emitted the preflight summary message

### Step 4: Execute Analysis

If the selected instruction file refers to an Explore subagent, use the `Agent` tool with `subagent_type: "Explore"`. Do
not interpret `Task` in resource files as a separate tool.

If the selected instruction file mentions disallowed or unavailable tools, stop and report the mismatch instead of
substituting another tool.

For depth handling inside this skill:

- `quick`: perform a concise local analysis using the allowed tools in this skill
- `detailed`: perform a fuller local analysis using the allowed tools in this skill
- `deep`: perform the deepest local analysis available with the allowed tools in this skill

Do not call `mcp__zen__*` tools from this skill.

Execute analysis following the loaded instructions with the specified depth. The instruction file defines the structure
and output format -- follow it.

**Depth levels:**

| Depth      | Output Size | Execution                        |
| ---------- | ----------- | -------------------------------- |
| `quick`    | \<500 words | Local analysis, concise          |
| `detailed` | 500-1000    | Local analysis, fuller coverage  |
| `deep`     | Full        | Local analysis, maximum coverage |

When a resource file contains tool guidance that conflicts with this skill's allowed tools, this SKILL.md file wins. Do
not improvise around the conflict.

**Output routing:** If `--output` was specified, write the complete explanation to that path using the Write tool
(create parent directories if needed). Print a one-line confirmation: `Wrote explanation to {path}`. Do not also print
the full result in the conversation. If `--output` was not specified, print the result in the conversation as usual.
