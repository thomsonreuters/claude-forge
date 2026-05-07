# Documentation Guidelines

Documentation standards for Claude Forge.

---

## Living vs Static Documents

### Living Documents (update regularly)

- **Implementation checklists** - Current tasks
- **Change logs** - Completed work record
- **Evaluation results** - Update after evals/tests

### Coding Context Documents

- **Design docs** - Aggregated coding reference
  - Architecture context
  - Consolidates key contracts, file locations, patterns
  - **Maintenance Rule**: Update FIRST on refactors/moves
  - Must stay accurate; it's future-session context

### Proposals (`docs/proposals/`)

- Forward-looking design sketches for features not yet implemented or scheduled
- May reference aspirational architecture, upstream capabilities, or research prototypes
- No update cadence — refresh when the topic becomes active work

### Static Design Documents (aspirational architecture)

- Describe **target system** (what we're building toward)
- May reference unimplemented features

---

## Documentation Rules

**Rule 1**: Checklist = current work; change log = completed work; design docs = target system; proposals = future
sketches; AutoMem = evolving state

**Rule 2**: Verbosity has a cost; balance with clarity and intent.

**Rule 3**: Design-doc code blocks show the **gist**, not full implementations:

- Show signatures + key logic flow
- Use `...` for obvious details
- Prefer terse one-liners over long comments
- Link to full specs
- Goal: convey architecture, not copy-paste code

---

## Where to Document What

| What                 | Where              | When to Update            |
| -------------------- | ------------------ | ------------------------- |
| Coding context       | Design docs        | FIRST, on refactors       |
| Current metrics      | AutoMem            | On evolving facts         |
| Target system design | Static design docs | On architecture changes   |
| Future sketches      | `docs/proposals/`  | When topic becomes active |

---

## Checklist Policy

### TDD-First Acceptance Criteria

Each phase SHOULD define acceptance criteria:

1. **Testable**: Verified by a specific test
2. **Measurable**: Numeric thresholds or boolean outcomes
3. **Fixture-grounded**: References fixtures when relevant

**Acceptance test table**:

```markdown
| Test | Fixture | Assertion | Test File |
| ---- | ------- | --------- | --------- |
| Policy blocks write | git_repo | PreToolUse returns deny | `test_guard.py` |
| Stop hook fast | mock | Execution < 100ms | `test_stop_hook.py` |
```

**Anti-patterns**:

| Avoid                  | Instead                                                 |
| ---------------------- | ------------------------------------------------------- |
| "Hook works correctly" | "Stop hook completes in \<100ms with transcript copied" |
| "Tests pass"           | "58 guard tests pass; mypy clean"                       |

### Checklist Lifecycle

1. **Start**: Create tasks with `[ ]` + acceptance test tables
2. **During**: Update checkboxes; note blockers
3. **Complete**: Move notes to change_log; remove phase details; update focus

---

## Change Log Policy

### Entry Structure (Required)

Each entry MUST include:

1. **Goal** (1 sentence): Objective
2. **Key Changes** (bullets): Added/modified/deleted
3. **Verification** (1 line): How validated

Each entry MAY include (when relevant):

- **Design decisions**: Key choices + rationale
- **Files created/modified**: Only for major refactors (>10 files) — summarize by package
- **Deferred items**: Explicitly not done

### Entry Format

```markdown
## YYYY-MM-DD

### Phase X.Y: Short Title

**Goal**: One sentence describing the objective.

**Key changes**:

- Bullets: WHAT changed (code shows HOW)
- New files
- Key decisions

**Verification**: How validated (e.g., "58 tests pass; mypy clean")

**Deferred**: Items postponed (optional)
```

### Detail Level Guidelines

| Entry Type            | Target Lines | Content                                    |
| --------------------- | ------------ | ------------------------------------------ |
| Bug fix               | 5-10         | Goal + fix + verification                  |
| Feature completion    | 15-25        | Goal + key changes + tests added           |
| Phase completion      | 25-40        | Goal + major changes + acceptance criteria |
| Architecture refactor | 40-60 max    | Include package summaries, migration notes |

**Consistency matters**: Similar work should have similar detail. If one bug fix is 5 lines, another shouldn't be 50.

**Anti-pattern**: Listing every file modified. If >10 files, summarize by package (e.g., "Updated 14 files in
`src/guard/` and `tests/src/guard/`").

**Rule of thumb**: If it can't be summarized in 40 lines, it's too detailed. Code is HOW; docs are WHAT/WHY.

---

## Writing Style

Docs are read by humans and AI agents; be direct and specific.

### Principles

1. **Say the thing.** Say it once; no preambles, repetition, or summaries.
2. **Specifics over gestures.** "Improves performance" is vague; "p99 200ms→45ms" isn't. If you don't have the number,
   say so.
3. **Earn every sentence.** If it doesn't add new info, merge or cut.
4. **Plain language wins.** "Use" not "utilize." Prefer plain meaning over fancy synonyms.
5. **Structure follows content.** Bullets for parallel items. Prose for arguments. Tables for comparisons.

### Vocabulary Hygiene

Avoid AI filler words:

- **Always cut**: delve, tapestry, vibrant, myriad, plethora, utilize, unlock, groundbreaking, revolutionary,
  transformative
- **Check context**: robust (ML/stats), seamless (failover), leverage (existing infra), comprehensive (test suite)
- **Replace metaphors with specifics**: name the work/scale/practice/criteria

### Structural Tells to Avoid

- Every section opening with "X is a Y that Z" (definition → elaboration)
- Opening paragraphs that restate the heading (echo effect)
- Uniform paragraph/section lengths — vary with importance
- Summary paragraphs on short documents — the reader remembers
- "Furthermore," / "Moreover," / "Additionally," as paragraph openers (filler transitions)

### When Writing for AI Consumption

CLAUDE.md files and design docs are AI context. Write for machine parsability too:

- **Be specific over general.** "Run `uv run pytest tests/src -v`" beats "run the tests."
- **State constraints, not aspirations.** "Never skip tests" beats "we value testing". Include must-NOT constraints.
- **Frontload actionable content.** Put important rules first.
- **Use exact identifiers.** Say `forge session start`, not "the session command." Avoid "it"/"this" when ambiguous.
- **Tag code blocks with language.** Agents parse tagged blocks more reliably.
- **Keep files within context limits.** A 25K-token doc degrades performance; split or archive.

---

## Size Limits

### Maximum Document Size (Hard Limits)

Agents: ~25k tokens; Read truncates >2k lines. Keep docs under these limits; use
[count-tokens.py](../../scripts/count-tokens.py) with `--model` matching the coding agent's model for accurate counts
(the agent knows its own model ID):

```bash
./scripts/count-tokens.py --model claude-sonnet-4-6 docs/design.md
25,677 tokens | 99,378 chars | 1,924 lines
  method: anthropic API (claude-sonnet-4-6)
```
