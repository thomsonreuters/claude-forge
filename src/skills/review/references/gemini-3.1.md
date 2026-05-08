# Gemini 3.1 Pro Prompting Guide

> Synthesized from [Google AI Developer Docs](https://ai.google.dev/gemini-api/docs/gemini-3),
> [Google Cloud Vertex AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-1-pro), and
> [Google DeepMind Model Card](https://deepmind.google/models/model-cards/gemini-3-1-pro/). May 2026.

## Overview

Gemini 3.1 Pro is Google's frontier reasoning model, released in public preview on February 19, 2026. For prompting and
integration, the important changes are:

- **Three-tier thinking** - `low`, `medium`, and `high`; `medium` is new in 3.1 Pro
- **1,048,576 token context window** and **65,536 max output tokens**
- **Custom tools endpoint** - `gemini-3.1-pro-preview-customtools` for agents that mix bash with custom tools
- **Thought signatures** - important for multi-turn and strict function-calling workflows
- **Media resolution control** - use `media_resolution_*` levels to trade detail for token cost

**Key characteristic:** Gemini 3.1 Pro favors **directness over persuasion** and **logic over verbosity**. It is
intentionally slower on complex tasks — taking time to reason rather than rushing to a plausible-sounding answer. It
performs best with clear, concise prompts that define the task, constraints, and output shape. Treat it like briefing a
consultant: the more structured your input, the more structured and useful your output.

**Migration note:** Gemini 3 Pro Preview was deprecated March 9, 2026. Gemini 3.1 Pro Preview is the replacement at
identical pricing.

---

## Core API Parameters

### `thinking_level`

Controls the depth of internal reasoning. Replaces `thinking_budget` from Gemini 2.5 (still accepted for backward
compatibility, but do not use both in the same request).

| Level    | Behavior                                                      | Use when                                      |
| -------- | ------------------------------------------------------------- | --------------------------------------------- |
| `low`    | Constrains reasoning for lower latency and cost.              | Extraction, classification, simple tool calls |
| `medium` | Balanced setting introduced in 3.1 Pro.                       | Everyday coding, analysis, and research       |
| `high`   | Highest reasoning setting; default dynamic mode for Gemini 3. | Hard reasoning, math, debugging, planning     |

**Key insight:** If you omit `thinking_level`, Gemini 3 models use high/dynamic thinking. Use `medium` or `low` only
after evals show the lower setting preserves quality for the task.

**Cost optimization:** Lowering `thinking_level` can reduce latency and billed thinking tokens, but it can also reduce
instruction-following quality on multi-step tasks. Tune against evals rather than setting one global level.

**Important:** Thinking cannot be turned off. The lowest setting is LOW, which still performs basic reasoning. Thinking
tokens are billed as output tokens.

**OpenAI compatibility layer:** OpenAI-style `reasoning_effort` maps to Gemini `thinking_level`; verify the exact
mapping in the SDK or gateway you use.

### Temperature

**Keep at default 1.0.** Do not lower it. Gemini 3's reasoning engine is optimized for 1.0; lowering it may cause
looping or degraded performance in complex tasks.

```python
# BAD - may cause looping
generation_config = {"temperature": 0.2}

# GOOD - use default
generation_config = {}  # temperature defaults to 1.0
```

### Context Window & Output

- **1,048,576 tokens** input (~1,500 A4 pages)
- **65,536 tokens** max output

**Important:** Configure `maxOutputTokens` explicitly when you need long output. A high limit does not force a long
answer; the prompt still needs a section plan, target depth, or completeness criteria.

### Knowledge Cutoff

**January 2025.** Use Search Grounding for more recent information.

---

## Key Behavioral Differences from Gemini 3 Pro

| Aspect            | Gemini 3.1 Pro Behavior                                                          |
| ----------------- | -------------------------------------------------------------------------------- |
| Thinking control  | Adds `medium`; omit the parameter for the default high/dynamic behavior          |
| Prompt shape      | Direct, concise prompts work better than verbose legacy scaffolding              |
| Output style      | Concise by default; ask explicitly for conversational tone or detailed rationale |
| Long context      | Put the specific question after large context and anchor answers to that context |
| Tool routing      | Use `customtools` only for agents that mix bash with custom file/search tools    |
| Multimodal inputs | Use `media_resolution_*`, named references, and timestamps deliberately          |

---

## Core Principles

### 1. Be Direct and Concise

State your goal clearly. Gemini 3.1 Pro may over-analyze verbose prompt engineering techniques designed for older
models.

```
# BAD (too verbose)
I would really appreciate it if you could kindly help me with
summarizing the following document. Please make sure to capture
all the key points and present them in a clear manner.

# GOOD (direct)
Summarize this document. Include all key points.
```

### 2. Default Output is Concise

Gemini 3.1 Pro provides direct, efficient answers by default. If you need detailed or conversational responses,
explicitly request it:

```xml
<constraints>
- Verbosity: High
- Provide detailed explanations with examples
- Use a conversational, friendly tone
</constraints>
```

### 3. Structure with XML or Markdown (Not Both)

Use consistent delimiters. XML-style tags or Markdown headings work well. Choose one format per prompt — mixing causes
confusion.

**XML Example:**

```xml
<rules>
1. Be objective.
2. Cite sources.
</rules>

<context>
[Your data here - model knows this is data, not instructions]
</context>

<task>
[Your specific request]
</task>
```

**Markdown Example:**

```markdown
# Identity
You are a senior solution architect.

# Constraints
- No external libraries allowed.
- Python 3.11+ syntax only.

# Output Format
Return a single code block.
```

### 4. Place Instructions Strategically

- **System instruction / top of prompt:** Behavioral constraints, role definitions
- **End of prompt:** Specific instructions when working with large contexts

### 5. Avoid Overly Broad Negative Constraints

Open-ended instructions like "do not infer" or "do not guess" may cause the model to over-index and fail basic logic.
Instead, tell the model explicitly to use provided context for deductions and avoid outside knowledge:

```xml
<!-- BAD -->
<constraints>Do not infer or guess anything.</constraints>

<!-- GOOD -->
<constraints>
- Use only the provided context for deductions.
- Avoid using outside knowledge.
- If the answer is not in the context, say so.
</constraints>
```

### 6. Anchor After Large Contexts

When transitioning from data to your query, use explicit bridging:

```
[Large document/codebase here]

Based on the information above, identify the three main performance bottlenecks.
```

### 7. Add Grounding and Time-Awareness

For time-sensitive queries, add to system instructions:

```
You MUST follow the provided current time (date and year) when formulating search queries.
Remember it is 2026 this year. Your knowledge cutoff date is January 2025.
```

---

## Structured Prompting Patterns

### Role + Goal + Constraints + Output Format

A reliable pattern for most tasks:

```xml
<role>
You are a specialized assistant for [Domain].
You are precise, analytical, and persistent.
</role>

<instructions>
1. Plan: Analyze the task and create step-by-step sub-tasks
2. Execute: Carry out the plan. If using tools, reflect before every call
3. Validate: Review output against user's task
4. Format: Present final answer in requested structure
</instructions>

<constraints>
- Verbosity: [Low/Medium/High]
- Tone: [Formal/Casual/Technical]
- Handling Ambiguity: Ask clarifying questions ONLY if critical info is missing
</constraints>

<output_format>
1. Executive Summary: [2 sentence overview]
2. Detailed Response: [Main content]
</output_format>
```

### Explicit Planning & Decomposition

```
Before providing the final answer, please:
1. Parse the stated goal into distinct sub-tasks.
2. Is the input information complete? If not, stop and ask for it.
3. Are there tools, shortcuts, or "power user" methods that solve this better?
4. Create a structured outline to achieve the goal.
5. Validate your understanding before proceeding.
```

### Self-Critique

```
Before returning your final response, review against the user's constraints:
1. Did I answer the user's *intent*, not just their literal words?
2. Is the tone authentic to the requested persona?
3. If I made an assumption due to missing data, did I flag it?
```

### Error Handling

```xml
<error_handling>
IF <context> is empty, missing code, or lacks necessary data:
  DO NOT attempt to generate a solution.
  DO NOT make up data.
  Output a polite request for the missing information.
</error_handling>
```

---

## Agentic Workflows & Tool Calling

### The Persistence Directive

```
You are an autonomous agent.
- Continue working until the user's query is COMPLETELY resolved.
- If a tool fails, analyze the error and try a different approach.
- Do NOT yield control back to the user until you have verified the solution.
```

### Pre-Computation Reflection

```
Before calling any tool, explicitly state:
1. Why you are calling this tool.
2. What specific data you expect to retrieve.
3. How this data helps solve the user's problem.
```

### Thought Signatures (Critical for Multi-turn)

Gemini 3 uses **thought signatures** to maintain reasoning context across API calls. These are encrypted representations
of the model's internal thought process.

**You MUST return thought signatures exactly as received:**

```python
# When you receive a response with a thought signature
response = model.generate_content(prompt)

# In the next turn, include the thought signature
next_response = model.generate_content(
    contents=[
        # Include previous response with thought signature
        response.candidates[0].content,
        # Your new message
        {"role": "user", "parts": [{"text": "Continue..."}]}
    ]
)
```

**For function calling:** The API enforces strict validation — missing signatures result in a 400 error. This applies
even when `thinking_level` is set to LOW.

### Custom Tools Endpoint (New in 3.1 Pro)

A dedicated model variant `gemini-3.1-pro-preview-customtools` for agents that mix bash commands with custom function
calls.

**The problem it solves:** Standard Gemini 3.1 Pro sometimes bypasses registered custom tools in favor of raw bash
commands (`cat` instead of `view_file`, `grep` instead of `search_code`). The customtools variant prioritizes registered
tools.

**When to switch:** If bash usage exceeds ~30% of actions that could be handled by registered tools, switch to
customtools. Diagnostic signals:

- Model uses `cat` when `view_file` is registered
- Model uses `grep` when `search_code` is available
- Model uses `sed` when `edit_file` exists

**Usage:** Change the model parameter only — no other code changes needed:

```python
# Standard
model = "gemini-3.1-pro-preview"

# Custom tools optimized
model = "gemini-3.1-pro-preview-customtools"
```

**Caveat:** The customtools version is not "stronger" — it is fine-tuned for tool calling. For tasks that don't involve
custom tools, the standard version performs better.

### Tool Calling Best Practices

1. **Maximize a single agent first** — Gemini handles dozens of tools in a single prompt well
2. **Stream function call arguments** — Set `streamFunctionCallArguments: true` to reduce perceived latency
3. **Use `thinking_level: high`** for deep planning and complex instruction following
4. **Use `thinking_level: low`** for high-throughput tasks
5. **Use customtools** when building coding agents with custom file/search/edit tools

---

## Multimodal Prompting

For multimodal prompts, treat each media input as a named source with an explicit resolution choice and task-specific
reference. Avoid vague "look at this" prompts when there are multiple media parts.

### Media Resolution Control

Use the `media_resolution` parameter to balance quality vs token cost. It can be set per media part or globally; global
`ultra_high` is not supported.

| Level                         | Use Case                                                    |
| ----------------------------- | ----------------------------------------------------------- |
| `media_resolution_low`        | General video/action understanding, lowest token cost       |
| `media_resolution_medium`     | PDFs and documents where quality usually saturates          |
| `media_resolution_high`       | Images, fine text, dense visual details, text-heavy video   |
| `media_resolution_ultra_high` | Per-part maximum fidelity for unusually detail-heavy inputs |

### Be Explicit with References

```
# BAD (ambiguous)
Look at this and tell me what's wrong.

# GOOD (explicit)
Use Image 1 (Funnel Dashboard) and Video 2 (Checkout Flow)
to identify the drop-off point.
```

### Use Timestamps for Audio/Video

```
Analyze the user reaction in the video from 1:30 to 2:00.
```

### Input Order

For single-media prompts, add your video/media first, then your question.

### Multimodal Function Responses

Function responses can now include multimodal objects like images and PDFs in addition to text.

---

## Structured Output & Grounding

### Combine Structured Output with Built-in Tools

```python
response = model.generate_content(
    contents="Find the current stock price of GOOGL and return as JSON",
    generation_config={
        "response_mime_type": "application/json",
        "response_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "price": {"type": "number"},
                "currency": {"type": "string"}
            }
        }
    },
    tools=[{"google_search": {}}]  # Grounding with Google Search
)
```

Choose grounding tools explicitly. Common choices include:

- Google Search
- URL Context
- Code Execution
- File Search
- Maps grounding, when the target API surface supports it

---

## Coding Prompting

### Practical Limits

- Higher latency for small iterative edits (intentional — reasoning over speed)
- Verify outputs, especially dependency versions and commands
- May bypass custom tools for raw bash (use customtools endpoint)

### Recommended Approach

1. Be direct with requirements
2. Start with the default/high reasoning baseline, then evaluate `medium` for routine coding tasks
3. Keep `thinking_level: high` for complex refactors, debugging, and architectural planning
4. Break large tasks into sub-tasks
5. Ask for validation/testing steps

---

## Migration from Gemini 3 Pro

### What Changed

| Aspect          | Gemini 3 Pro | Gemini 3.1 Pro                      |
| --------------- | ------------ | ----------------------------------- |
| Thinking levels | LOW, HIGH    | LOW, MEDIUM, HIGH                   |
| Max output      | Lower limit  | 65,536 max output tokens            |
| Tool routing    | Standard     | Standard + `customtools` variant    |
| Prompting style | Direct       | Direct, concise, less scaffolding   |
| Multimodal cost | Less control | `media_resolution_*` per media part |

### Migration Strategy

1. **Change the model name** — Start with the standard `gemini-3.1-pro-preview` endpoint
2. **Retune thinking levels** — Keep default/high for baseline quality, then evaluate `medium` or `low`
3. **Set `maxOutputTokens`** — Configure it explicitly for longer output
4. **Try customtools** — If building coding agents with custom file/search tools
5. **Simplify prompts** — 3.1 Pro reasons better; you may be able to remove chain-of-thought scaffolding

---

## Complete Example: System Prompt

```xml
<role>
You are a specialized assistant for [Insert Domain].
You are precise, analytical, and persistent.
</role>

<instructions>
1. **Plan**: Analyze the task and create a step-by-step plan into distinct sub-tasks
2. **Execute**: Carry out the plan. If using tools, reflect before every call.
   Track progress: [ ] pending, [x] complete
3. **Validate**: Review your output against the user's task
4. **Format**: Present the final answer in the requested structure
</instructions>

<constraints>
- Verbosity: [Low/Medium/High]
- Tone: [Formal/Casual/Technical]
- Handling Ambiguity: Ask clarifying questions ONLY if critical info is missing;
  otherwise, make reasonable assumptions and state them
- Use only the provided context for deductions; avoid outside knowledge
</constraints>

<output_format>
1. **Executive Summary**: [2 sentence overview]
2. **Detailed Response**: [The main content]
</output_format>
```

---

## Key Differences: Gemini 3.1 Pro vs GPT-5.5 vs Claude 4.7

| Aspect            | Gemini 3.1 Pro                       | GPT-5.5                                  | Claude Opus 4.7                           |
| ----------------- | ------------------------------------ | ---------------------------------------- | ----------------------------------------- |
| Default reasoning | `high` (dynamic, 3 tiers)            | `medium`                                 | Thinking off unless `adaptive` set        |
| Thinking control  | `thinking_level` (low/medium/high)   | `reasoning.effort` (none to xhigh)       | `thinking: {"type": "adaptive"}` + effort |
| Temperature       | Keep at 1.0                          | Flexible                                 | Omit non-default sampling params          |
| Context window    | 1M tokens                            | 1.05M tokens                             | 1M tokens                                 |
| Max output        | 65K tokens                           | 128K tokens                              | 128K sync; 300K batch beta                |
| Structured tags   | XML or Markdown, not both            | XML preferred                            | XML strongly preferred                    |
| Multi-turn state  | Thought signatures                   | `previous_response_id` / reasoning items | Conversation state + compaction beta      |
| Knowledge cutoff  | January 2025                         | Dec 1, 2025                              | January 2026                              |
| Best for          | Direct reasoning, multimodal prompts | Agentic, coding, professional work       | Hard coding, review, long agents          |

---

## Pro Tips

01. **Baseline with default/high thinking** — Lower to `medium` or `low` only after evals preserve quality

02. **Use `medium` for balanced throughput** — Good candidate for routine coding, research, and analysis

03. **Keep temperature at 1.0** — Seriously, don't change it

04. **Configure `maxOutputTokens` explicitly** — You have up to 65K available

05. **Try customtools for coding agents** — If the model bypasses your tools for raw bash

06. **Simplify prompts from Gemini 3 era** — 3.1 Pro reasons better; remove chain-of-thought scaffolding

07. **One format only** — XML or Markdown, never mix

08. **Return thought signatures** — Critical for multi-turn and function calling; 400 error if missing

09. **Use `media_resolution` for multimodal** — Balance quality vs token cost per input

10. **Avoid broad negative constraints** — "Do not infer" causes over-indexing; be specific instead

---

## Sources

- [Google AI: Gemini 3 Developer Guide](https://ai.google.dev/gemini-api/docs/gemini-3)
- [Google AI: Prompt Design Strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies)
- [Google AI: Thinking](https://ai.google.dev/gemini-api/docs/thinking)
- [Google Cloud: Gemini 3.1 Pro Documentation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-1-pro)
- [Google DeepMind: Gemini 3.1 Pro Model Card](https://deepmind.google/models/model-cards/gemini-3-1-pro/)
- [Google Blog: Gemini 3.1 Pro Announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-1-pro/)
- [Google Cloud Blog: Gemini 3.1 Pro on CLI, Enterprise, Vertex AI](https://cloud.google.com/blog/products/ai-machine-learning/gemini-3-1-pro-on-gemini-cli-gemini-enterprise-and-vertex-ai)
