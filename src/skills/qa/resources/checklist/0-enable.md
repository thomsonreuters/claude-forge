## 0. Install Forge (New User Flow)

This simulates what a new user does to install Forge from scratch.

### 0.1 Pre-requisites Check

<!-- auto -->

```bash
# Check you have the required tools
python3 --version   # Need 3.11+
uv --version        # Need uv package manager
git --version       # Need git
```

- [ ] Python 3.11+ installed
- [ ] uv installed
- [ ] git installed

### 0.2 Verify Clean State

<!-- auto -->

```bash
# Ensure no previous Forge installation
ls -la ~/.forge/           # Should not exist
which forge                # Should not be on PATH (or points to dev venv)

# Check Claude settings have no Forge hooks
cat ~/.claude/settings.json | jq '.hooks' 2>/dev/null || true
cat ~/.claude/settings.local.json | jq '.hooks' 2>/dev/null || true
```

- [ ] `~/.forge/` does not exist
- [ ] No Forge hooks in `~/.claude/settings.json`
- [ ] No Forge hooks in `~/.claude/settings.local.json`

### 0.3 Install via setup.sh

<!-- auto -->

```bash
# Install Forge using the local copy (already in container at /forge/)
cd /forge && /forge/scripts/setup.sh --local
```

- [ ] Prerequisites check passes
- [ ] Repository linked at `~/.forge/repo/` (symlink to `/forge`)
- [ ] `setup.sh --local` completes successfully
- [ ] `forge` binary available (at `~/.local/bin/forge` for `--local` mode)
- [ ] Success message displayed

> Note: `setup.sh` installs Forge but does not automatically install Claude extensions. After install, run:
>
> - `forge extension enable --scope user` (installs commands/agents/skills/hooks)

### 0.4 Verify Installation

<!-- auto -->

```bash
# Source your profile (or restart terminal)
source ~/.zshrc  # or ~/.bash_profile

# Verify forge is on PATH
which forge
forge --version

# Check installation artifacts
ls -la ~/.forge/
ls -la ~/.forge/repo/
```

- [ ] `forge` command available on PATH
- [ ] `~/.forge/` directory created
- [ ] `~/.forge/repo/` contains repo (symlink or clone)

---
