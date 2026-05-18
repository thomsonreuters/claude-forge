# GPT-5.5 Prompting Guide

> Synthesized from [OpenAI Prompt Guidance](https://developers.openai.com/api/docs/guides/prompt-guidance),
> [OpenAI Platform docs](https://developers.openai.com/api/docs/guides/latest-model), and
> [OpenAI Cookbook](https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_prompting_guide). May 2026.

## Overview

GPT-5.5 is OpenAI's frontier model for **complex professional work**, announced April 23, 2026 and made available in the
API on April 24, 2026. It is tuned for long-context, tool-heavy, professional workflows. Prompt-relevant changes:

- **1,050,000 token API context window**
- **128,000 max output tokens**
- **`medium` default reasoning effort**, with `none`, `low`, `high`, and `xhigh` available
- **More outcome-first behavior** - shorter prompts with clear success criteria usually work better than process-heavy
  legacy scaffolding

**Key characteristic:** GPT-5.5 is designed for production-grade assistants and agents. It performs best when prompts
clearly specify the **output contract**, **tool-use expectations**, and **completion criteria**. The highest-leverage
prompt changes are choosing reasoning effort by task shape, defining exact output and citation formats, and making
completion criteria explicit.

---

## Core API Parameters

### `reasoning.effort`

| Level    | Use Case                                                                                         |
| -------- | ------------------------------------------------------------------------------------------------ |
| `none`   | Execution-heavy workloads: workflow steps, extraction, triage, structured transforms.            |
| `low`    | Tasks needing nuanced interpretation: implicit requirements, ambiguity, cancelled-tool recovery. |
| `medium` | **Default.** Research-heavy: long-context synthesis, multi-document review, conflict resolution. |
| `high`   | Complex multi-step problems, strategy writing.                                                   |
| `xhigh`  | Maximum reasoning depth. 3-5x cost of `none`.                                                    |

**Defaults across the GPT-5 family:**

- GPT-5: `medium`
- GPT-5.1, GPT-5.2: `none`
- GPT-5.5: `medium`

**Best practice:** Make prompt updates before increasing reasoning effort. Increase `reasoning.effort` one notch only
after prompt fixes. When lowering to `none` for execution-heavy workloads, encourage the model to "think" or outline
steps before answering.

### `verbosity`

A **dedicated API parameter** (not just prompt engineering) that controls response length.

| Level    | Behavior                                     | Use when                                      |
| -------- | -------------------------------------------- | --------------------------------------------- |
| `low`    | Terse, to-the-point, just the facts          | Latency and scanability matter most           |
| `medium` | **Default.** Balanced detail for most tasks. | General assistants and professional workflows |
| `high`   | Detailed, explanatory, comprehensive.        | The user asked for depth or auditability      |

```python
response = client.responses.create(
    model="gpt-5.5",
    input="Your prompt here",
    text={"verbosity": "low"}
)
```

**Interaction with prompts:** If explicit instructions conflict with the `verbosity` parameter, explicit instructions
take precedence. For code generation, Cursor found that setting `verbosity: low` for text output while prompting for
verbose code in tool calls produced the best results.

### Context Window

- **1,050,000 tokens** input / **128,000 tokens** max output
- Prompts above 272K input tokens have higher API pricing; use context budgets deliberately.

### Knowledge Cutoff

**December 1, 2025.**

---

## Key Behavioral Differences from GPT-5.2

| Aspect                | GPT-5.5 Behavior                                                                  |
| --------------------- | --------------------------------------------------------------------------------- |
| Reasoning default     | `medium`; use `low` before `none` when planning, search, or tool use still matter |
| Prompt shape          | Outcome-first prompts usually work better than step-by-step process scaffolding   |
| Tool calling          | Stronger tool selection; define tool triggers, evidence rules, and stop rules     |
| User-visible preamble | Useful for time-to-first-token in long or tool-heavy turns                        |
| Verbosity             | Concise and direct by default; controllable via the `verbosity` API parameter     |
| Instruction following | More literal and thorough; define success criteria and stopping conditions        |

---

## Prompting Patterns

### Output Contracts and Completion Criteria

OpenAI's primary recommendation for GPT-5.5. Explicitly define **what "done" looks like**:

```xml
<output_contract>
- Return a JSON object with keys: summary, findings[], recommendations[], confidence_score.
- Each finding must include: file_path, line_range, severity (critical|warning|info), description.
- confidence_score is 0.0-1.0 reflecting how thoroughly the codebase was analyzed.
- Task is complete when all files in scope have been reviewed and findings are deduplicated.
</output_contract>
```

Start with the smallest prompt that passes your evals. Add blocks only when they fix a measured failure mode.

### Controlling Verbosity and Output Shape

Use the `verbosity` API parameter as the **primary lever**, and prompt-level constraints as secondary:

```xml
<output_verbosity_spec>
- Default: 3-6 sentences or <=5 bullets for typical answers.
- For simple "yes/no + short explanation" questions: <=2 sentences.
- For complex multi-step or multi-file tasks:
  - 1 short overview paragraph
  - then <=5 bullets tagged: What changed, Where, Risks, Next steps, Open questions.
- Do not rephrase the user's request unless it changes semantics.
</output_verbosity_spec>
```

### Initiative Nudges

If the model feels too literal or stops at the first plausible answer, add an **initiative nudge** before raising
`reasoning.effort`:

```xml
<initiative>
- Do not stop at the first plausible answer.
- Look for second-order issues, edge cases, and missing constraints.
- If the task is safety or accuracy critical, perform at least one verification step.
</initiative>
```

This is cheaper and often more effective than bumping `reasoning.effort` up a notch.

### Preventing Scope Drift

GPT-5.5 is more controllable than GPT-5.2 but still prone to scope drift on coding tasks:

```xml
<design_and_scope_constraints>
- Implement EXACTLY and ONLY what the user requests.
- No extra features, no added components, no UX embellishments.
- Style aligned to the design system at hand.
- Do NOT invent colors, shadows, tokens, animations, or new UI elements unless requested.
- If any instruction is ambiguous, choose the simplest valid interpretation.
</design_and_scope_constraints>
```

### Long-Context and Recall

With 1.05M tokens available, long-context handling is more common. For inputs >10K tokens, use **forced re-grounding**:

```xml
<long_context_handling>
- For inputs longer than ~10k tokens (multi-chapter docs, long threads, multiple PDFs):
  - First, produce a short internal outline of key sections relevant to the user's request.
  - Re-state the user's constraints explicitly before answering.
  - Anchor claims to sections ("In the 'Data Retention' section...") rather than speaking generically.
- If the answer depends on fine details (dates, thresholds, clauses), quote or paraphrase them.
</long_context_handling>
```

### Preambles (Tool-Use Transparency)

GPT-5.5 can generate brief, user-visible explanations before invoking tools — outlining its intent before the actual
tool call. This boosts tool-calling accuracy without bloating reasoning overhead.

Enable with a system instruction:

```
Before you call a tool, explain in one sentence why you are calling it.
```

### Handling Ambiguity & Hallucination Risk

```xml
<uncertainty_and_ambiguity>
- If the question is ambiguous or underspecified, explicitly call this out and:
  - Ask up to 1-3 precise clarifying questions, OR
  - Present 2-3 plausible interpretations with clearly labeled assumptions.
- Never fabricate exact figures, line numbers, or external references when uncertain.
- When unsure, prefer language like "Based on the provided context..." instead of absolute claims.
</uncertainty_and_ambiguity>
```

**High-risk self-check for sensitive contexts:**

```xml
<high_risk_self_check>
Before finalizing an answer in legal, financial, compliance, or safety-sensitive contexts:
- Briefly re-scan your own answer for:
  - Unstated assumptions,
  - Specific numbers or claims not grounded in context,
  - Overly strong language ("always," "guaranteed," etc.).
- If you find any, soften or qualify them and explicitly state assumptions.
</high_risk_self_check>
```

---

## Agentic Steerability & User Updates

GPT-5.5 works well in long-running workflows when the prompt defines progress, stopping conditions, and when to ask for
help.

### Verbosity + Code Quality (Cursor's Pattern)

Cursor found the best results by separating text and code verbosity:

- Set `verbosity: low` at the API level to keep text outputs brief
- In the prompt, strongly encourage verbose, well-commented output in coding tools only

This prevents status updates and post-task summaries from disrupting flow while keeping code readable.

### User Update Discipline

```xml
<user_updates_spec>
- Send brief updates (1-2 sentences) only when:
  - You start a new major phase of work, or
  - You discover something that changes the plan.
- Avoid narrating routine tool calls ("reading file...", "running tests...").
- Each update must include at least one concrete outcome ("Found X", "Confirmed Y", "Updated Z").
- Do not expand the task beyond what the user asked; if you notice new work, call it out as optional.
</user_updates_spec>
```

### Delegation Rules

```xml
<delegation_rules>
- Delegate only when subtasks are independent or can proceed in parallel.
- For each delegated task, define ownership, expected output, dependencies, and "done".
- Keep blocking decisions in the main workflow unless delegation is explicitly useful.
- Integrate delegated results before finalizing.
</delegation_rules>
```

---

## Tool Calling and Parallelism

### Tool Use Rules

```xml
<tool_usage_rules>
- Prefer tools over internal knowledge whenever:
  - You need fresh or user-specific data (tickets, orders, configs, logs).
  - You reference specific IDs, URLs, or document titles.
- Parallelize independent reads (read_file, fetch_record, search_docs) when possible.
- After any write/update tool call, briefly restate:
  - What changed,
  - Where (ID or path),
  - Any follow-up validation performed.
</tool_usage_rules>
```

### Parallel Tool Calls

- GPT-5.5 supports parallel function calls — invoking multiple tools in a single model pass
- Do not rely on `none` for multi-step tool workflows; use `low` or higher when planning, search, or chained tools
  matter
- OpenAI measures parallelization efficiency via **tool yields**: if 3 tools are called in parallel, followed by 3 more
  in parallel, the number of yields is 2 (a better latency proxy than raw tool call count)

---

## Structured Extraction

For extraction, prompts should define the schema, missing-field behavior, and completeness check.

1. Always provide a schema or JSON shape
2. Use structured outputs for strict schema adherence
3. Distinguish required vs optional fields
4. Ask for "extraction completeness"
5. Handle missing fields explicitly

```xml
<extraction_spec>
You will extract structured data from tables/PDFs/emails into JSON.
- Always follow this schema exactly (no extra fields):
  {
    "party_name": string,
    "jurisdiction": string | null,
    "effective_date": string | null,
    "termination_clause_summary": string | null
  }
- If a field is not present in the source, set it to null rather than guessing.
- Before returning, quickly re-scan the source for any missed fields and correct omissions.
</extraction_spec>
```

**New in GPT-5.5:** You can define tools with `type: custom` to enable models to send plaintext inputs directly to
tools, rather than being limited to structured JSON.

---

## Web Search and Research

GPT-5.5 is more steerable at synthesizing across many sources. Knowledge cutoff: **December 1, 2025**.

### Research Agent Prompt

```xml
<web_search_rules>
- Act as an expert research assistant; default to comprehensive, well-structured answers.
- Prefer web research over assumptions whenever facts may be uncertain or incomplete.
- Include citations for all web-derived information.
- Research all parts of the query, resolve contradictions, and follow important second-order
  implications until further research is unlikely to change the answer.
- Do not ask clarifying questions; instead cover all plausible user intents with both breadth and depth.
- Write clearly and directly using Markdown (headers, bullets, tables when helpful).
- Define acronyms, use concrete examples, and keep a natural, conversational tone.
</web_search_rules>
```

### Search Modes

| Mode           | Use Case                                    |
| -------------- | ------------------------------------------- |
| Non-reasoning  | Quick lookups, completes in seconds         |
| Agentic search | Iterative reasoning with follow-up searches |
| Deep research  | Exhaustive investigations, takes minutes    |

**Tip:** Using hints like "go deep" triggers more thorough research.

---

## Responses API

GPT-5.5 is designed around the **Responses API** for reasoning, tool-calling, and multi-turn use cases.

| Feature                     | Chat Completions | Responses API |
| --------------------------- | ---------------- | ------------- |
| Basic text generation       | Yes              | Yes           |
| Reasoning item preservation | No               | Yes           |
| `previous_response_id`      | No               | Yes           |

**Why Responses API matters:** It preserves reasoning items across turns, which improves multi-step tool workflows and
can reduce redundant reasoning. If you manually replay assistant output items, preserve returned reasoning and `phase`
items unchanged.

---

## Migration Guide to GPT-5.5

### Migration Mapping

| Current Model | Target  | Reasoning Effort   | Notes                               |
| ------------- | ------- | ------------------ | ----------------------------------- |
| GPT-5.2       | GPT-5.5 | Default (drop-in)  | Just change the model name          |
| GPT-5.3-Codex | GPT-5.5 | Default            | GPT-5.5 subsumes Codex capabilities |
| o3            | GPT-5.5 | `medium` or `high` | For reasoning-heavy workloads       |
| GPT-4.1       | GPT-5.5 | `none`             | Treat as fast/low-deliberation      |
| GPT-4o        | GPT-5.5 | `none`             | Same as GPT-4.1                     |

### Migration Steps

1. **Switch models, don't change prompts yet** — Test model change in isolation
2. **Pin `reasoning.effort`** — Match prior model's latency/depth profile
3. **Run evals for baseline** — If results look good, ready to ship
4. **If regressions, try an initiative nudge first** — Before raising reasoning effort
5. **If still regressing, tune the prompt** — Use Prompt Optimizer + targeted constraints
6. **Re-run evals after each small change** — Iterate incrementally

### Prompt Optimizer

OpenAI's [Prompt Optimizer](https://platform.openai.com/chat/edit?optimize=true) in Playground helps:

- Quickly improve existing prompts for GPT-5.5
- Migrate across GPT-5 models
- Remove common failure modes

---

## Complete Example: Enterprise Agent System Prompt

```xml
<role>
You are a GPT-5.5 enterprise assistant for [DOMAIN].
You are precise, analytical, persistent, and disciplined.
</role>

<output_contract>
- Define the exact output shape for each task type.
- Task is complete when [explicit completion criteria].
- If completion criteria cannot be met, explain what is missing and what would unblock it.
</output_contract>

<output_verbosity_spec>
- Default: 3-6 sentences or <=5 bullets for typical answers.
- For simple questions: <=2 sentences.
- For complex tasks: 1 overview paragraph + <=5 tagged bullets
  (What changed, Where, Risks, Next steps, Open questions).
- Do not rephrase the user's request unless it changes semantics.
</output_verbosity_spec>

<design_and_scope_constraints>
- Implement EXACTLY and ONLY what the user requests.
- No extra features, no added components, no embellishments.
- If instruction is ambiguous, choose the simplest valid interpretation.
</design_and_scope_constraints>

<initiative>
- Do not stop at the first plausible answer.
- Look for second-order issues, edge cases, and missing constraints.
- If safety or accuracy critical, perform at least one verification step.
</initiative>

<uncertainty_and_ambiguity>
- If ambiguous: ask 1-3 clarifying questions OR present 2-3 interpretations with labeled assumptions.
- Never fabricate exact figures or references when uncertain.
- Prefer "Based on the provided context..." over absolute claims.
</uncertainty_and_ambiguity>

<tool_usage_rules>
- Prefer tools over internal knowledge for fresh/user-specific data.
- Parallelize independent reads when possible.
- Before calling a tool, explain in one sentence why you are calling it.
- After write/update: restate what changed, where, and validation performed.
</tool_usage_rules>

<user_updates_spec>
- Brief updates (1-2 sentences) only when starting new phase or plan changes.
- Avoid narrating routine tool calls.
- Each update must include concrete outcome.
- Do not expand task beyond what user asked.
</user_updates_spec>

<high_risk_self_check>
Before finalizing in legal/financial/compliance/safety contexts:
- Re-scan for unstated assumptions, ungrounded claims, overly strong language.
- Soften or qualify as needed.
</high_risk_self_check>
```

---

## Key Differences: GPT-5.5 vs GPT-5.2 vs Gemini 3.1 Pro

| Aspect                | GPT-5.5                                  | GPT-5.2                   | Gemini 3.1 Pro             |
| --------------------- | ---------------------------------------- | ------------------------- | -------------------------- |
| Default reasoning     | `medium`                                 | `none`                    | `high` (dynamic)           |
| Default verbosity     | Direct, controllable via API             | Low, prompt-controlled    | Concise                    |
| Context window        | 1.05M tokens                             | 400K tokens               | 1M tokens                  |
| Temperature           | Flexible                                 | Flexible                  | Keep at 1.0                |
| Structured extraction | Strong (+ custom tool types)             | Strong                    | Good                       |
| Tool prompting        | Define triggers, evidence, stop rules    | May need more scaffolding | Use direct tool rules      |
| Multi-turn state      | `previous_response_id` / reasoning items | Responses state           | Thought signatures         |
| Knowledge cutoff      | Dec 1, 2025                              | August 2025               | January 2025               |
| Best for              | Agentic, coding, professional work       | Enterprise, document      | Reasoning, multimodal work |

---

## Pro Tips

1. **Define output contracts first** — Explicit completion criteria are the highest-leverage prompt change for GPT-5.5

2. **Use initiative nudges before raising reasoning effort** — Cheaper and often more effective

3. **Set `verbosity` at the API level** — Separate text brevity from code verbosity (Cursor pattern)

4. **Enable preambles for tool-use transparency** — "Before calling a tool, explain why" boosts accuracy

5. **Tune `reasoning.effort` by task shape** — Lower to `none` for execution-heavy workloads; raise to `high` for
   complex multi-step problems

6. **Anchor long-context answers** — Reference specific sections even with 1.05M context available

7. **Migration is incremental** — One change at a time; model first, then reasoning effort, then prompt

8. **Parallelize tool calls** — Use `low` or higher for multi-step tool planning; measure tool yields, not raw calls

---

## Sources

- [OpenAI: Prompt Guidance for GPT-5.5](https://developers.openai.com/api/docs/guides/prompt-guidance)
- [OpenAI: Using GPT-5.5](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI: GPT-5.5 Model](https://developers.openai.com/api/docs/models/gpt-5.5)
- [OpenAI: Introducing GPT-5.5](https://openai.com/index/introducing-gpt-5-5/)
- [OpenAI: Reasoning Models](https://developers.openai.com/api/docs/guides/reasoning)
- [OpenAI: Reasoning Best Practices](https://developers.openai.com/api/docs/guides/reasoning-best-practices)
- [OpenAI Cookbook: GPT-5 Prompting Guide](https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_prompting_guide)
- [OpenAI Cookbook: GPT-5 New Params and Tools](https://cookbook.openai.com/examples/gpt-5/gpt-5_new_params_and_tools)
