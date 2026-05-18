# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Claude Forge is a toolkit for Claude Code enhancement that consolidates multiple AI developer tools (proxy, session
manager, status line, TDD guard) into a unified monorepo. The architecture follows a "glue approach" - building
connective tissue between specialized tools rather than a monolithic application.

## Development Commands

```bash
# Install dependencies
uv sync

# Run tests (ALWAYS use make - handles prerequisites)
make test-unit              # Fast unit tests (no Docker)
make test-integration       # Docker-based integration tests (auto-starts local LiteLLM)
make test                   # Full suite

# Why use make? It ensures prerequisites:
# - Builds Docker images if missing
# - Starts local LiteLLM on test port (4001) if needed
# - Uses litellm-gemini-test template for test isolation (port 4001)
#
# Advanced: Direct pytest (only AFTER running make once to set up prerequisites)
# uv run pytest tests/src -m "not integration" -v
# uv run pytest tests/integration -v

# Code quality (run `make pre-commit` before every commit)
make lint                  # Run ruff linter
make format                # Run ruff formatter
make type-check            # Run mypy
make pre-commit            # All pre-commit hooks
make clean                 # Remove caches

# Alternative: Direct tool usage
uv run ruff check src/
uv run ruff format src/
uv run mypy src/
pre-commit run --all-files
```

## Git Branching

- **`main`**: Primary branch. All PRs target `main`.
- **Feature branches**: Branch from `main`, PR back into `main`.

## Git Hooks

Pre-commit hooks reformat code (black, isort) and **strip emoji from staged files** (personal `normalize-text` hook).
Use `\U` escape sequences (e.g., `"\U0001F504"`) for emoji that must survive commits. After edits, run `git add -u` to
re-stage auto-formatted files. If a commit fails due to formatting, re-stage and retry without asking.

## Architecture

### File-Based State System (Core Design)

Forge uses a three-tier file-based state system instead of a database:

1. **Session manifest** (per-session): `.forge/sessions/<name>/forge.session.json` - contains intent (what session
   should be) and confirmed state (what Claude Code actually did). Multiple sessions can coexist per worktree.
2. **Proxy registry** (global): `~/.forge/proxies/index.json` - running proxies (template, base_url, pid).
3. **Runtime truth**: Live proxy introspection via `ANTHROPIC_BASE_URL`.

### Directory Structure

```
src/forge/
├── cli/        # Click-based CLI commands (forge session, forge proxy, etc.)
│   └── hooks/  # Hook handlers invoked by Claude Code
├── config/     # Configuration loading and proxy templates
├── core/       # Shared libraries (auth, models, state, llm, workqueue, reactive)
├── guard/      # Policy enforcement (TDD, coding standards, semantic supervisor)
├── install/    # Extension installer and tracking
├── proxy/      # Model routing proxy
├── review/     # Multi-model review engine (fan-out, adversarial)
├── search/     # Transcript search (BM25 index)
├── session/    # Session manager (worktrees, artifacts, resume)
└── sidecar/    # Docker sidecar mode (proxy + Claude in container)
```

### Shared Libraries (`src/forge/core/`)

- `forge.core.auth` - Credential resolution (env > `~/.forge/credentials.yaml`), template-to-secrets mapping
- `forge.core.llm` - Async-first LLM client abstraction (see design_appendix.md §J)
- `forge.core.models` - Model catalog with templates/tiers
- `forge.core.state` - State read/write operations
- `forge.core.workqueue` - File-based async work queue
- `forge.core.reactive` - Shared reactive library (session runner, throttle cache, tagger)

### Key Concepts

- **Templates**: Operational profiles that map to proxy ports (e.g., `litellm-gemini` on port 8084)
- **Tiers**: User-facing abstraction (`haiku`/`sonnet`/`opus`) that maps to backend models
- **Intent vs Confirmed**: Session manifest separates what Forge requested from what Claude Code actually did

## Implementation Status

Test suite has ~3,900 tests with Docker-based isolation. Key capabilities: multi-model proxy routing, session management
with resume/handoff, policy engine (TDD + semantic supervisor), search, workflow runners (fan-out, adversarial), skills
architecture, and interactive manual testing (`/forge:smoke-test`, `/forge:walkthrough`, `/forge:qa`).

**Install profiles**: `standard` (default) includes most skills. `full` adds `/forge:qa` (Docker-based QA).

See [design.md](docs/design.md) for architecture details.

## Design & Implementation

When the user describes a new concept (e.g., 'backend', 'work queue'), treat it as a FIRST-CLASS architectural concept
unless told otherwise. Do not reduce user-defined abstractions to internal implementation details. Ask for clarification
if scope is unclear rather than assuming minimal scope.

## Critical Thinking on User Input

When the user (or another AI model) provides feedback, corrections, claims, or design notes, do not blindly accept them.
Instead:

1. **Verify claims against the codebase** — check that referenced behavior, files, or patterns actually exist as
   described
2. **Reason through the implications** — consider whether the suggested change is consistent with existing architecture
3. **Push back when warranted** — if evidence contradicts the user's claim, say so clearly with specifics
4. **Ask clarifying questions** — if a claim is ambiguous or untestable, ask before assuming it's correct

**Especially in planning mode**: When the user provides feedback on a plan, independently verify their corrections
before incorporating them. A wrong assumption accepted during planning cascades into a flawed implementation. Treat plan
reviews as a dialogue, not a dictation.

The user values being challenged over being agreed with. Sycophantic acceptance leads to wasted work and subtle bugs.

## Code Reviews

When performing code reviews, do a COMPLETE first pass covering ALL findings before presenting results. Do not present a
partial subset — the user expects comprehensive coverage in a single pass.

## Editing Discipline

When editing documents or code, preserve the user's preferred terminology. Do not replace domain-specific terms (e.g.,
fact_id, orchestration) unless explicitly asked. When in doubt, ask before renaming.

## Guidelines (load into context)

@docs/developer/coding-standards.md @docs/developer/testing-guidelines.md @docs/developer/documentation-guidelines.md

## Key Design Documents

- `docs/design.md` - Unified design and migration plan (canonical)
- `docs/design_appendix.md` - Reference details (schemas, config tables)
- `docs/end-user/` - End-user guides (sessions, proxies, hooks, configs)

## UX Guidelines

### Error Handling

Keep user-facing error messages simple and accurate. Do not suggest installation methods or workarounds that don't apply
to this project's setup. When fixing errors, match the existing error message style in the codebase.

### Console Output Formatting

**Tips and hints** — Use `Tip:` prefix with dim styling for helpful suggestions:

```python
# Standard format for tips
console.print("[dim]Tip: Use --force to override.[/dim]")
console.print(f"\n[dim]Tip: Run 'forge session resume {name}' to continue.[/dim]")
```

**Do NOT use:**

- `Hint:` — inconsistent, slightly condescending tone
- Unprefixed suggestions — harder for users to scan/recognize

**Other output categories** (no prefix needed):

- Informational: `[dim]Already up to date.[/dim]`
- Status: `[dim]Backup: {path}[/dim]`
- Dry-run: `[dim](dry-run)[/dim] Would patch...`
- Next steps: `\n[dim]Next steps:[/dim]` followed by bullet list
