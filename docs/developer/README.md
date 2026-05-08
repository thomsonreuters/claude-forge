# Developer Guide

Setup, testing, and architecture reference for Claude Forge contributors.

## Setup

```bash
# Clone and install dependencies
git clone <repo-url>
cd claude-forge
uv sync

# Install pre-commit hooks
pre-commit install

# Editable install for development
./scripts/setup.sh --local

# Set up environment files
cp .env.example .env
# Edit .env with your API keys and credentials (secrets only)
```

## Branching

- **`main`**: Primary branch. All PRs target `main`.
- **Feature branches**: Branch from `main`, PR back into `main`.

## Running Tests

### Environment Setup for Tests

Integration tests use **real credentials** to test against actual APIs. Your `.env` file (secrets only) is used by both
dev and tests:

- **Development**: Uses `litellm-gemini-local` template (port 4000)
- **Tests**: Uses `litellm-gemini-test` template (port 4001, auto-started by `make test-integration`)

No separate `.env.test` file needed - port configuration is handled by templates.

### Make Targets

**ALWAYS use `make` targets** - they handle prerequisites automatically (Docker images, local LiteLLM on test port):

```bash
# Fast unit tests (~2,600 tests, ~30s)
make test-unit

# Integration tests (auto-starts local LiteLLM on port 4001, builds Docker images)
make test-integration

# Full test suite
make test
```

**Why `make` is required:**

- Ensures Docker images are built
- Auto-starts local LiteLLM on test port (4001, isolated from dev port 4000)
- Uses `litellm-gemini-test` template for test isolation (port 4001)
- Integration tests fail loudly when prerequisites missing (never skip)

**Advanced: Direct `pytest` usage** (only after running `make` once to set up prerequisites):

```bash
# Unit tests only
uv run pytest tests/src -m "not integration" -x

# Specific integration test (assumes make test-integration ran once)
uv run pytest tests/integration/cli/test_proxy_commands_integration.py -v
```

### When Docker is needed

E2E tests in `tests/integration/` test against **real Claude Code binaries**:

```bash
# Run tests inside Docker container
docker build -f docker/Dockerfile.forge -t forge-test .
docker run --rm forge-test uv run pytest tests/integration/
```

### Test Organization

| Layer      | Location                     | Runs on host? | Purpose                                        |
| ---------- | ---------------------------- | ------------- | ---------------------------------------------- |
| Unit       | `tests/src/`                 | Yes           | Single module tests (mirrors `src/` structure) |
| CIT        | `tests/src/*_integration.py` | Yes           | Component integration (2-3 modules)            |
| E2E        | `tests/integration/`         | Docker        | Full workflows with real Claude Code           |
| Regression | `tests/regression/`          | Yes           | Bug fix validation                             |

See [testing-guidelines.md](testing-guidelines.md) for details.

### Docker Test Fixtures: Helper Methods

Integration tests use Docker fixtures that provide helper methods for clean file operations. No more quote escaping!

**Available helpers (tests/fixtures/docker.py):**

```python
# File operations
workspace.write_file("$HOME/.forge/config.yaml", content)
workspace.write_json("$HOME/.forge/registry.json", {"version": 1, "data": {...}})
workspace.read_file("$HOME/.forge/config.yaml")
workspace.read_json("$HOME/.forge/registry.json")

# Directory operations
workspace.mkdir("$HOME/.forge/proxies", parents=True)
workspace.file_exists("$HOME/.forge/config.yaml")  # Returns bool

# Raw command execution (when needed)
workspace.exec("forge proxy list")
```

**Why these exist:** Eliminates shell quote escaping complexity. Before helpers:

```python
# OLD: Quote escaping hell
escaped_content = content.replace("'", "'\\''")
workspace.exec(f"printf '%s' '{escaped_content}' > $HOME/.forge/config.yaml")

# NEW: Clean and readable
workspace.write_file("$HOME/.forge/config.yaml", content)
```

**Pattern for new integration tests:**

1. Use `clean_workspace` fixture (provides ContainerLike)
2. Use helpers for file/dir operations
3. Use `exec()` only for running CLI commands
4. Use `read_json()` to verify output files

## Code Quality

**Run `make pre-commit` before every commit**

```bash
make lint                     # Ruff linter only
make format                   # Ruff formatter
make type-check               # mypy only
make pre-commit               # All pre-commit hooks (ruff, black, isort, mypy, mdformat, gitleaks)
```

## Architecture

Claude Forge follows a "glue" approach -- connecting specialized tools rather than building a monolith.

### Core Components

| Component       | Location             | Purpose                                    |
| --------------- | -------------------- | ------------------------------------------ |
| Session Manager | `src/forge/session/` | Named sessions, worktrees, artifacts       |
| Installer       | `src/forge/install/` | Extension installer and tracking           |
| Proxy           | `src/forge/proxy/`   | Model routing, tier mappings               |
| Guard           | `src/forge/guard/`   | Policy enforcement (TDD, coding standards) |
| Core Libraries  | `src/forge/core/`    | Shared auth, models, state, LLM client     |

### Key Concepts

- **Sessions** -- User workflow units with intent/overrides/confirmed state
- **Proxies** -- Proxy routing identities (base_url + port + template)
- **Templates** -- Pre-configured proxy profiles (litellm-openai, litellm-gemini, litellm-anthropic, etc.)

See [design.md](../design.md) for full architecture.

### File-Based State

Forge uses files instead of a database:

| File                                                | Purpose                     |
| --------------------------------------------------- | --------------------------- |
| `~/.forge/sessions/index.json`                      | Global session registry     |
| `.forge/sessions/<session_name>/forge.session.json` | Per-session manifest        |
| `~/.forge/proxies/index.json`                       | Proxy registry              |
| `~/.forge/proxies/<id>/proxy.yaml`                  | Per-proxy configuration     |
| `~/.forge/credentials.yaml`                         | File-based credential store |
| `~/.forge/config.yaml`                              | Runtime preferences         |

## Developer Docs (this directory)

| Document                                                   | Purpose                                       |
| ---------------------------------------------------------- | --------------------------------------------- |
| [coding-standards.md](coding-standards.md)                 | Python conventions, type safety, async        |
| [testing-guidelines.md](testing-guidelines.md)             | Test organization, Docker fixtures, real>mock |
| [documentation-guidelines.md](documentation-guidelines.md) | Doc structure, change log format, size limits |

## Project Docs

| Document                                    | Purpose                                             |
| ------------------------------------------- | --------------------------------------------------- |
| [design.md](../design.md)                   | Canonical architecture and design                   |
| [design_appendix.md](../design_appendix.md) | Reference details (schemas, tables)                 |
| [end-user/](../end-user/)                   | End-user guides (sessions, proxies, policies, etc.) |

## Common Tasks

### Adding a New CLI Command

1. Add command in `src/forge/cli/` (use Click)
2. Register in `src/forge/cli/main.py` (add to CLI group)
3. Add tests in `tests/src/cli/`

### Adding a Hook Handler

1. Implement in `src/forge/cli/hooks/`
2. Register in hook dispatcher (`src/forge/cli/hooks/commands.py`)
3. Add to installer's hook settings
