# Testing Guidelines

Testing standards for Claude Forge.

---

## Test Organization

4-layer layout in `tests/`:

```
tests/
├── src/                    # Unit + component integration (CIT)
│   ├── session/            # Mirrors src/forge/session/
│   │   ├── test_*.py                    # Unit: Single module
│   │   ├── test_*_integration.py        # CIT: 2-3 comps (cached LLMs)
│   │   └── conftest.py                  # Fixtures
│   └── .../                # Same pattern
│
├── integration/            # E2E integration tests (E2EIT)
│   └── test_*.py           # Full workflows (real LLMs)
│
├── regression/             # Regression
│   └── test_bug_*.py       # Bug validation (one file/bug)
│
└── fixtures/               # Test data
```

---

## Test Placement Rules

### 1. Unit Tests: `tests/src/package/test_module.py`

```
src/forge/session/store.py -> tests/src/session/test_store.py
src/forge/core/models.py -> tests/src/core/test_models.py
```

- **Rule**: Mirror source structure exactly (1:1)

### 2. Component Integration Tests (CIT): `tests/src/package/test_*_integration.py`

```
tests/src/session/test_session_integration.py   # Session + Store + Index
tests/src/proxy/test_proxy_integration.py       # Proxy + Auth + Models
```

- **Purpose**: Verify 2-3 components
- **Location**: Co-located with unit tests (same package)
- **Marking**: `pytestmark = pytest.mark.integration` at file level (**REQUIRED**)

### 3. End-to-End Integration Tests (E2EIT): `tests/integration/test_*.py`

- **Purpose**: Verify full workflows across components
- **Location**: In `tests/integration/`
- **Marking**: `pytestmark = pytest.mark.integration` at file level (**REQUIRED**)

> **Why mark integration tests?** Without marks, slow tests pollute unit tests; `pytest -m "not integration"` stays
> fast.

**Exception: mixing `integration` and `slow` in one file.** If a file mixes integration and network tests
(`@pytest.mark.slow`), mark `@pytest.mark.integration` at the **class level**. Then `-m integration` runs fast tests and
`-m slow` runs network tests. See `tests/integration/core/test_auth_credential_resolution.py`.

### 4. Regression Tests: `tests/regression/test_bug_*.py`

- **Purpose**: Prevent regressions, validate performance
- **Naming**: `test_bug_<id>_<description>.py` where `<id>` is the review/phase ID (e.g.,
  `test_bug_h7_streaming_index.py`, `test_bug_214_hyperparams.py`)
- **Marking**: `pytestmark = pytest.mark.regression` at file level (**REQUIRED**)
- One file per bug/perf issue
- Docstring: bug ID + regression description

#### Regression Test Mandate

**Every bug fix MUST include a regression test** — especially for corruption, silent loss, security issues, races, and
cross-module bugs.

**Process**: Write a failing test → fix bug → verify pass → place in `tests/regression/`.

**Good regression tests:**

- Reproduce the **exact failure** (not general functionality)
- Assert on the **specific failure mode** (e.g., "leading zeros preserved", not just "it works")
- Are fast (prefer unit; no Docker/LLM unless integration-level)
- Docstring: bug ID, root cause, affected file(s)

**Optional** (recommended): typos/cosmetics, refactors covered by existing unit tests, test-only bugs.

---

## Running Tests

### Quick Start

**ALWAYS use `make` targets** — they handle prerequisites:

```bash
# Unit tests (host, no Docker)
make test-unit              # ~20s

# Integration (Docker; infra)
make test-integration       # ~2-3 min

# Full suite
make test                   # ~3 min

# Other targets
make lint                   # Ruff lint
make format                 # Ruff format
make type-check             # mypy
make pre-commit             # pre-commit
make clean                  # remove caches
```

**Why `make` is required:**

- Builds Docker images
- Starts LiteLLM on 4001 (not dev 4000)
- Uses `litellm-gemini-test` for isolation
- Fails loudly on missing prereqs ("never skip")

### Advanced: Direct pytest (after `make` ran once)

**WARNING:** Direct `pytest` assumes `make` ran; integration fails if LiteLLM isn't on 4001.

```bash
# Unit tests only
uv run pytest tests/src -m "not integration" -v

# Integration tests (assumes make ran once)
uv run pytest -m integration

# Regression tests
uv run pytest -m regression

# Shared/remote LiteLLM infrastructure tests
uv run pytest -m remote_litellm

# Specific file
uv run pytest tests/src/session/test_store.py -v

# Filter
uv run pytest -k test_proxy
```

### Real-Claude E2E Tests (`@pytest.mark.slow`)

A separate tier of tests that use **real Claude Code** with **real LLM calls** in Docker. These verify hook integration,
session lifecycle, and supervisor flows that can't be tested with mock Claude.

```bash
# Run all real-Claude tests (requires ANTHROPIC_API_KEY + Docker)
./scripts/test-integration.sh tests/integration/docker/test_real_claude_hooks.py tests/integration/docker/test_supervisor_e2e.py -v

# Run a specific real-Claude test
./scripts/test-integration.sh -k test_plan_fork_wire_supervisor_check -v
```

**Requirements:**

- `ANTHROPIC_API_KEY` environment variable (or in `.env`). Tests fail loudly if missing (never skip).
- Docker with the `forge-claude-test` image (`make test-integration` builds it).
- Real Claude Code binary in the container (the test restores it from backup after `forge_workspace` mocks it).

**Design rules:**

- Mark with `@pytest.mark.slow` + `@pytest.mark.integration` + `@pytest.mark.docker_in`
- Use **narrow assertions** — assert on field presence and exit codes, not LLM output content
- Use shared helpers `setup_real_claude()` and `run_claude_print()` from `tests/integration/docker/conftest.py`
- Keep prompts minimal to reduce cost and execution time (~30-60s per test)
- These are for **release validation**, not CI gating

### Remote LiteLLM E2E Tests (`@pytest.mark.remote_litellm`)

Remote LiteLLM tests are the opt-in path for maintainers with shared/internal LiteLLM infrastructure. They require
`LITELLM_API_KEY` and `LITELLM_BASE_URL`, and should also be marked `integration` and `slow`. OpenRouter real-API tests
use the existing `slow` marker; do not add a separate paid marker.

**Current real-Claude tests:**

| File                        | What it tests                                                |
| --------------------------- | ------------------------------------------------------------ |
| `test_real_claude_hooks.py` | SessionStart hook sets UUID, Stop hook records transcript    |
| `test_supervisor_e2e.py`    | Full plan -> fork -> wire supervisor -> on-demand check flow |

### Requirements

- **Unit tests:** No Docker (host)
- **Integration tests:** Docker required (session-scoped)
  - **Docker memory:** ≥6GB in Docker Desktop (Settings > Resources > Memory)
- **First run:** Builds images (~2-3 min), then cached

---

## Writing Tests

**Use pytest** (not unittest):

```python
# Good
def test_feature(workspace_fixture):
    result = function_under_test(workspace_fixture)
    assert result == "expected"


# Bad
class TestFeature(unittest.TestCase):
    def test_feature(self):
        self.assertEqual(result, "expected")
```

**Why pytest?** Better assertions, fixtures, and parametrization; `TestCase` adds boilerplate without benefit.

---

## Test Maintenance Policy

**Never skip tests.** Must pass or fail cleanly (actionable).

### Core Principle: Failures Are Signals

A failing test means:

- **Regression**: code broke
- **Expectation shift**: assumptions changed
- **Flakiness**: nondeterminism to fix
- **Removal**: feature removed → delete the test

### What to Do When Tests Fail

| Situation            | Don't                         | Do                                              |
| -------------------- | ----------------------------- | ----------------------------------------------- |
| Fails after refactor | Skip with `@pytest.mark.skip` | Fix code or update expectations                 |
| Flaky (intermittent) | Skip "for now"                | Fix root cause (race, timing, async, isolation) |
| Feature removed      | Skip/disable                  | Delete the test                                 |
| Slow test            | Skip in CI                    | Optimize, or move layers (unit/integration/e2e) |
| Missing local setup  | Skip temporarily              | Fix setup (deps, servers, keys)                 |

### Forbidden Patterns

```python
# ✘ DO NOT: Skip broken/flaky/platform-only
@pytest.mark.skip(reason="Flaky")
def test_session_resume():
    ...

@pytest.mark.skipif(sys.platform == "darwin", reason="macOS-only")
def test_feature():
    ...

# ✔ DO: Fix the test or the implementation
def test_session_resume():
    # Fix race; make deterministic
    ...

def test_feature():
    # Make cross-platform (or add explicit tests below)
    ...
```

### When Tests Become Obsolete

From `coding-standards.md`:

**Delete Obsolete Tests**: If functionality is removed, delete its tests (don't skip).

- If a test validates removed code → delete it
- If behavior moved → update the test to match the new location/contract
- Don't accumulate skips; delete to keep the suite maintainable

### Enforcement

**No skips allowed.** Tests must pass or fail cleanly:

- Missing deps → Install (add to requirements)
- Missing env config → Fix env (API keys, servers)
- Missing infra → Fix setup, don't skip

**Rationale**: Skipping leaves code untested. If a test can't run, fix env/root cause.

**Current scope**: Mac laptops (some Docker). No CI or multi-platform yet; revisit if added.

---

## Testing Philosophy: Real Over Mock

> **Philosophy**: Real integrations catch bugs mocks miss. Mocks encode *assumptions*; real calls test *behavior*.
> Prefer real calls with caching/sharing.

Applies to **all external deps**:

| Domain          | Mock Approach (Avoid)    | Real Approach (Preferred)                |
| --------------- | ------------------------ | ---------------------------------------- |
| **LLM calls**   | Mock response objects    | Real cached LLM calls (cheap models)     |
| **Claude Code** | Mock hook events         | Real Claude Code in Docker container     |
| **Filesystem**  | `tmp_path` on host       | Shared workspace in container            |
| **Git repos**   | Create new repo per test | Session-scoped repo, reset between tests |

**When to mock** (sparingly):

- Specific error conditions (`side_effect = TimeoutError()`)
- Pure-logic unit tests (no externals)
- When real integration is impractical

---

## Monkeypatch Policy

Prefer **shared fixtures** over inline `monkeypatch`. If 3+ tests patch a target the same way, extract a fixture in
`conftest.py`.

**Before patching**, check existing fixtures:

- **Autouse** (`tests/conftest.py`): `isolate_forge_home` sets `FORGE_HOME`, `isolate_claude_home` sets `CLAUDE_HOME` —
  don't re-set unless you need a different path (comment why).
- **Proxy** (`tests/src/proxy/conftest.py`): `orchestrator`, `orch_stubs`, `orch_registry`, `orch_health`,
  `mock_registry_path`, `no_proxy_id_env`, `server_stubs` — see docstrings.

**Inline monkeypatch** is for one-off patches (callbacks, trackers, error injection). Repeated identical patches across
tests = missing fixture.

**Module-level test doubles**: If a fake class (e.g., `_Proc`) appears in multiple tests in one file, define it once at
module level with parameterized construction — don't duplicate inline.

---

## LLM Testing Strategy

| Scenario                | Approach                  | Why                                             |
| ----------------------- | ------------------------- | ----------------------------------------------- |
| CIT with LLM            | Cached LLM client fixture | Real integration, cached = fast + deterministic |
| SDK-based tests         | Caching proxy fixture     | Caches Claude SDK calls via proxy               |
| Testing error handling  | Mock                      | Need to simulate specific failures              |
| Unit tests (logic only) | Mock or no LLM            | Testing code paths, not integration             |

---

## Docker E2E Testing Strategy

Use Docker for Claude Code, filesystem ops, and full workflows:

**Rules of thumb:**

1. **If it needs real Claude Code** → Don't mock it; install it in a container
2. **If it creates temp directories/repos** → Consider doing it in a container
3. **Share fixtures widely** → Session-scoped containers and workspaces

**Fixture hierarchy (maximize reuse):**

```
Session-scoped (once/run)
├── base_container          # Claude Code + Forge container
├── synced_container        # + `uv sync`
└── base_git_repo           # + git repo in `/workspace`

Function-scoped (per test; reuses session resources)
├── clean_workspace         # Reset `/workspace` (git clean)
├── workspace_with_forge    # + `forge init`
└── workspace_with_session  # + session started
```

**Pattern: Use Docker fixture helpers**

```python
@pytest.fixture(scope="session")
def base_git_repo(synced_container: ContainerLike) -> ContainerLike:
    """Session-scoped repo."""
    synced_container.exec("""
        mkdir -p /workspace && cd /workspace
        git init && git config user.email "test@test.com" && git config user.name "Test"
        echo "# Test" > README.md && git add . && git commit -m "init"
    """)
    return synced_container

@pytest.fixture
def clean_workspace(base_git_repo: ContainerLike) -> ContainerLike:
    """Per-test: reset workspace."""
    base_git_repo.exec("cd /workspace && git clean -fdx && git checkout -- . && rm -rf .claude .forge")
    return base_git_repo

def test_something(clean_workspace: ContainerLike):
    """Test with helpers."""
    # Write files
    clean_workspace.write_file("$HOME/.forge/config.yaml", "key: value")
    clean_workspace.write_json("$HOME/.forge/data.json", {"version": 1})

    # Run
    result = clean_workspace.exec("forge session start test")
    assert result.returncode == 0

    # Verify
    data = clean_workspace.read_json("$HOME/.forge/output.json")
    assert data["status"] == "success"
```

**Why this works:** Repo init ~100ms; reset ~10ms. 50 tests: ~5s → ~0.5s, plus container isolation.

---

## Docker Fixture Helper Methods

Integration tests use `ContainerLike` helpers from `tests.fixtures.docker` (no shell escaping).

### Available Methods

#### File Operations

**write_file(path, content)** - Write text (heredoc; no escaping)

```python
workspace.write_file("$HOME/.forge/config.yaml", """
key: value
nested:
  field: data
""")
```

**write_json(path, data)** - Write JSON (serializes)

```python
registry = {"version": 1, "proxies": {...}}
workspace.write_json("$HOME/.forge/registry.json", registry)
```

**read_file(path)** - Read text (error if missing)

```python
content = workspace.read_file("$HOME/.forge/config.yaml")
assert "key: value" in content
```

**read_json(path)** - Read JSON

```python
data = workspace.read_json("$HOME/.forge/registry.json")
assert data["version"] == 1
```

#### Directory Operations

**mkdir(path, parents=True)** - Create directory (parents)

```python
workspace.mkdir("$HOME/.forge/proxies/my-proxy", parents=True)
```

**file_exists(path)** - Check file exists (bool)

```python
if workspace.file_exists("$HOME/.forge/config.yaml"):
    content = workspace.read_file("$HOME/.forge/config.yaml")
```

#### Raw Command Execution

**exec(command, timeout=60)** - Run command

```python
result = workspace.exec("forge proxy list")
assert result.returncode == 0
assert "my-proxy" in result.stdout
```

### Migration Pattern: Before → After

**Before (quote escaping):**

```python
# Write files
escaped_content = content.replace("'", "'\\''")
workspace.exec(f"printf '%s' '{escaped_content}' > $HOME/.forge/config.yaml")

# Write JSON
registry_json = json.dumps(registry).replace("'", "'\\''")
workspace.exec(f"printf '%s' '{registry_json}' > $HOME/.forge/registry.json")

# Read file
result = workspace.exec("cat $HOME/.forge/config.yaml")
content = result.stdout

# Read JSON
result = workspace.exec("cat $HOME/.forge/registry.json")
data = json.loads(result.stdout)
```

**After (clean):**

```python
# Write files
workspace.write_file("$HOME/.forge/config.yaml", content)

# Write JSON
workspace.write_json("$HOME/.forge/registry.json", registry)

# Read file
content = workspace.read_file("$HOME/.forge/config.yaml")

# Read JSON
data = workspace.read_json("$HOME/.forge/registry.json")
```

### Why These Helpers Exist

**Problem:** Shell escaping is error-prone

- Single quotes disable expansion: `'$HOME'` → literal `$HOME`
- Double quotes expand but need escaping: `"$HOME"` → `/root`
- Nested quotes need escaping: `.replace("'", "'\\''")` → fragile

**Solution:** Helpers use heredoc + single-quoted delimiter

- Content: Single-quoted delimiter (`'FORGE_EOF'`) blocks expansion
- Path: Double-quoted for variable expansion (`"$HOME"` → `/root`)
- Result: No escaping needed

**Security note:** Double-quoted paths allow substitution (`$(...)`/backticks). Safe for trusted test paths; for
untrusted input, use arg arrays/escaping.

### Writing New Integration Tests

**Pattern:**

1. Use `clean_workspace` (ContainerLike)
2. Use helpers for file/dir ops
3. Use `exec()` for CLI only
4. Use `read_json()` to verify outputs

**Example:**

```python
def test_proxy_create(clean_workspace: ContainerLike):
    """Proxy creation."""
    # Setup
    clean_workspace.mkdir("$HOME/.forge/proxies", parents=True)
    registry = {"version": 1, "proxies": {}}
    clean_workspace.write_json("$HOME/.forge/proxies/index.json", registry)

    # Run
    result = clean_workspace.exec("forge proxy create litellm-openai --no-start")
    assert result.returncode == 0

    # Verify
    updated_registry = clean_workspace.read_json("$HOME/.forge/proxies/index.json")
    assert "litellm-openai" in updated_registry["proxies"]
```

---

## Interactive Manual Testing (`/forge:smoke-test`, `/forge:walkthrough`, `/forge:qa`)

Automated tests miss UX/latency/real-system failures. Three skills provide three tiers of verification.

### Location

```
src/skills/smoke-test/
├── SKILL.md                          # Read-only health check runner
└── scripts/
    └── smoke-test.sh                 # Read-only probes + mtime assertions

src/skills/walkthrough/
├── SKILL.md                          # Interactive walkthrough (host-based)
├── resources/
│   └── checklist.md      # Annotated checklist
└── scripts/
    ├── setup-test-repo.sh           # Hermetic repo setup
    ├── run-in-repo.sh              # Safety wrapper (4 gates)
    └── walkthrough-state.py        # Deterministic state machine

src/skills/qa/
├── SKILL.md                          # Full QA in Docker container
├── resources/
│   ├── checklist.md                  # Index (metadata + section map)
│   ├── checklist/                    # One file per section
│   │   ├── 0-enable.md … 20-cleanup.md
│   └── report-template.md           # Report template
└── scripts/
    ├── start-container.sh           # Docker lifecycle
    └── walkthrough-state.py        # Deterministic state machine (byte-identical copy)
```

### Running

```bash
# In Claude Code:
/forge:smoke-test                           # Read-only health check
/forge:walkthrough                          # Walkthrough (hermetic functional test)
/forge:walkthrough --setup-only             # Create test repo only
/forge:qa                                   # Docker QA
/forge:qa --from 4.1                        # Resume from section 4.1
```

### Safety model

Risky operations go through safety scripts. The agent handles read-only checks directly.

| Mode                 | Safety layer                    | Isolation                                          |
| -------------------- | ------------------------------- | -------------------------------------------------- |
| `/forge:smoke-test`  | `smoke-test.sh`                 | Read-only probes; mtime snapshot before/after      |
| `/forge:walkthrough` | `run-in-repo.sh` (agent-driven) | 4 safety gates; agent mtime verification           |
| `/forge:qa`          | `start-container.sh` + Docker   | OS-level isolation; `docker exec` for all commands |

### Install profiles

`/forge:qa` requires the `full` install profile (`forge extension enable --profile full`). The walkthrough and
smoke-test skills install with any profile that includes the SKILLS module (standard or higher).

### When to run

- After installing Forge: `/forge:smoke-test` then `/forge:walkthrough`
- After upgrading Forge: walkthrough catches regressions
- Before releases: `/forge:qa` for full Docker QA

### Updating the QA checklist

When adding/changing a feature, update the QA checklist:

- **Index**: `src/skills/qa/resources/checklist.md` (metadata + section map)
- **Content**: `src/skills/qa/resources/checklist/*.md` (one file per `## N.` section)

1. **Add tests** under `## N.` using `### N.X` + `- [ ]`.
2. **Annotate** each subsection (HTML comment):
   - `<!-- auto -->` -- fully automatable (Bash + checks)
   - `<!-- human:confirm -->` -- agent runs command, user verifies output
   - `<!-- human:guided -->` -- agent shows instructions, user performs action (interactive input, editor, live Claude)
   - `<!-- requires: proxy,docker,api-key -->` -- skip if infra missing
   - `<!-- destructive -->` -- modifies real system, needs consent
   - No annotation = human verification
3. **Update index header** `<!-- test-count: ~N -->` and `<!-- last-updated: YYYY-MM-DD -->` in `checklist.md`.

---

## Type Checking

```bash
uv run mypy src/               # all
uv run mypy src/forge/session/ # module
```

**Rules**:

- All `src/` and `tests/` must pass mypy
- Tests relax mypy (`disallow_untyped_defs = false`) to reduce fixture noise
- Fix errors; avoid `# type: ignore` unless unavoidable
