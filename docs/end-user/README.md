# End-User Guides

How to use Forge features. Each guide is self-contained; start here for the overview.

## Why Launch Through Forge?

Running `claude` directly works, but you lose session tracking. Forge wraps Claude Code to add:

- **Session tracking** -- named sessions with artifacts, plans, and transcripts
- **Session resume** -- AI-curated handoff when context fills up
- **Hook-driven capture** -- plan snapshots, transcript archival on exit
- **Status line** -- proxy, session, and policy info in the Claude UI
- **Policy enforcement** -- TDD, coding standards, semantic supervisor
- **Search** -- `forge search` across past sessions
- **Handoff agent** -- auto-updates project docs on session exit

These features require launching through Forge because they depend on `FORGE_SESSION` being set, hooks being wired, and
the session manifest existing. Running `claude` directly bypasses all of this.

**You don't need a proxy to benefit.** `forge session start` defaults to direct mode (Anthropic API), giving you
everything above without any proxy setup.

## The "Day 1" Workflow

### A. Install extensions

```bash
forge extension enable    # Installs hooks, skills, status line into Claude Code
```

### B. Launch Claude

The simplest path -- no proxy, no API key setup needed (uses your existing Claude subscription):

```bash
forge session start
```

This creates a managed Forge session (auto-named), launches Claude, and gives you session tracking, hooks, and the
status line.

### C. (Optional) Add multi-model routing

If you want to route through other providers (Gemini, GPT, etc.):

```bash
# Store your credentials (API keys + connection values)
forge authentication login

# Create a proxy (OpenRouter direct, no LiteLLM needed)
forge proxy create openrouter-anthropic

# Verify upstream connectivity (optional, recommended on first setup)
forge proxy start openrouter-anthropic --smoke-test

# Launch with proxy routing
forge session start --proxy openrouter-anthropic
```

See [proxies.md](proxies.md) for templates, tier mappings, and per-tier hyperparameter tuning. See
[auth.md](auth.md#which-auth-do-i-need) for which credentials each workflow needs.

### D. Resume when context fills up

```bash
# AI-selected highlights (best for long sessions)
forge session resume my-feature --fresh --strategy ai-curated

# Structured skeleton (faster, no LLM call)
forge session resume my-feature --fresh --strategy structured

# Lossless: carry full conversation (lost on /compact)
forge session resume my-feature --fresh --resume-mode native
```

### E. Optional: Enable large context windows

When routing through a proxy, Forge sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to match the routed model's context window.
No patching required.

### F. Store credentials

```bash
forge authentication login               # Prompt for API keys, store in ~/.forge/credentials.yaml
forge authentication status              # Show where each credential comes from (env, file, missing)
```

See [auth.md](auth.md) for profiles and credential resolution.

## Feature Guides

### Sessions -- Named Work Units

`forge session start` creates a managed Forge session (1:1 with the Claude process). Sessions track intent, artifacts,
and confirmed state:

```bash
forge session start                                            # Auto-named, direct to Anthropic
forge session start quick-fix                                  # Named, direct to Anthropic
forge session start my-feature --proxy openrouter-anthropic    # With proxy routing
forge session resume my-feature                                # Reattach to conversation
forge session show my-feature                                  # Session details
```

See [sessions.md](sessions.md) for worktrees, fork, incognito, and `%` commands.

### Policies -- Code Quality Gates

Enable TDD enforcement, coding standards checks, or a semantic supervisor that verifies alignment with your plan:

```bash
forge guard enable --bundle tdd                        # Deterministic TDD policy
forge guard supervise planner                          # Semantic plan supervision
forge session fork planner --name executor --supervise # Wire at fork time
forge guard supervise --off                            # Suspend (preserves config)
forge guard supervise --on                             # Resume
forge guard supervise --reload                         # Reload plan after changes
```

See [policies.md](policies.md).

### Skills -- Review, Understand, Panel

Skills teach Claude how to compose Forge capabilities. Model family is auto-detected from session context:

```bash
/forge:review src/forge/session/           # code review
/forge:review-docs docs/design.md          # document review
/forge:understand src/forge/core/ops/      # explain code structure
/forge:panel src/forge/session/ --code     # multi-model code review
```

See [skills.md](skills.md).

### Workflows -- Multi-Model CLI Engine

The CLI engine behind skills. Fan out reviews to multiple models, get adversarial debate, or deep analysis:

```bash
forge workflow panel src/forge/session/ --code
forge workflow debate "Should we rewrite the core in Rust?"
forge workflow analyze "What are the failure modes of the handoff agent?"
```

See [workflows.md](workflows.md).

### Hooks -- Lifecycle & Artifacts

Forge hooks capture session artifacts (plans, transcripts) and enforce policies at tool-use boundaries. Installed
automatically by `forge extension enable`.

See [hooks.md](hooks.md).

### Handoff Agent -- Automatic Memory Docs

A headless agent is queued at session end and runs on the next Forge CLI startup to update designated project docs
(checklists, changelogs, pattern files) based on what happened in the session.

See [handoff.md](handoff.md).

### Search -- Transcript Search

Search across past session transcripts:

```bash
forge search -q "proxy routing bug"
forge search rebuild-index
```

See [search.md](search.md).

### Configuration

Runtime preferences live in `~/.forge/config.yaml`. Claude Code settings customizations live in
`~/.forge/claude.preset.json`:

```bash
forge config show
forge config set context_limit=1000000
forge claude preset edit
```

See [configs.md](configs.md).

### Verification -- Installation Testing

Three tiers of verification:

| Skill                | What it does                        |
| -------------------- | ----------------------------------- |
| `/forge:smoke-test`  | Read-only health check (30 seconds) |
| `/forge:walkthrough` | Interactive feature tour (hermetic) |
| `/forge:qa`          | Full Docker-based QA                |

See [manual-testing.md](manual-testing.md).
