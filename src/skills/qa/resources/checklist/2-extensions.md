<!-- prereq: 0, 1 -->

## 2. Claude Code Extensions (`forge extension enable`)

### 2.1 Basic Installation (User Scope)

<!-- auto -->

```bash
# Install forge extensions (default: standard profile, user scope)
cd $FORGE_TEST_REPO
forge extension enable --scope user --symlink

# Optional: preview changes instead of applying
forge extension enable --scope user --dry-run

# Verify installation
ls -la $CLAUDE_HOME/skills/
cat $CLAUDE_HOME/settings.json | jq '.hooks'
cat $FORGE_HOME/installed.json | jq '.installations.user.modules_enabled'

# Optional: confirm status line + permissions were merged (user scope)
cat $CLAUDE_HOME/settings.json | jq '.statusLine'
cat $CLAUDE_HOME/settings.json | jq '.permissions'
```

- [ ] `modules_enabled` in `installed.json` lists `commands` and `agents` (directories created only if source has
  installable files)
- [ ] Skills installed to `$CLAUDE_HOME/skills/` (standard profile)
- [ ] Hooks configured in `$CLAUDE_HOME/settings.json` (or in `$CLAUDE_HOME/settings.local.json` if you used hooks-only
  install)
- [ ] `$FORGE_HOME/installed.json` tracking file created

### 2.2 Verify Installed Content

<!-- auto -->

```bash
# Check what was installed
cat $FORGE_HOME/installed.json | jq '.'

# Verify user-scope status line setting
cat $CLAUDE_HOME/settings.json | jq '.statusLine'

# Verify user-scope permissions
cat $CLAUDE_HOME/settings.json | jq '.permissions'

# Verify skills are installed
ls $CLAUDE_HOME/skills/
```

- [ ] `installed.json` lists all installed files
- [ ] `statusLine` points to `forge status-line`
- [ ] Permissions include Forge-required entries
- [ ] Skills directory contains skill folders (analyze, debate, panel, review, review-docs, etc.)

### 2.3 Verify Pre-Existing Settings Preserved

<!-- auto -->

```bash
# Check that user's original settings survived installation
cat .claude/settings.local.json | jq '.'

# Verify original permissions still present (merged, not replaced)
cat .claude/settings.local.json | jq '.permissions.allow'
# Should include BOTH:
# - Original: "Bash(npm test)", "Bash(uv run pytest*)"
# - Forge-added permissions (if any added to local scope)

# Verify custom env var preserved
cat .claude/settings.local.json | jq '.env.MY_CUSTOM_VAR'
# Should show: "should-survive-forge"
```

- [ ] Original `permissions.allow` entries preserved
- [ ] `env.MY_CUSTOM_VAR` still present
- [ ] Forge merged settings, didn't replace

### 2.4 Install Local Scope

<!-- auto -->

```bash
# Install Forge extensions to LOCAL scope (this project only)
cd $FORGE_TEST_REPO
forge extension enable --scope local

# Verify local installation
cat .claude/settings.local.json | jq '.hooks'
LOCAL_KEY="local:$(cd "$FORGE_TEST_REPO" && pwd -P)"
cat $FORGE_HOME/installed.json | jq --arg key "$LOCAL_KEY" '.installations[$key].modules_enabled'
```

- [ ] `modules_enabled` for local installation lists `commands` and `agents` (directories created only if source has
  installable files)
- [ ] Hooks configured in `.claude/settings.local.json`

### 2.5 Verify Both Installations Tracked

<!-- auto -->

```bash
# Check tracking file shows BOTH installations
LOCAL_KEY="local:$(cd "$FORGE_TEST_REPO" && pwd -P)"
cat $FORGE_HOME/installed.json | jq '.installations | keys'
printf 'Expected local key: %s\n' "$LOCAL_KEY"

# Show user installation
cat $FORGE_HOME/installed.json | jq '.installations.user.scope'
# Should show: "user"

# Show local installation (note the key format with path)
cat $FORGE_HOME/installed.json | jq --arg key "$LOCAL_KEY" '.installations[$key].scope'
# Should show: "local"

# Verify project_path is tracked
cat $FORGE_HOME/installed.json | jq --arg key "$LOCAL_KEY" '.installations[$key].project_path'
# Should show the resolved path part of LOCAL_KEY
```

- [ ] Tracking shows "user" key
- [ ] Tracking shows "local:/path/to/project" key
- [ ] Both installations tracked separately
- [ ] project_path field populated for local installation

### 2.6 Test Double-Install Prevention

<!-- auto -->

```bash
# Try to install local again to same project
forge extension enable --scope local

# Should either:
# - Say "already installed" and skip
# - Or update existing installation (idempotent)

# Verify only ONE local entry in tracking
cat $FORGE_HOME/installed.json | jq '.installations | keys | length'
# Should show 2 (user + 1 local), not 3
```

- [ ] Re-running `forge extension enable --scope local` is idempotent
- [ ] No duplicate entries in tracking

### 2.7 Check Install Status (Nearest Scope)

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

# Status for the nearest installation (auto-detects local/project/user)
forge extension status
```

- [ ] `forge extension status` succeeds
- [ ] Shows detected scope + profile/modules summary

### 2.8 Check Install Status (All Scopes)

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

# Show user + project + local scopes
forge extension status --all
```

- [ ] Shows all three scopes (user/project/local)
- [ ] Missing scopes are shown as "Not installed" (does not error)

### 2.9 Update Installation (Idempotent)

<!-- auto -->

```bash
cd $FORGE_TEST_REPO

# Update the nearest installation (auto-detects)
forge extension sync
```

- [ ] Update completes (or reports already up to date)

---
