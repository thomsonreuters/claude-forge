# Claude 4.6 Prompting Guide (Opus 4.6 / Sonnet 4.6)

> Synthesized from
> [Anthropic Claude Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices),
> [What's New in Claude 4.6](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-6),
> [Anthropic Engineering Blog](https://www.anthropic.com/engineering), and web research. February 2026.

## Overview

Claude 4.6 models (Opus 4.6, Sonnet 4.6) are Anthropic's frontier models, released February 2026. Key advances over
Claude 4.5:

- **1M token context window** at standard pricing (up from 200K standard / 1M beta)
- **Adaptive thinking** — Claude dynamically decides when and how much to think
- **Effort parameter** — `low` / `medium` / `high` / `max` (Opus only) replaces `budget_tokens`
- **Context compaction** — server-side automatic summarization (beta)
- **76% on 8-needle MRCR v2** (vs 18.5% for Sonnet 4.5) — qualitative leap in long-context reasoning
- **Prefilling removed** — assistant message prefilling returns 400 error on 4.6 models

**Key mindset shift:** Claude 4.6 models follow instructions precisely and **think adaptively**. The effort parameter
replaces prompt-level workarounds ("think carefully", "be thorough") which can now cause overthinking loops. Use the
effort parameter as the primary lever for reasoning depth.

### Model Selection

| Model          | Best For                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------ |
| **Opus 4.6**   | Hardest problems: large-scale migrations, deep research, extended autonomous work          |
| **Sonnet 4.6** | 80%+ of tasks: fast turnaround, cost-efficient, 98% of Opus coding quality at 1/5 the cost |
| **Haiku 4.5**  | Fast, cost-effective. Straightforward tools. (No 4.6 version yet)                          |

**Rule of thumb:** Use Sonnet 4.6 by default. Reach for Opus only for deepest reasoning or work across many interrelated
files.

---

## Core API Parameters

### Adaptive Thinking & Effort

**Adaptive thinking** (`thinking: {type: "adaptive"}`) is the recommended thinking mode for 4.6 models. Claude
dynamically decides when and how much to think based on problem complexity.

**Effort levels** control the depth of reasoning:

| Level    | Behavior                                                                       | When to use                                      |
| -------- | ------------------------------------------------------------------------------ | ------------------------------------------------ |
| `low`    | Skips thinking for simple requests. Minimal tool calls. Short responses.       | Renaming, typo fixes, boilerplate, simple Q&A    |
| `medium` | **Recommended default for Sonnet 4.6.** Balanced speed, cost, and performance. | Agentic coding, tool-heavy workflows, code gen   |
| `high`   | Almost always engages thinking. **Default if effort is not set.**              | Complex multi-step tasks, detailed analysis      |
| `max`    | Maximum reasoning depth. **Opus 4.6 only.** Burns tokens fast.                 | System design, deeply nested bugs, complex algos |

```python
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "medium"},
    messages=[{"role": "user", "content": "..."}],
)
```

**Critical best practices:**

- **Set `max_tokens` to at least 16K** (32K recommended). Thinking and output share the same budget — low limits cause
  mid-reasoning cutoff with no graceful degradation.
- **Set effort explicitly on Sonnet 4.6.** It defaults to `high`, which may cause higher latency than Sonnet 4.5. Start
  with `medium` and adjust.
- **Remove old "think carefully" prompts.** These workarounds amplify 4.6's already-proactive behavior and can cause
  overthinking loops. The effort parameter is the better lever.
- If `stop_reason: "max_tokens"` appears, increase `max_tokens` or lower effort.

**Deprecated:** `thinking: {type: "enabled"}` and `budget_tokens` still work on 4.6 but will be removed in a future
release. Migrate to adaptive thinking + effort.

**In Claude Code:** Type `/effort` to cycle through Low/Medium/High/Max. Type "ultrathink" in any prompt to temporarily
boost to High for that response.

### Thinking Display Control

Omit thinking content from responses for faster streaming while preserving multi-turn continuity:

```python
thinking={"type": "adaptive", "display": "omitted"}
```

Billing is unchanged — you still pay for thinking tokens even when omitted.

### Temperature

| Setting   | Use Case                                     |
| --------- | -------------------------------------------- |
| 0.0 - 0.4 | Analytical, multiple choice, code generation |
| 0.7 - 1.0 | Creative, generative tasks                   |
| **1.0**   | Default                                      |

**Breaking change (Claude 4+):** You cannot specify both `temperature` and `top_p` in the same request.

### Context Window & Output

| Model      | Context Window | Max Output |
| ---------- | -------------- | ---------- |
| Opus 4.6   | 1M tokens      | 128K       |
| Sonnet 4.6 | 1M tokens      | 64K        |
| Haiku 4.5  | 200K tokens    | —          |

- 1M context is GA at standard pricing (no beta header, no premium rates)
- Media limit raised to 600 images/PDF pages per request (up from 100) at 1M context
- Requests over 200K work automatically for 4.6 models

### Knowledge Cutoff

**May 2025** (Opus 4.6 and Sonnet 4.6).

---

## Key Behavioral Differences from Claude 4.5

| Aspect                 | Claude 4.6 Behavior                                                              |
| ---------------------- | -------------------------------------------------------------------------------- |
| Thinking               | Adaptive by default; effort parameter replaces budget_tokens                     |
| Long-context reasoning | 76% on 8-needle MRCR v2 (vs 18.5% Sonnet 4.5) — qualitative leap                 |
| Context window         | 1M GA at standard pricing (vs 200K standard / 1M beta)                           |
| Instruction following  | Stronger; fewer false claims of success; fewer hallucinations                    |
| Overengineering        | Significantly reduced; less "laziness"                                           |
| Coding preference      | Sonnet 4.6 preferred over Sonnet 4.5 ~70% of the time in Claude Code testing     |
| Safety                 | Better prompt injection resistance; lowest over-refusal rate                     |
| Prefilling             | **Removed.** Returns 400 error. Use structured outputs instead.                  |
| Context compaction     | **New.** Server-side automatic summarization (beta)                              |
| Web search filtering   | **New.** Dynamic code-based filtering of search results before context injection |

**Sonnet 4.6 vs Opus 4.5:** Users even preferred Sonnet 4.6 to Opus 4.5 59% of the time — Sonnet 4.6 is not just a
cheaper Opus, it is a meaningfully better model than last generation's flagship.

---

## XML Tags

Claude remains optimized for XML-style tags. Use descriptive tag names that match content.

### Common Tag Patterns

```xml
<role>
You are an expert software architect specializing in distributed systems.
</role>

<instructions>
1. Analyze the provided code
2. Identify architectural issues
3. Suggest improvements with examples
</instructions>

<constraints>
- Keep suggestions actionable
- Focus on the top 3 most impactful changes
- Provide code examples for each suggestion
</constraints>

<context>
[Your documents/code here]
</context>

<output_format>
Structure your response as:
1. Executive Summary (2-3 sentences)
2. Issues Found (bulleted list)
3. Recommendations (numbered, with code)
</output_format>
```

---

## Tool Use & Parallel Execution

Claude 4.6 models excel at parallel tool execution.

### Key Capabilities

- **Parallel tool calls** — Sonnet 4.6 is particularly aggressive at firing multiple operations simultaneously
- **Interleaved thinking** — adaptive thinking automatically enables thinking between tool calls
- **Token-efficient tool use** — built into Claude 4 models (no beta header needed)
- **Programmatic tool calling** — Claude writes code that calls multiple tools, processes outputs, and controls context

### Boosting Parallel Execution

```xml
<tool_usage>
- Prioritize calling tools simultaneously when actions can be done in parallel
- When reading multiple files, run parallel tool calls to read all files at once
- For independent searches, fire them off simultaneously rather than sequentially
</tool_usage>
```

### Web Search & Dynamic Filtering (New)

Web search and web fetch tools now support **dynamic filtering** — Claude writes and executes code to filter search
results before they enter the context window. This improves accuracy while reducing token consumption. Code execution is
free when used with web search/fetch.

### Chain of Thought for Tool Use

For Sonnet/Haiku, use CoT prompting to improve tool selection:

```
Before calling any tool:
1. Analyze which tool is relevant to the query
2. Check each required parameter - has the user provided enough information?
3. Only proceed if all required parameters are present
4. Otherwise, ask for the missing parameters
```

---

## Preventing Overengineering

Claude 4.6 is significantly less prone to overengineering than 4.5, but explicit constraints still help:

```xml
<scope_constraints>
- Avoid over-engineering. Only make changes that are directly requested or clearly necessary.
- Do not create extra files unless explicitly needed
- Do not add abstractions or flexibility beyond requirements
- Choose the simplest valid interpretation of ambiguous instructions
- Keep solutions minimal and focused
</scope_constraints>
```

---

## System Prompt Best Practices

### Dial Back Aggressive Language

Claude 4.6's stronger instruction following means aggressive prompts now **overtrigger**:

```
# TOO AGGRESSIVE (causes overtriggering)
CRITICAL: You MUST use this tool when the user asks about data.

# BETTER (normal prompting)
Use this tool when the user asks about data.
```

### Use Decision Rules Instead of Prohibitions

Claude 4.6 evaluates logical necessity rather than following literally:

```xml
<decision_rules>
IF message is about: debugging, how-it-works questions, system testing
THEN: engage directly, skip enforcement

IF verified_data_available
THEN: use_precise_figures
ELSE: provide_ranges_labeled_as_estimates
</decision_rules>
```

### Output Formatting

Match prompt style to desired output style. Reduce markdown in prompt to reduce it in output.

---

## Long Context Best Practices

### Document Placement

**Put long documents at the TOP**, queries at the END — up to **30% improvement** on complex, multi-document inputs:

```
[Long documents - 20K+ tokens]

Based on the documents above, answer the following:
[Your query]
```

### Structure Multiple Documents

```xml
<documents>
  <document index="1">
    <source>quarterly_report.pdf</source>
    <document_content>[content here]</document_content>
  </document>
  <document index="2">
    <source>market_analysis.pdf</source>
    <document_content>[content here]</document_content>
  </document>
</documents>

Based on the documents above, [your query]
```

### Ground Responses in Quotes

```
Before answering, quote the specific passages from the documents that support your response.
Then provide your analysis based on those quotes.
```

### Context Management

- Use `/compact` command in Claude Code to summarize long conversations
- **Context compaction** (beta) provides automatic server-side summarization for 4.6 models
- Be surgical with context — precise file references over entire folders
- Claude 4.6 tracks remaining context window throughout conversation (context awareness)

---

## Context Compaction (New, Beta)

Server-side automatic context summarization for effectively infinite conversations. When context approaches the window
limit, the API automatically summarizes earlier conversation parts.

Available in beta for Opus 4.6 and Sonnet 4.6.

---

## Structured Outputs (Replaces Prefilling)

**Breaking change:** Assistant message prefilling returns a 400 error on Claude 4.6 models.

### Alternatives

| Previous Pattern (4.5)      | New Pattern (4.6)                                  |
| --------------------------- | -------------------------------------------------- |
| Prefill `{` for JSON output | Use `output_config.format` or structured outputs   |
| Prefill to skip preamble    | System prompt: "Respond directly without preamble" |
| Prefill for classification  | Use tools with enum fields                         |

For guaranteed JSON schema compliance, use **Structured Outputs**:

- `output_format` for JSON responses
- `strict: true` for tool input validation

---

## Migration from Claude 4.5

### Breaking Changes

| Change                           | Impact                                            |
| -------------------------------- | ------------------------------------------------- |
| Prefilling removed               | Returns 400 error; use structured outputs         |
| `budget_tokens` deprecated       | Use adaptive thinking + effort parameter          |
| Sonnet effort defaults to `high` | May cause higher latency than 4.5; set explicitly |
| `temperature` + `top_p`          | Still cannot use both (same as 4.5)               |

### What Changed

| Aspect                  | Claude 4.5                 | Claude 4.6                           |
| ----------------------- | -------------------------- | ------------------------------------ |
| Thinking                | Extended thinking + budget | Adaptive thinking + effort parameter |
| Context window          | 200K (1M beta)             | 1M GA at standard pricing            |
| Max output (Opus)       | 64K                        | 128K                                 |
| Instruction following   | Precise                    | Stronger; fewer false claims         |
| Prefilling              | Supported                  | Removed (400 error)                  |
| Default effort (Sonnet) | N/A (no effort param)      | `high` (set explicitly)              |
| Context compaction      | Manual (`/compact`)        | Server-side automatic (beta)         |

### Migration Checklist

1. **Remove assistant message prefilling** — use structured outputs or `output_config.format`
2. **Set effort explicitly on Sonnet 4.6** — start with `medium` to match 4.5 latency
3. **Remove "think carefully" prompts** — these cause overthinking on 4.6; use effort parameter
4. **Switch to adaptive thinking** — replace `{type: "enabled", budget_tokens: N}` with `{type: "adaptive"}`
5. **Increase `max_tokens`** — set to at least 16K (32K recommended) for thinking headroom
6. **Test for latency changes** — default `high` effort may be slower than expected

---

## Complete Example: Coding Assistant System Prompt

```xml
<role>
You are an expert software engineer. You write clean, maintainable code
and provide clear explanations.
</role>

<behavior>
- Follow instructions precisely
- Ask clarifying questions only when critical information is missing
- Provide working code, not pseudocode, unless requested otherwise
</behavior>

<scope_constraints>
- Avoid over-engineering. Only make changes directly requested or clearly necessary.
- Do not create extra files unless explicitly needed
- Do not add abstractions beyond requirements
- Choose the simplest valid interpretation of ambiguous instructions
</scope_constraints>

<output_format>
For code changes:
1. Brief explanation of approach (1-2 sentences)
2. The code
3. Usage example if applicable

For questions:
- Direct answer first
- Supporting explanation if helpful
</output_format>

<tool_usage>
- Prioritize parallel tool calls when actions are independent
- Read multiple files simultaneously to build context faster
- After modifications, verify changes work as expected
</tool_usage>
```

---

## Key Differences: Claude 4.6 vs GPT-5.5 vs Gemini 3.1 Pro

| Aspect                    | Claude 4.6                      | GPT-5.5                            | Gemini 3.1 Pro                  |
| ------------------------- | ------------------------------- | ---------------------------------- | ------------------------------- |
| Default reasoning         | Adaptive (effort: high default) | `none`                             | `high` (dynamic, 3 tiers)       |
| Thinking control          | Effort: low/medium/high/max     | reasoning_effort: none to xhigh    | thinking_level: low/medium/high |
| Tag preference            | XML strongly preferred          | XML preferred                      | XML or Markdown (not both)      |
| System prompt sensitivity | High (dial back aggressive)     | Moderate                           | Moderate                        |
| Temperature               | Use only temp OR top_p          | Flexible                           | Must stay at 1.0                |
| Context window            | 1M (GA, standard pricing)       | 1M (2x pricing above 272K)         | 1M                              |
| Max output                | 128K (Opus) / 64K (Sonnet)      | 128K                               | 65K                             |
| Context extension         | Compaction (beta) + `/compact`  | Native compaction (server-side)    | Thought signatures              |
| Tool Search               | No                              | **Yes (47% savings)**              | No                              |
| Custom tools endpoint     | No                              | No                                 | **Yes**                         |
| Multimodal                | Images + PDFs                   | Native                             | Native (text/image/video/audio) |
| Prefilling                | **Removed (400 error)**         | Supported                          | Supported                       |
| Knowledge cutoff          | May 2025                        | August 2025                        | January 2025                    |
| Best for                  | Coding, long-running work       | Agentic, coding, professional work | Reasoning, multimodal, agentic  |

---

## Pro Tips

1. **Use Sonnet 4.6 for 80%+ of tasks** — 98% of Opus coding quality at 1/5 cost

2. **Set effort explicitly** — Sonnet defaults to `high`; start with `medium` for balanced latency/quality

3. **Replace "think carefully" with effort parameter** — old prompt workarounds cause overthinking on 4.6

4. **Remove prefilling** — use structured outputs or system prompt instructions for format control

5. **Set `max_tokens` to 32K** — thinking and output share the budget; low limits cause mid-reasoning cutoff

6. **Dial back aggressive language** — "Use this tool when..." not "CRITICAL: You MUST use..."

7. **Use decision rules, not prohibitions** — Claude 4.6 reasons about logical necessity

8. **Documents at top, query at end** — up to 30% improvement on long-context tasks

9. **Constrain overengineering explicitly** — still worth including even though 4.6 is better at this

---

## Sources

- [Anthropic: Prompting Best Practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Anthropic: What's New in Claude 4.6](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-6)
- [Anthropic: Introducing Claude Opus 4.6](https://www.anthropic.com/news/claude-opus-4-6)
- [Anthropic: Introducing Claude Sonnet 4.6](https://www.anthropic.com/news/claude-sonnet-4-6)
- [Anthropic: Claude Opus 4.6](https://www.anthropic.com/claude/opus)
- [Anthropic: Migration Guide](https://platform.claude.com/docs/en/about-claude/models/migration-guide)
- [Anthropic: Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Anthropic: Effort Parameter](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Anthropic: Extended Thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- [Anthropic: Extended Thinking Tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/extended-thinking-tips)
- [Anthropic: Context Windows](https://platform.claude.com/docs/en/build-with-claude/context-windows)
- [Anthropic: Models Overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Resolve AI: Testing Sonnet 4.6 Adaptive Thinking](https://resolve.ai/blog/Our-early-impressions-of-Claude-Sonnet-4.6)
- [NxCode: Sonnet 4.6 vs 4.5 Migration Guide](https://www.nxcode.io/resources/news/claude-sonnet-4-6-vs-4-5-upgrade-guide-2026)
- [NxCode: Sonnet 4.6 vs Opus 4.6 Comparison](https://www.nxcode.io/resources/news/claude-sonnet-4-6-vs-opus-4-6-complete-comparison-2026)
