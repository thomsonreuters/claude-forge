# Claude 4.7 Prompting Guide (Opus 4.7)

> Synthesized from
> [Anthropic Claude Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices),
> [What's New in Claude Opus 4.7](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7),
> [Anthropic Migration Guide](https://platform.claude.com/docs/en/about-claude/models/migration-guide),
> [Anthropic Effort](https://platform.claude.com/docs/en/build-with-claude/effort),
> [Anthropic Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking), and
> [Anthropic Task Budgets](https://platform.claude.com/docs/en/build-with-claude/task-budgets). May 2026.

## Overview

Claude 4.7 currently means **Claude Opus 4.7**. As of May 2026, Anthropic has not released Sonnet 4.7 or Haiku 4.7. Use
Sonnet 4.6 for the best speed/intelligence tradeoff and Haiku 4.5 for low-latency work.

Claude Opus 4.7 was released April 16, 2026 as Anthropic's most capable generally available model for complex reasoning
and agentic coding. It keeps the 1M context window and 128K synchronous Messages API max output of Opus 4.6, while
adding 300K Batch API output through a beta header. It changes how prompts should be tuned:

- **More literal instruction following** - instructions are applied exactly, especially at lower effort
- **New `xhigh` effort level** - recommended for most coding and agentic workloads
- **Strict effort behavior** - `low` and `medium` really mean scoped, cost-sensitive work
- **Extended thinking mode removed** - any `thinking: {"type": "enabled"}` configuration returns 400
- **Adaptive thinking off by default** - explicitly set `thinking: {"type": "adaptive"}` to enable thinking
- **Sampling parameters removed** - non-default `temperature`, `top_p`, or `top_k` returns 400
- **Task budgets beta** - advisory token budget across a full agentic loop
- **300K Batch API output beta** - Message Batches can exceed the standard 128K synchronous output cap
- **Thinking content omitted by default** - opt into summarized thinking display if your product needs it
- **Updated tokenizer** - same text can use up to about 35% more tokens than Opus 4.6
- **Fewer tool calls and subagents by default** - more internal reasoning, less automatic fan-out

**Key mindset shift:** Claude Opus 4.7 is more capable, more autonomous, and more literal. Do not rely on vague "be
thorough" prompting or inherited 4.6 scaffolding. State the exact scope, choose effort deliberately, and use prompt
language to decide when the model should reason, use tools, spawn subagents, or report every finding.

### Model Selection

| Model          | Best For                                                                       |
| -------------- | ------------------------------------------------------------------------------ |
| **Opus 4.7**   | Hard coding, long-horizon agents, code review, and large-context reasoning     |
| **Sonnet 4.6** | Default for fast, cost-efficient coding, analysis, and everyday tool workflows |
| **Haiku 4.5**  | Lowest-latency routing, extraction, classification, and simple tool calls      |

**Rule of thumb:** Use Sonnet 4.6 by default for ordinary work. Use Opus 4.7 when failure is expensive, the task spans
many files or many steps, the model must verify its own work, or code-review recall matters more than cost.

## Core API Parameters

### Model ID

```python
response = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=64000,
    thinking={"type": "adaptive"},
    output_config={"effort": "xhigh"},
    messages=[{"role": "user", "content": "Audit this codebase for correctness bugs."}],
)
```

### Adaptive Thinking

Claude Opus 4.7 supports **adaptive thinking only**. The old `enabled` extended thinking mode is rejected:

```python
# Before: Opus 4.6. Not accepted on Opus 4.7, even without budget_tokens.
thinking = {"type": "enabled", "budget_tokens": 32000}

# After: Opus 4.7
thinking = {"type": "adaptive"}
output_config = {"effort": "high"}
```

Important details:

- Adaptive thinking is **off by default** on Opus 4.7. Omit `thinking` only when you want the lowest latency.
- Any `thinking: {"type": "enabled"}` configuration returns a 400 error, with or without `budget_tokens`.
- Interleaved thinking is automatically enabled with adaptive mode.
- Thinking and final output share the `max_tokens` cap, so large effort levels need a large `max_tokens`.
- If `stop_reason: "max_tokens"` appears, increase `max_tokens`, lower effort, or narrow the task.

### Effort Levels

| Level    | Behavior                                                                           | When to use                                                   |
| -------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `low`    | Most efficient. Strictly scoped. May under-think moderately complex work.          | Small edits, simple routing, cheap subagents                  |
| `medium` | Balanced cost/performance. More literal and narrower than Opus 4.6 medium.         | Cost-sensitive agents, scoped analysis                        |
| `high`   | High capability. Same as omitting effort. Minimum for intelligence-sensitive work. | Complex Q&A, difficult coding, review, large-context analysis |
| `xhigh`  | **New in 4.7.** Extended capability for long-horizon work.                         | Most coding and agentic workflows                             |
| `max`    | Maximum capability. Can show diminishing returns and overthinking.                 | Hardest architecture, deep audits, final verification         |

**Default:** `high`.

**Anthropic's 4.7 guidance:**

- Start with `xhigh` for coding and agentic workflows.
- Use at least `high` for most intelligence-sensitive tasks.
- Use `medium` or `low` only when latency/cost matters more than depth.
- Test `max` for the hardest work, but measure whether it improves quality enough to justify token use.
- At `xhigh` or `max`, start with `max_tokens` of at least 64K and tune from there.

**Critical difference from 4.6:** Opus 4.7 respects low effort strictly. If you run complex work at `low` or `medium`
and observe shallow reasoning, raise effort first. If you must keep low effort for latency, targeted reasoning guidance
can help:

```
This task involves multi-step reasoning. Think carefully through the problem before responding.
```

That sentence was often counterproductive on 4.6 because effort already controlled thinking depth. On 4.7, it is useful
only as a narrow low-effort rescue, not as a blanket system-prompt habit.

### Steering Thinking Frequency

Large system prompts can make Opus 4.7 think more often than needed. Steer adaptive thinking directly:

```
Thinking adds latency and should only be used when it will meaningfully improve answer quality,
typically for problems that require multi-step reasoning. When in doubt, respond directly.
```

Conversely, if `medium` effort under-thinks hard work, raise effort before adding more prompt scaffolding.

### Thinking Display

Thinking content is omitted by default on Opus 4.7. Claude may still think, and those thinking tokens are still billed,
but the thinking text is not returned in the API response unless you opt in:

```python
thinking = {
    "type": "adaptive",
    "display": "summarized",  # or "omitted" (default)
}
```

Use `"summarized"` if your product streams reasoning or needs visible progress during a long pause before text output.
Billing is still based on generated tokens.

### Sampling Parameters

Starting with Claude Opus 4.7, do **not** set non-default sampling parameters:

| Parameter     | 4.7 Behavior                  | Migration                          |
| ------------- | ----------------------------- | ---------------------------------- |
| `temperature` | Non-default values return 400 | Omit it. Use prompt instructions.  |
| `top_p`       | Non-default values return 400 | Omit it. Use examples/constraints. |
| `top_k`       | Non-default values return 400 | Omit it.                           |

If you previously used `temperature = 0` for determinism, remove it. Anthropic notes that it never guaranteed identical
outputs. Use stricter output formats, examples, and validation instead.

### Context Window, Output, and Cutoff

| Model      | Context Window | Max Output (Messages API) | Reliable Knowledge Cutoff | Training Data Cutoff |
| ---------- | -------------- | ------------------------- | ------------------------- | -------------------- |
| Opus 4.7   | 1M tokens      | 128K                      | Jan 2026                  | Jan 2026             |
| Sonnet 4.6 | 1M tokens      | 64K                       | May 2025                  | May 2025             |
| Haiku 4.5  | 200K tokens    | 64K                       | Feb 2025                  | Jul 2025             |

Opus 4.7's 1M context window is available at standard API pricing with no long-context premium.

### Batch API Extended Output (Beta)

On the Message Batches API, Claude Opus 4.7, Opus 4.6, and Sonnet 4.6 support up to **300K output tokens** with the
`output-300k-2026-03-24` beta header.

Important constraints:

- Extended output is available only on the **Message Batches API**, not the synchronous Messages API.
- It is supported on the Claude API, not Amazon Bedrock, Vertex AI, or Microsoft Foundry.
- A single 300K-token generation can take over an hour; plan around the 24-hour batch processing window.
- Use it for long-form generation, exhaustive extraction, large scaffolds, or long reasoning chains.

### Tokenization Change

Opus 4.7 uses a new tokenizer. Anthropic says token counts can be up to about 35% higher than Opus 4.6, depending on
content, so assume some increase until representative requests prove otherwise. Consequences:

- Re-run `/v1/messages/count_tokens` against representative requests.
- Add headroom to `max_tokens`, especially near compaction triggers.
- Re-baseline cost and latency before assuming old Opus 4.6 budgets still apply.
- Use effort, task budgets, and concise prompts as the main cost levers.

---

## Task Budgets (Beta)

Task budgets are available through the `task-budgets-2026-03-13` beta header. They give Claude an advisory token budget
for a full agentic loop, including thinking, tool calls, tool results, and final output.

- You need the model to self-regulate token spend on long agentic tasks.
- You have predictable cost or latency ceilings.
- You want graceful wrap-up instead of `max_tokens` truncation.
- Your agent loop has multiple tool calls and decisions before the next human response.

Practical rules:

- Minimum `task_budget.total` is 20K tokens; smaller values return 400.
- A task budget is **not a hard cap**. `max_tokens` is still the hard per-request ceiling.
- At `xhigh` or `max`, set `max_tokens` to at least 64K.
- Effort tunes reasoning depth per step; task budget tunes total breadth across the loop.
- Use task budgets only after measuring representative task lengths.
- Do not confuse task budgets with the Batch API's 300K extended-output beta.

---

## Context Compaction (Beta)

Server-side compaction remains the recommended context-management strategy for long-running conversations and agentic
workflows that approach context limits. It is especially important on Opus 4.7 because the new tokenizer can change
token counts and task budgets may need to survive multiple compacted turns.

Enable it with the beta header `compact-2026-01-12` and a `context_management.edits` entry:

```python
response = client.beta.messages.create(
    model="claude-opus-4-7",
    max_tokens=4096,
    messages=messages,
    context_management={"edits": [{"type": "compact_20260112"}]},
    betas=["compact-2026-01-12"],
)
```

### Practical Rules

- Default trigger: 150K input tokens.
- Minimum trigger: 50K input tokens.
- Compaction adds a separate sampling step and is billed separately.
- When compaction occurs, cost/audit logic must aggregate `usage.iterations`, not only top-level usage fields.
- `/v1/messages/count_tokens` applies existing compaction blocks but does not trigger new compactions.
- If combining compaction with task budgets, preserve the remaining task budget across the compacted loop.
- Use custom compaction instructions only when the default summary loses domain-critical state.

---

## Key Behavioral Differences from Claude 4.6

| Aspect                | Claude Opus 4.7 Behavior                                                                 |
| --------------------- | ---------------------------------------------------------------------------------------- |
| Model lineup          | Opus 4.7 only. No Sonnet 4.7 or Haiku 4.7 yet.                                           |
| Reasoning control     | Adaptive thinking only; `thinking: {"type": "enabled"}` rejected.                        |
| Effort                | Adds `xhigh`; `low`/`medium` are stricter and can under-think hard tasks.                |
| Sampling              | Non-default `temperature`, `top_p`, `top_k` return 400.                                  |
| Token usage           | New tokenizer can count 1.0x to 1.35x vs 4.6; higher effort thinks more in later turns.  |
| Response length       | Calibrates to perceived task complexity, not a fixed default verbosity.                  |
| Instruction following | More literal; will not silently generalize instructions across items.                    |
| Tool use              | Fewer tool calls by default; raising effort increases tool use.                          |
| Subagents             | Fewer spawned by default; fan-out rules must be explicit.                                |
| Progress updates      | More regular and higher quality; remove forced-update scaffolding before retuning.       |
| Tone                  | More direct and opinionated; less validation-forward than 4.6.                           |
| Frontend design       | Better design instincts but persistent cream/serif/terracotta default without direction. |
| Code review           | Higher bug-finding capability, but strict prompts may suppress lower-severity findings.  |

---

## XML Tags

Claude remains strongly responsive to XML-style tags. Use descriptive tags and avoid mixing instructions with data.

```xml
<role>
You are a senior software engineer specializing in distributed systems and code review.
</role>

<task>
Review the patch for correctness bugs, missing tests, and architecture risks.
</task>

<scope>
Review every changed file and any directly connected caller/callee needed to validate behavior.
</scope>

<tool_usage>
Use tools to inspect files before making claims. Parallelize independent reads and searches.
</tool_usage>

<output_format>
Return findings first, ordered by severity. Each finding must include file, line, severity, and evidence.
</output_format>
```

Use a small number of examples for format and tone. Keep them diverse enough that the model does not overfit.

---

## Tool Use and Agentic Workflows

### Fewer Tool Calls by Default

Opus 4.7 often reasons more and calls fewer tools than Opus 4.6. This can improve quality per tool call, but it can hurt
workflows where ground truth lives outside the model. Increase tool use by:

1. Raising effort to `high` or `xhigh`.
2. Explaining exactly when tools are required.
3. Making evidence requirements part of the output contract.

```xml
<evidence_contract>
Every claim about the repository must be backed by a file path, line reference, command output, or explicit statement
that the claim is an inference from the inspected evidence.
</evidence_contract>
```

### User-Facing Progress Updates

Opus 4.7 gives better updates during long agentic traces. Remove old scaffolding such as "after every 3 tool calls,
summarize progress" and re-baseline. If the defaults still do not fit:

```xml
<progress_updates>
- Send a brief update when starting a new phase, discovering a blocker, or changing the plan.
- Keep updates to 1-2 sentences.
- Include a concrete finding or next action.
- Do not narrate routine file reads or ordinary tool calls.
</progress_updates>
```

### Interactive Coding Products

Opus 4.7 may use more tokens in interactive coding than in autonomous one-shot tasks because it reasons after user
turns. Anthropic recommends:

- Use `xhigh` or `high` effort for coding products.
- Prefer autonomous modes that reduce back-and-forth.
- Put the full task, intent, constraints, and acceptance criteria in the first human turn.
- Avoid progressive underspecified instructions when token efficiency matters.

## Prompting Principles for 4.7

### 1. Be Literal About Scope

Opus 4.7 will not infer broad scope from a narrow instruction. If a rule applies everywhere, say so:

```xml
<scope>
Apply these formatting rules to every section, every generated table, and every file you edit.
Do not apply them only to the first example.
</scope>
```

Avoid repeated emphasis. One clear instruction is usually enough. Repetition can make the model over-weight a rule and
ignore nuance.

This is the biggest prompting change from Opus 4.6: earlier Claude models often generalized a rule stated once. Opus 4.7
is more likely to apply the rule exactly where you named it and stop there.

### 2. Replace Vague Thoroughness with Effort

Prefer API controls:

```python
output_config = {"effort": "xhigh"}
```

Use prompt-level reasoning guidance only when it states a decision rule:

```xml
<thinking_policy>
Think before answering when the task requires multi-step reasoning, cross-file analysis, security review,
or reconciling contradictory evidence. For simple factual or mechanical requests, respond directly.
</thinking_policy>
```

### 3. Define Tool Triggers

Because Opus 4.7 uses tools less often by default, tell it when tools are required:

```xml
<tool_usage>
- Use tools before making claims about files, logs, tickets, URLs, current events, or user-specific data.
- Search/read in parallel when the needed evidence is independent.
- If a tool fails, inspect the error and try the next most direct path before asking the user.
- Do not rely on memory for codebase facts that can be opened or searched.
</tool_usage>
```

### 4. Tell It When to Fan Out

Opus 4.7 spawns fewer subagents by default. Be explicit:

```xml
<delegation_policy>
- Do not spawn a subagent for work you can complete directly in one response.
- Spawn multiple subagents in the same turn when fanning out across independent files, hypotheses, or review domains.
- Each subagent must own a distinct scope and report changed files or evidence paths.
- Do not assign two subagents to edit the same file unless explicitly coordinating the merge.
</delegation_policy>
```

### 5. Specify Verbosity Positively

Opus 4.7 varies response length by perceived complexity. If your product needs a stable style, show the target:

```xml
<response_style>
Provide concise, focused responses. Skip non-essential context, keep examples minimal, and lead with the answer.
For complex analysis, use one short overview paragraph followed by no more than five bullets.
</response_style>
```

Positive examples work better than long lists of "do not over-explain" prohibitions.

### 6. Tune Tone Explicitly

Opus 4.7 is more direct and opinionated than Opus 4.6. If you need warmth:

```xml
<tone>
Use a warm, collaborative tone. Acknowledge the user's framing briefly before answering.
Stay direct, but do not sound brusque.
</tone>
```

If you need a neutral enterprise voice, ask for it directly rather than relying on lower temperature.

---

## Preventing Overengineering

Opus 4.7's literal instruction following makes scope constraints more reliable, not less necessary. State exactly what
kind of initiative is welcome and what counts as scope drift:

```xml
<scope_constraints>
- Implement only the behavior the user requested or behavior clearly required to make that request work.
- Preserve existing architecture, naming, and style unless changing them is necessary for correctness.
- Do not add optional features, new configuration, new dependencies, or broad abstractions without an explicit reason.
- If the request is ambiguous, choose the smallest valid interpretation and state the assumption.
- If you notice useful follow-up work outside scope, mention it separately instead of implementing it.
</scope_constraints>
```

For code review, invert the constraint: broad discovery is good, but remediation should stay scoped:

```xml
<review_scope>
Report all plausible correctness, security, compatibility, and test-coverage issues.
Do not propose unrelated refactors or style preferences unless they directly affect the reviewed behavior.
</review_scope>
```

---

## Code Review Harnesses

This is one of the most important 4.7-specific changes for review workflows.

Opus 4.7 is better at finding bugs, but older prompts can accidentally reduce reported recall. When a prompt says "only
report high-severity issues," "be conservative," or "do not nitpick," Opus 4.7 may identify a real bug and then silently
drop it because it judges the issue below the stated bar.

For a finding-discovery pass, use coverage-oriented language:

```xml
<finding_policy>
Report every issue you find, including ones you are uncertain about or consider low-severity.
Do not filter for importance or confidence at this stage; a separate verification step will do that.
Your goal is coverage: it is better to surface a finding that later gets filtered out than to silently drop a real bug.
For each finding, include confidence and estimated severity.
</finding_policy>
```

If the review is single-pass and must self-filter, define the bar concretely:

```xml
<reporting_threshold>
Report any bug that could cause incorrect behavior, a test failure, data loss, security exposure, user-visible
misleading output, or broken compatibility. Omit only pure style, naming, or preference nits.
</reporting_threshold>
```

For review prompts, pair the finding policy with investigation rules: inspect changed files and directly connected code,
avoid claims about unopened code, cite exact paths/lines, and return findings first with severity and confidence.

---

## Frontend and Design Defaults

Opus 4.7 has stronger design instincts than Opus 4.6, but it tends to default to warm editorial styling:

Generic negative instructions are weak:

```
Do not use cream. Make it clean and minimal.
```

Specify domain, palette, typography, density, and interaction style instead:

```xml
<visual_direction>
Build a dense enterprise operations UI, not an editorial landing page.
Use a cool neutral palette: #F5F7F8, #D7DEE2, #6F7D86, #24313A, #0E1419.
Use a compact sans-serif, 4px radii, restrained shadows, and information-first layout.
Avoid warm cream backgrounds, serif display type, italic accents, and terracotta/amber accents.
</visual_direction>
```

When the visual direction is underspecified, ask for options before building:

```xml
<design_process>
Propose 3 visual directions tailored to the product domain.
For each, include palette, type style, density, and one-line rationale.
Wait for selection before implementation.
</design_process>
```

Shorter aesthetic prompts often work better than long anti-generic scaffolding, as long as the direction is concrete.

---

## Long Context

For long-context prompts, put source material first, put the task at the end, wrap multiple documents in explicit source
tags, and ground claims in source names or short quotes.

```
[Long documents, code, logs, transcripts]

Based on the material above, answer the following:
[Specific task]
```

4.7-specific rules:

- Prefer precise file ranges and relevant excerpts over entire folders.
- Use compaction for long conversations, but preserve decisions, unresolved tasks, and file ownership.
- Keep broad instructions explicit about every source or section they apply to; 4.7 will not reliably generalize them.
- Re-run token counting after migrating to 4.7 because tokenization changed.

---

## Structured Outputs and Prefilling

Assistant message prefilling remains unavailable. Use structured outputs, tools, and direct prompt instructions instead:

| Previous Pattern                 | 4.7 Pattern                                   |
| -------------------------------- | --------------------------------------------- |
| Prefill `{` for JSON             | `output_config.format` or structured outputs  |
| Prefill enum/classification text | Tool with enum field or strict schema         |
| Prefill to skip preamble         | "Respond directly without preamble" in prompt |

For classification or extraction, define required fields, enum values, missing-field behavior, and validation rules in
the schema rather than relying on prose alone.

---

## Migration from Claude 4.6

### Breaking Changes

| Change                           | Impact                                                     |
| -------------------------------- | ---------------------------------------------------------- |
| Extended thinking mode removed   | Any `thinking: {"type": "enabled"}` config returns 400     |
| Adaptive thinking off by default | Requests without `thinking` run without thinking           |
| Sampling params removed          | Non-default `temperature`, `top_p`, or `top_k` returns 400 |
| Thinking omitted by default      | Reasoning streams may look like a pause unless summarized  |
| Token counting changed           | Same text may count 1.0x to 1.35x as many tokens           |

### What Changed

| Aspect             | Claude Opus 4.6                         | Claude Opus 4.7                                   |
| ------------------ | --------------------------------------- | ------------------------------------------------- |
| Model availability | Opus and Sonnet 4.6                     | Opus 4.7 only                                     |
| Thinking modes     | Adaptive recommended; manual deprecated | Adaptive only; manual rejected                    |
| Effort levels      | low, medium, high, max                  | low, medium, high, xhigh, max                     |
| Effort behavior    | More forgiving at low/medium            | Lower effort strictly narrows work                |
| Sampling           | Temp/top_p restriction from Claude 4+   | Non-default temp/top_p/top_k rejected             |
| Tool use           | More tool-aggressive                    | Fewer tools by default; effort increases tool use |
| Subagents          | More willing to fan out                 | Fewer by default; prompt explicit fan-out rules   |
| Design default     | Strong but more generic                 | More tasteful, with persistent house style        |
| Tokenizer          | 4.6 tokenizer                           | New tokenizer, different counts                   |

### Migration Checklist

01. **Change model ID** to `claude-opus-4-7`.
02. **Remove `thinking: {"type": "enabled"}` entirely** and use adaptive thinking when thinking is needed.
03. **Set effort deliberately**: start `xhigh` for coding/agents, `high` for intelligence-sensitive work.
04. **Omit sampling parameters**: remove non-default `temperature`, `top_p`, and `top_k`.
05. **Raise `max_tokens` headroom**: start at 64K for `xhigh`/`max`, especially tool-heavy workflows.
06. **Decide thinking display**: set `display: "summarized"` if users need visible reasoning progress.
07. **Re-run token counts** against real prompts and adjust cost expectations.
08. **Remove old progress-update scaffolding** and re-baseline.
09. **Audit literal instructions**: ensure broad rules say exactly what they apply to.
10. **Retune code-review thresholds** so 4.7 does not silently drop lower-severity findings.
11. **Add tool/subagent triggers** for workflows that require evidence gathering or fan-out.
12. **Use task budgets only after measuring** representative task token usage.

---

## Complete Example: 4.7 Coding and Review Assistant

```xml
<role>
You are a senior software engineer and code reviewer.
</role>

<scope>
Resolve the user's requested task end to end. Keep edits scoped to the request and existing architecture.
</scope>

<thinking_policy>
Think before answering for cross-file analysis, debugging, security review, or conflicting evidence.
For simple mechanical tasks, respond directly.
</thinking_policy>

<tool_usage>
- Use tools before making claims about repository files, logs, tests, URLs, or current facts.
- Parallelize independent reads and searches.
- After edits, run the most relevant verification command available.
</tool_usage>

<delegation_policy>
- Work directly for small or tightly coupled tasks.
- Spawn subagents only when independent scopes can proceed in parallel.
</delegation_policy>

<review_policy>
Report every plausible correctness, security, compatibility, or test-coverage issue.
Include severity, confidence, and exact file:line evidence.
</review_policy>

<output_format>
Findings first for reviews. For code changes, summarize what changed, files touched, verification, and remaining risk.
</output_format>
```

Recommended API pairings:

```python
# hard coding/review
thinking={"type": "adaptive", "display": "omitted"}
output_config={"effort": "xhigh"}
max_tokens=64000

# cost-bounded agentic loop
thinking={"type": "adaptive"}
output_config={"effort": "high", "task_budget": {"type": "tokens", "total": 96000}}
betas=["task-budgets-2026-03-13"]
```

---

## Key Differences: Claude 4.7 vs GPT-5.5 vs Gemini 3.1 Pro

| Aspect                 | Claude Opus 4.7                                | GPT-5.5                          | Gemini 3.1 Pro                   |
| ---------------------- | ---------------------------------------------- | -------------------------------- | -------------------------------- |
| Default reasoning      | Off unless `thinking` set; effort high         | `reasoning.effort` medium        | Dynamic thinking high            |
| Thinking control       | Adaptive + effort; no manual budgets           | `reasoning.effort` none to xhigh | `thinking_level` low/medium/high |
| Best effort for coding | `xhigh`                                        | Medium/high/xhigh by task        | High default; low for latency    |
| Token budget control   | Task budgets beta across agentic loop          | Reasoning + verbosity controls   | Thinking level + max output      |
| Sampling controls      | Omit temp/top_p/top_k                          | Flexible                         | Keep temperature at 1.0          |
| Context window         | 1M                                             | 1.05M                            | 1M                               |
| Max output             | 128K sync; 300K batch beta                     | 128K                             | 64K                              |
| Tool behavior          | Fewer calls by default, higher quality         | Functions, search, CUA, MCP      | Functions; Search/File/Code/URL  |
| Structured tags        | XML strongly preferred                         | XML preferred                    | XML or Markdown, not both        |
| Knowledge cutoff       | Reliable Jan 2026                              | Dec 1, 2025                      | January 2025                     |
| Best for               | Hard coding, review, agents, long-context work | Agentic professional work        | Reasoning and multimodal         |

---

## Pro Tips

01. **Use Opus 4.7 only where it pays off** - hard coding, code review, long agents, and large-context reasoning.

02. **Start coding agents at `xhigh`** - Anthropic recommends it for coding and agentic use cases; use at least `high`
    for intelligence-sensitive work.

03. **Enable adaptive thinking explicitly** - no `thinking` field means no thinking; omit `temperature`, `top_p`, and
    `top_k`.

04. **Treat `low` and `medium` as strict** - they save cost but can under-think moderately complex work.

05. **Use `max_tokens >= 64K` for `xhigh`/`max`** - thinking, tools, and output share the cap.

06. **Re-baseline before adding scaffolding** - 4.7 often no longer needs 4.6-era progress or validation text.

07. **Make scope global when it is global** - 4.7 applies instructions literally, so name every target the rule covers.

08. **For review, optimize discovery before filtering** - otherwise conservative prompts can hide real bugs.

09. **Prompt tool and subagent triggers** - if evidence or fan-out matters, specify exactly when to use them.

10. **Measure tokens after migration** - tokenizer counts changed; use compaction, task budgets, and Batch API extended
    output deliberately.

11. **Specify frontend style concretely** - otherwise Opus 4.7 may drift into its cream/serif/terracotta house style.

---

## Sources

- [Anthropic: Prompting Best Practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Anthropic: What's New in Claude Opus 4.7](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7)
- [Anthropic: Migration Guide](https://platform.claude.com/docs/en/about-claude/models/migration-guide)
- [Anthropic: Models Overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Anthropic: Batch Processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- [Anthropic: Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Anthropic: Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Anthropic: Task Budgets](https://platform.claude.com/docs/en/build-with-claude/task-budgets)
- [Anthropic: Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)
- [Anthropic: Introducing Claude Opus 4.7](https://www.anthropic.com/news/claude-opus-4-7)
- [Claude: Working with Claude Opus 4.7](https://claude.com/resources/tutorials/working-with-claude-opus-4-7)
- [OpenAI: GPT-5.5 Model](https://developers.openai.com/api/docs/models/gpt-5.5)
- [OpenAI: Models](https://developers.openai.com/api/docs/models)
- [Google DeepMind: Gemini 3.1 Pro](https://deepmind.google/models/gemini/pro/)
- [Google DeepMind: Gemini 3.1 Pro Model Card](https://deepmind.google/models/model-cards/gemini-3-1-pro/)
- [Google AI: Gemini 3 Developer Guide](https://ai.google.dev/gemini-api/docs/gemini-3)
- [Google AI: Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking)
