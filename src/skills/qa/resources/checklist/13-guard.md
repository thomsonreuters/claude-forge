<!-- prereq: 0.3, 5.1 -->

## 13. Policy/Guard (`forge guard`)

### 13.1 Guard Status

<!-- auto -->

```bash
forge guard status
```

- [ ] Shows enabled/disabled state
- [ ] Shows active bundles (if any)
- [ ] Shows fail mode (if guard was previously enabled; omitted when never configured)

### 13.2 Enable TDD Enforcement

<!-- auto -->

```bash
# Enable TDD bundle
forge guard enable --bundle tdd

# Verify
forge guard status
```

- [ ] TDD bundle activated
- [ ] `tests-before-impl` and `no-skip-tests` rules listed

### 13.3 Enable with Permissive Mode

<!-- auto -->

```bash
# Enable TDD in warn-only mode
forge guard enable --bundle tdd --permissive

# Verify
forge guard status
```

- [ ] TDD in permissive mode (warns instead of blocks)

### 13.4 Enable Coding Standards

<!-- auto -->

```bash
forge guard enable --bundle coding_standards

forge guard status
```

- [ ] Coding standards bundle activated
- [ ] `no-type-checking` and `no-backward-compat` rules listed

### 13.5 On-Demand Policy Check

<!-- auto -->

```bash
# Create a second commit so HEAD~1 is valid
echo 'print("new")' >> src/main.py
git add -A && git commit -q -m "add code for guard diff test"

# Check a diff against policies
git diff HEAD~1 | forge guard check --bundle tdd --bundle coding_standards --diff

# Check with JSON output
git diff HEAD~1 | forge guard check --bundle tdd --diff --json
```

- [ ] Evaluates diff against specified bundles
- [ ] `--json` produces structured output with `passed` and `clean` fields

### 13.6 Supervisor CLI Surface (Phase 19)

<!-- auto -->

```bash
# Verify CLI is wired up
forge guard supervisor --help

# Missing file produces clear error (exit 2)
forge guard supervisor -f /nonexistent/file.py -r 00000000-0000-0000-0000-000000000000 --json
echo "exit: $?"
```

- [ ] `--help` shows usage with `-f`, `-r`, `--json`, `--proxy`, `--timeout` options
- [ ] Missing file produces clear error and exit 2

### 13.7 Manual Supervisor Wiring (Planner -> Supervisor -> Executor)

<!-- prereq: 2.4, 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

In the **container shell**, walk through the manual promotion flow described in the design docs: create a planning
session, approve a tiny plan in Claude, fork a dedicated supervisor session from that planner, then fork the planner
again into a direct-routing executor with `--no-proxy` and wire the promoted supervisor UUID into it. Use a concrete
tiny task so the final `forge guard supervisor` check is evaluating something real, not a placeholder. If any live
Claude launch is not available in this environment, mark this step `Skip` rather than inventing evidence.

```
cd $FORGE_TEST_REPO

# Clean up from previous runs
forge session delete guard-planner --force 2>/dev/null || true
forge session delete guard-supervisor --force 2>/dev/null || true
forge session delete guard-executor --force 2>/dev/null || true
rm -f src/supervisor_demo.py

# 1) Start a planning session on the proxy created in 4.2.
# Once Claude launches:
#   a) Type:  /plan
#   b) Paste:
#        Skip the exploration step. Create a plan only. Do not edit files or run any write tools.
#
#        The exact approved plan should be:
#        1. Create `src/supervisor_demo.py`
#        2. Add:
#           def greet(name: str) -> str:
#               return f"hello, {name}"
#        3. Do not modify any other files
#
#        After showing the plan, wait for my approval.
#   c) When Claude shows the plan, type:
#        I approve this exact plan. Do not implement it in this session. Wait.
#   e) Verify a plan file was created:  ls ~/.claude/plans/
#   f) Exit:  /exit
forge session start guard-planner --proxy "$FORGE_QA_OPENAI_PROXY"

# 2) Fork that planner into a dedicated supervisor session, then launch it.
# In Claude, do three things:
#   a) Optional human-friendly label:  /rename guard-sup
#   b) Send one message so Claude materializes the session:
#        Reply with this exact phrase: supervisor ready
#   c) Exit:  /exit
forge session fork guard-planner --name guard-supervisor --no-launch
forge session resume guard-supervisor

# 3) Fork the planner into a direct/no-proxy executor session.
# --no-proxy overrides the parent's proxy routing so the executor talks
# to Anthropic directly, while inheriting the planner's conversation context.
forge session fork guard-planner --name guard-executor --no-proxy --no-launch
forge session set --session guard-executor policy.enabled true
forge session set --session guard-executor policy.supervisor.resume_id guard-supervisor
forge session set --session guard-executor policy.supervisor.proxy "$FORGE_QA_OPENAI_PROXY"

# 4) Verify the executor now points at the promoted supervisor, then launch it.
# The executor inherits the planner's conversation (via fork) but routes directly
# to Anthropic. In Claude, paste this executor prompt:
#   Create the file `src/supervisor_demo.py` with exactly this content:
#
#   def greet(name: str) -> str:
#       return f"hello, {name}"
#
#   Do not modify any other files. Do not add tests, docstrings, or imports.
#
# After Claude finishes, exit with:
#   /exit
FORGE_SESSION=guard-executor forge guard status
forge session resume guard-executor

# 5) Inspect the result and run a real supervisor check against the renamed session.
cat src/supervisor_demo.py
forge guard supervisor -f src/supervisor_demo.py -r guard-supervisor --json
echo "exit: $?"
```

- [ ] Planner and supervisor sessions launch successfully, and the supervisor session is renamed to `guard-sup` via
  `/rename`
- [ ] Executor forks planner with `--no-proxy` (inherits conversation context, routes to Anthropic directly), shows
  `Supervisor: Configured` with `resume_id: guard-supervisor` in `forge guard status`, then implements the exact tiny
  planned file
- [ ] `forge guard supervisor -f src/supervisor_demo.py -r guard-supervisor --json` returns structured output for the
  real tiny task (expected: aligned / exit 0)

### 13.8 Disable Policies

<!-- auto -->

```bash
forge guard disable

forge guard status
```

- [ ] All policies disabled
- [ ] Status confirms disabled state

---
