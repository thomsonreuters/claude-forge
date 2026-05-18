# Claude Forge

[![PyPI](https://img.shields.io/pypi/v/tr-claude-forge.svg)](https://pypi.org/project/tr-claude-forge/)
[![Python](https://img.shields.io/pypi/pyversions/tr-claude-forge.svg)](https://pypi.org/project/tr-claude-forge/)
[![License](https://img.shields.io/pypi/l/tr-claude-forge.svg)](LICENSE)

> [!WARNING]
> **Research Preview** -- Forge is under active development. APIs, commands, and file formats may change without notice
> between releases. Not recommended for production use.

**The missing connective tissue for [Claude Code](https://claude.ai/code).**

Forge is a CLI that wraps Claude Code, adding persistent sessions, multi-model routing, and autonomous verification. You
run `forge session start` instead of `claude`, and Forge handles the rest -- routing to your chosen model provider,
tracking state across sessions, and enforcing policies.

```bash
# Use Claude with session tracking (no proxy needed)
forge session start

# Or route through different model providers (after creating proxies -- see Quick Start)
forge session start planner --proxy openrouter-openai    # GPT for planning
forge session start --proxy openrouter-gemini            # Gemini for review
```

## Why Forge?

Claude Code talks to Anthropic and tracks conversations. Forge adds an operational layer on top:

- **Session Tracking** -- Named sessions that persist artifacts, plans, and transcripts. Works with or without a proxy.
- **Multi-Model Routing** -- Route to GPT, Gemini, or any model via OpenRouter or LiteLLM through a local proxy.
- **Cost Control** -- Proxy cost logs and spend caps keep metered API and multi-model workflow usage predictable.
- **Context Compatibility** -- When routing to models with different context windows, Forge sets the native
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` so compaction timing matches the routed model.
- **Autonomous Loops** -- Verification policies that keep Claude working until tests pass.
- **Session Resume** -- When context fills up, hand off to a fresh session with AI-curated history.
- **Policy Engine** -- TDD enforcement, coding standards, and semantic alignment checks.
- **Multi-Model Review** -- Fan out code reviews to multiple models, get adversarial consensus.

### Why launch through Forge?

Running `claude` directly bypasses session tracking. When you launch through Forge (`forge session start`), you get:

| Feature                | `claude` directly | `forge session start`                         |
| ---------------------- | ----------------- | --------------------------------------------- |
| Session tracking       | No                | Yes -- named sessions, artifacts, transcripts |
| Session resume         | No                | Yes -- AI-curated handoff to fresh context    |
| Status line            | No                | Yes -- proxy, session, policy info            |
| Hook-driven artifacts  | No                | Yes -- plan snapshots, transcript capture     |
| Policy enforcement     | No                | Yes -- TDD, coding standards, supervisor      |
| Search across sessions | No                | Yes -- `forge search` indexes transcripts     |
| Handoff agent          | No                | Yes -- auto-updates project docs on exit      |

Even without a proxy, `forge session start` gives you session tracking, hooks, and the status line (direct mode is the
default). The proxy adds multi-model routing on top. (`forge claude start` is also available as a bare launcher with
proxy routing only, no session state.)

## How it Works

Forge runs a local proxy that translates Claude Code's Anthropic API calls into requests for any LLM provider. Claude
Code connects to this proxy (via `ANTHROPIC_BASE_URL`), and Forge handles model selection, session state, and policy
enforcement.

```
Claude Code  -->  Forge Proxy (local)  -->  OpenRouter / LiteLLM  -->  Any LLM provider
                       |
                  Session state, policies, artifacts
```

**OpenRouter** templates call the OpenRouter API directly -- no LiteLLM needed. One API key gives access to Anthropic,
OpenAI, Google, Meta, and other models. **LiteLLM** templates route through a
[LiteLLM](https://github.com/BerriAI/litellm) proxy (remote or local subprocess).

**Direct mode** (the default) skips the proxy and talks to Anthropic directly. `forge session start` gives you session
tracking, hooks, and all Forge features except multi-model routing. Use `--proxy` to add routing.

## Requirements

- **Platform**: macOS or Linux
- **Python**: 3.11–3.13 (3.14 blocked on upstream `uvloop` wheels — see #1)
- **Claude Code**: installed and on PATH
- **Provider auth**: Claude Code login is enough for direct interactive sessions. Proxies and headless workflows need a
  supported API or gateway credential such as `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
  `OPENAI_API_KEY`, or LiteLLM auth.

## Quick Start

```bash
# Install Forge
pip install tr-claude-forge

# Or for development (editable install from local clone):
git clone https://github.com/thomsonreuters/claude-forge.git
cd claude-forge && pip install -e .

# Install extensions (hooks, skills, status line) into Claude Code
forge extension enable

# Launch Claude with session tracking (no proxy needed)
forge session start

# Or with multi-model routing via OpenRouter (easiest, no LiteLLM):
forge auth login -c openrouter                        # Store OPENROUTER_API_KEY
forge proxy create openrouter-anthropic               # Create and start a Claude-family proxy
forge session start --proxy openrouter-anthropic

# Optional: create default workflow proxies for GPT/Gemini review workers
forge proxy create openrouter-openai
forge proxy create openrouter-gemini

# Alternative: LiteLLM-based routing (shared/internal or local):
# forge auth login -c litellm-remote              # Store API key + base URL
# forge proxy create litellm-openai              # Connects to shared/internal LiteLLM
# forge proxy create litellm-openai-local        # Or start local LiteLLM
```

Once running, try `/forge:walkthrough` inside Claude Code for a guided tour in a sandboxed test environment.

### Upgrading from Pre-OSS Forge

Existing pre-OSS Forge installs are not supported in-place. If upgrading:

1. If Claude Code was previously patched, run `claude update` or reinstall Claude Code for a pristine binary.
2. Remove stale Forge state: `rm ~/.forge/installed.json`
3. Re-enable extensions: `forge extension enable`
4. If you had `FORGE_CONTEXT_LIMIT` in your shell config, remove it. Use `CLAUDE_CODE_AUTO_COMPACT_WINDOW` for native
   Claude Code behavior, or `forge config set context_limit=N` for Forge proxy fallback.

### Example Workflow: Plan, Execute, Review

With proxies configured, a typical feature workflow looks like:

```bash
# 1. Start a planning session with a high-reasoning model
forge session start planner --proxy openrouter-openai
# ... Claude creates a plan, you approve it, /exit

# 2. Fork the planner into a worktree with plan supervision
forge session fork planner --name executor --worktree --supervise
# ... Claude implements the plan; supervisor auto-checks every Write/Edit

# 3. Context fills up? Resume with AI-curated history (supervisor config carries over)
forge session resume executor --fresh --strategy ai-curated
# ... keeps working with fresh context

# 4. Fork the planner into the executor's worktree to review
forge session fork planner --into ../executor-worktree  # Path to executor's worktree
# ... reviews with full plan context, suggests fixes

# 5. Push and create a PR for human review
git push origin feature-branch
```

Each step uses the best model for the job. The `--supervise` flag wires the planner as a semantic supervisor -- every
code change is checked against the approved plan. Sessions track artifacts and transcripts automatically, so context
flows across forks and resumes. See the [end-user guide](docs/end-user/) for the full tour, or run `/forge:walkthrough`
inside Claude Code for an interactive walkthrough.

## CLI Overview

| Command Group          | Purpose                                      |
| ---------------------- | -------------------------------------------- |
| `forge claude`         | Bare launch, settings preset management      |
| `forge session`        | Named sessions, worktrees, resume, fork      |
| `forge proxy`          | Model routing, templates, tier mappings      |
| `forge authentication` | Credential management (`credentials.yaml`)   |
| `forge guard`          | Policy enforcement, plan supervision         |
| `forge workflow`       | Workflow runners (panel, analyze, debate)    |
| `forge search`         | Transcript search across sessions            |
| `forge config`         | Runtime preferences (`~/.forge/config.yaml`) |
| `forge extension`      | Enable/sync/disable extensions               |
| `forge info`           | System health and installation info          |

Run `forge <command> --help` for details on any command.

## Documentation

| Audience         | Location                           | Contents                                          |
| ---------------- | ---------------------------------- | ------------------------------------------------- |
| **Users**        | [docs/end-user/](docs/end-user/)   | Tour, guides for sessions, proxies, policies, ... |
| **Developers**   | [docs/developer/](docs/developer/) | Setup, coding standards, testing guidelines       |
| **Architecture** | [docs/design.md](docs/design.md)   | System narrative, data flow, invariants           |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and PR guidelines.

## Uninstall

```bash
forge extension disable
pip uninstall tr-claude-forge
```

## License

Apache 2.0 -- see [LICENSE](LICENSE).
