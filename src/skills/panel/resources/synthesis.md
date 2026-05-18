# Multi-Model Synthesis Instructions

You have received responses from multiple AI models reviewing the same target. Your task is to synthesize these into a
unified, actionable report.

## Synthesis Framework

### 1. Identify Consensus Issues

Issues found by **2 or more models** have higher confidence. List these first:

```markdown
## Consensus Findings (High Confidence)

### Critical
- **[Issue]** (found by: gpt-5.5, gemini-3.1-pro-preview)
  - Location: `file.py:123`
  - Impact: [description]
  - Fix: [suggestion]
```

### 2. Catalog Unique Findings

Each model has different strengths. Unique findings may be valid insights others missed:

| Model                  | Strength     | Unique Finding Type                        |
| ---------------------- | ------------ | ------------------------------------------ |
| gpt-5.5                | Logic errors | Edge cases, off-by-one, null handling      |
| gemini-3.1-pro-preview | Pragmatic    | Missing tests, documentation gaps          |
| claude-opus            | Architecture | Coupling, abstraction leaks, design issues |

### 3. Resolve Conflicts

When models disagree:

1. **Examine the target** directly to verify claims
2. **Consider context** - is one model misunderstanding the target's conventions?
3. **Note uncertainty** if unresolvable

```markdown
## Disputed Findings

- **[Issue]**: gpt-5.5 says X, gemini says Y
  - My assessment: [your determination after examining the target]
```

### 3.5. Extract Cross-Review Insights

What does the *combination* of reviews reveal that no single review shows?

1. **Convergence patterns**: Do independent reviewers flag the same subsystem or concern, even with different framing?
   Shared convergence on an area amplifies its importance.
2. **Blind spots from disagreement**: When one model flags a risk that others ignore, note whether the others lacked
   evidence or lacked the analytical frame to see it.
3. **Severity calibration**: Note where reviewers disagree on severity -- the spread itself is informative.
4. **Mechanical/parsing findings**: Findings based on literal parsing (syntax errors, invalid markup, broken links,
   wrong field names) are uniquely valuable from multi-model review. Elevate these regardless of which single model
   found them.

### 4. Create Unified Priority List

Rank all validated findings by:

1. **Severity**: Critical > High > Medium > Low
2. **Confidence**: Consensus > Unique (verified) > Unique (unverified)
3. **Scope**: Widespread > Isolated

### 5. Suggest Fix Order

Consider dependencies when ordering fixes:

```markdown
## Recommended Fix Order

1. [Critical issue] - blocks other fixes
2. [High issue] - foundation for others
3. [Medium issues] - can be parallelized
4. [Low issues] - nice to have
```

## Output Format

```markdown
# Multi-Model Review: [Target Name]

## Summary
- Models consulted: 3 (gpt-5.5, gemini-3.1-pro-preview, claude-opus)
- Consensus issues: N
- Unique findings: N
- Conflicts resolved: N

## Consensus Findings (High Confidence)
[...]

## Unique Findings Worth Noting
[...]

## Disputed or Uncertain
[...]

## Recommended Fix Order
[...]
```
