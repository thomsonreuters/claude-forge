# Forge Skills

Forge installs skills that teach Claude how to compose Forge capabilities into workflows. Skills are invoked with
`/forge:<name>` in a Claude Code session.

- Canonical architecture: [`docs/design.md`](../design.md) SS5.5
- Workflow CLI (engine): [`workflows.md`](workflows.md)
- Session context (model detection): [`sessions.md`](sessions.md)

---

## Quick start

```bash
# Code review
/forge:review src/forge/session/

# Document review
/forge:review-docs docs/design.md

# Explain code
/forge:understand src/forge/core/ops/session_context.py

# Multi-model panel review (fans out to 3 models)
/forge:panel src/forge/session/ --code

# Deep single-model analysis
/forge:analyze "Should we use event sourcing for the audit log?"

# Adversarial multi-model debate
/forge:debate "Should we migrate from skills to MCP?"
```

All target-taking skills accept a file path, directory, or free-form instruction. Skills auto-detect which model family
is running (openai, gemini, anthropic) from the session's proxy template and select model-optimized instructions.

---

## `/forge:review`

Review code for conformance, correctness, and architecture alignment.

```
/forge:review [target]
```

| Argument | Required | Description                                                         |
| -------- | -------- | ------------------------------------------------------------------- |
| `target` | Optional | File, directory, or instruction on what to review (defaults to cwd) |

**Model-aware resources:** The skill loads model-specific review instructions (`code-openai.md`, `code-gemini.md`, etc.)
based on the session's proxy. Falls back to the Opus-optimized default if no model-specific resource exists.

**Multi-model alternative:** For independent reviews from multiple models in parallel, use `forge workflow panel --code`
(CLI) or `/forge:panel --code` (skill).

---

## `/forge:review-docs`

Review design documents, specs, and technical writing for completeness and consistency.

```
/forge:review-docs [target]
```

| Argument | Required | Description                                                         |
| -------- | -------- | ------------------------------------------------------------------- |
| `target` | Optional | File, directory, or instruction on what to review (defaults to cwd) |

Same model-aware resource selection as `/forge:review`, but loads `docs.md` / `docs-{family}.md` rubrics.

**Multi-model alternative:** For independent reviews from multiple models, use `forge workflow panel` (CLI) or
`/forge:panel` (skill).

---

## `/forge:understand`

Explain code, documentation, or technical concepts. Auto-detects code vs docs mode.

```
/forge:understand [target] [--mode code|docs] [--depth quick|detailed|deep]
```

| Argument  | Required | Description                                                                    |
| --------- | -------- | ------------------------------------------------------------------------------ |
| `target`  | Optional | File, directory, question, or instruction on what to explain (defaults to cwd) |
| `--mode`  | Optional | `code` or `docs` (default: auto-detected from target)                          |
| `--depth` | Optional | `quick`, `detailed`, or `deep` (default: `detailed`)                           |

Auto-detects `code` or `docs` mode from the target (file extensions, directory contents). Same model-aware resource
selection as other skills.

**Depth levels** control output length and analysis method:

| Depth    | Output        | Method                                  |
| -------- | ------------- | --------------------------------------- |
| quick    | \<500 words   | High-level overview                     |
| detailed | 500-1000      | Step-by-step with architecture and flow |
| deep     | Comprehensive | Multi-step systematic investigation     |

---

## `/forge:panel`

Multi-model panel review. Multiple models review independently, then findings are synthesized.

```
/forge:panel [target] [--code] [--models model1,model2]
```

| Argument   | Required | Description                                                         |
| ---------- | -------- | ------------------------------------------------------------------- |
| `target`   | Optional | File, directory, or instruction on what to review (defaults to cwd) |
| `--code`   | Optional | Switch: use code review framework (default: document review)        |
| `--models` | Optional | Comma-separated model list (default: Forge workflow defaults)       |

The panel runs `forge workflow panel` under the hood. Each model reviews independently, then the main agent synthesizes
consensus findings, unique insights, and conflicts.

**Default models:**

| Model                    | Strength                            | Via                  |
| ------------------------ | ----------------------------------- | -------------------- |
| `gpt-5.5`                | Logical problems, systematic review | litellm-openai proxy |
| `gemini-3.1-pro-preview` | Balanced analysis, large context    | litellm-gemini       |
| `claude-opus`            | Stable Claude Opus 4.6 reasoning    | Direct Anthropic     |

Selectable direct Claude workers include `claude-opus-4.6`, `claude-opus-4.6-1m`, and `claude-opus-4.7`. Use
`--models claude-opus-4.6,claude-opus-4.7` when you want both stable Opus 4.6 and bounded-review Opus 4.7 in the panel.

**Requirements:** GPT-5.5 and Gemini require active proxies; Claude Opus requires `ANTHROPIC_API_KEY`. See
[auth.md](auth.md#which-auth-do-i-need) for setup.

---

## `/forge:debate`

Adversarial multi-model evaluation. Models argue for, against, and neutrally about a subject.

```
/forge:debate [subject] [--code] [--models model1,model2]
```

| Argument   | Required | Description                                                                     |
| ---------- | -------- | ------------------------------------------------------------------------------- |
| `subject`  | Optional | File, directory, proposal, or instruction on what to evaluate (defaults to cwd) |
| `--code`   | Optional | Switch: use code evaluation framework (default: proposal)                       |
| `--models` | Optional | Comma-separated model list (default: Forge workflow defaults)                   |

The debate runs `forge workflow debate` under the hood. Each model is assigned a stance (for/against/neutral) and
evaluates independently -- workers are blinded to each other's output. The main agent synthesizes points of agreement,
key disagreements, and an evidence-weighted recommendation.

**Default models:**

| Model                    | Stance  | Role                     | Via                  |
| ------------------------ | ------- | ------------------------ | -------------------- |
| `gpt-5.5`                | FOR     | Supporter -- strengths   | litellm-openai proxy |
| `gemini-3.1-pro-preview` | AGAINST | Critic -- risks          | litellm-gemini       |
| `claude-opus`            | NEUTRAL | Analyst -- balanced view | Direct Anthropic     |

**Requirements:** GPT-5.5 and Gemini require active proxies; Claude Opus requires `ANTHROPIC_API_KEY`. See
[auth.md](auth.md#which-auth-do-i-need) for setup.

---

## `/forge:challenge`

Pressure-test a claim, recommendation, or assumption with adversarial skepticism.

```
/forge:challenge [claim or objection]
```

| Argument | Required | Description                                                            |
| -------- | -------- | ---------------------------------------------------------------------- |
| `claim`  | Optional | Statement, objection, or question to pressure-test (inferred if empty) |

The skill defaults to skepticism: it assumes the claim may be wrong and tries to prove that. Only softens to a balanced
conclusion if the skeptical case fails. Returns a verdict: validated, partially validated, not supported, or
insufficient evidence.

**Model-invocable:** Claude can trigger this automatically when you say "are you sure?", "push back on this", or "what
am I missing?". When invoked without arguments, it infers the claim from the preceding conversation.

---

## Other skills

| Skill                | Purpose                                                          |
| -------------------- | ---------------------------------------------------------------- |
| `/forge:analyze`     | Deep single-model analysis (default model: claude-opus)          |
| `/forge:consensus`   | Two-round multi-model convergence toward a shared recommendation |
| `/forge:smoke-test`  | Read-only installation health check                              |
| `/forge:walkthrough` | Interactive feature tour (hermetic test repo)                    |
| `/forge:qa`          | Full Docker-based QA (requires `full` profile)                   |

---

## Model-aware resource selection

Skills automatically detect the model family from the session's proxy template:

```
Session -> proxy template -> tier model name -> vendor prefix -> family
```

| Family      | Templates using it          | Resource suffix |
| ----------- | --------------------------- | --------------- |
| `openai`    | `litellm-openai`            | `-openai.md`    |
| `gemini`    | `litellm-gemini`            | `-gemini.md`    |
| `anthropic` | `litellm-anthropic`, direct | (default)       |

The detection chain uses `forge session show --field model_family`, which resolves the session from `$FORGE_SESSION`
(set by Forge hooks at launch). Skills internally pass the Claude session UUID for resolution. If detection fails,
skills fall back to the Opus-optimized default resource.

**No manual configuration needed.** The right instructions are selected automatically based on which proxy you're
connected to.

---

## Troubleshooting

### "Skill not found"

Skills are installed by `forge extension enable`. Verify installation:

```bash
ls ~/.claude/skills/  # Should show review/, review-docs/, understand/, panel/, etc.
```

### Wrong model instructions selected

Check the detected family:

```bash
forge session show --field model_family
```

If the family is wrong, the proxy template's tier models may not have the expected vendor prefix. Check with
`forge session show --json` to see the full proxy and model mapping.

### Skills installed in both user and project scope

If you ran `forge extension enable --user` and `forge extension enable` (project) in the same repo, you have two copies
of every skill. This can cause stale instructions, duplicate hook firing, or unexpected behavior if one copy is
outdated.

Check with:

```bash
ls ~/.claude/skills/       # User-level
ls .claude/skills/         # Project-level
```

Fix by keeping one scope:

```bash
forge extension disable --user     # Remove user-level
forge extension enable --project   # Keep project-level
```

See [design_appendix.md §E.5](../design_appendix.md#e5-multi-scope-installation-55----skill-resolution) for details.

### Panel fails with "No active proxy found"

The panel's default model set includes `gpt-5.5` and `gemini-3.1-pro-preview`, which require active proxies:

```bash
forge proxy create litellm-openai
forge proxy create litellm-gemini
```
