<!-- prereq: 0.3, 2.4, 18 -->

## 19. Complete Uninstallation (setup.sh --uninstall)

This tests the curl-installable uninstall that removes EVERYTHING.

### 19.1 Pre-Uninstall State Verification

<!-- auto -->

<!-- destructive -->

```bash
# Verify we have both installations
cat ~/.forge/installed.json | jq '.installations | keys'
# Should show: ["user", "local:$FORGE_TEST_REPO"]

# Verify artifacts exist
ls ~/.forge/             # Should exist
ls ~/.forge/bin/forge    # Should exist
ls ~/.claude/commands/   # Should have Forge commands
ls .claude/commands/     # Should have Forge commands (local)
```

- [ ] Both user and local installations tracked
- [ ] `~/.forge/` exists
- [ ] User scope has Forge files
- [ ] Local scope has Forge files

### 19.2 Run Complete Uninstall

<!-- auto -->

<!-- destructive -->

```bash
# Run the uninstall script using the local copy
~/.forge/repo/scripts/setup.sh --uninstall
```

The script attempts to remove extensions via Forge CLI:

- `forge extension disable --all --force` (remove tracked extensions)

Then it removes `~/.forge/` and other artifacts.

- [ ] Script runs without errors
- [ ] ALL scopes uninstalled (via `forge extension disable --all --force` or equivalent)
- [ ] Shows "Found N Forge installation(s)" summary (if forge available)
- [ ] `~/.forge/` removed
- [ ] `~/.forge/sessions/` removed (Forge session data)
- [ ] Docker images removed (claude-forge-\*)
- [ ] Shell profile cleaned (block markers removed)

### 19.3 Verify Complete Removal

<!-- auto -->

<!-- destructive -->

```bash
# Verify ~/.forge/ is gone
ls ~/.forge/ 2>/dev/null || echo "~/.forge/ removed"

# Verify forge not on PATH (need new terminal or source profile)
# source ~/.zshrc  # or restart terminal
# which forge      # Should fail or show nothing

# Verify no Forge hooks in global settings
cat ~/.claude/settings.json | jq '.hooks'
# Should be null or empty of Forge entries

# Verify user commands removed
ls ~/.claude/commands/ 2>/dev/null | grep -v "^$" || echo "User commands removed"
ls ~/.claude/agents/ 2>/dev/null | grep -v "^$" || echo "User agents removed"
ls ~/.claude/skills/ 2>/dev/null | grep -v "^$" || echo "User skills removed"
```

- [ ] `~/.forge/` directory removed
- [ ] Forge hooks removed from `~/.claude/settings.json`
- [ ] User commands/agents/skills removed

### 19.4 Verify Local Project Settings Preserved

<!-- auto -->

<!-- destructive -->

```bash
cd $FORGE_TEST_REPO

# CRITICAL: Local pre-existing settings should survive
cat .claude/settings.local.json | jq '.'

# Original permissions should still be there
cat .claude/settings.local.json | jq '.permissions.allow'
# Should show: ["Bash(npm test)", "Bash(uv run pytest*)"]

# Custom env var should still be there
cat .claude/settings.local.json | jq '.env.MY_CUSTOM_VAR'
# Should show: "should-survive-forge"
```

- [ ] `.claude/settings.local.json` still exists
- [ ] Original permissions preserved
- [ ] `env.MY_CUSTOM_VAR` preserved
- [ ] Forge-added entries (hooks, Write/Edit permissions, env) removed; user-approved permissions (e.g.,
  `Bash(forge workflow:*)`) may remain

### 19.5 Verify Shell Profile Cleaned

<!-- auto -->

<!-- destructive -->

```bash
# Check that block markers were removed from any shell profile Forge may touch.
PROFILES=("$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.config/fish/config.fish")
FOUND_PROFILE=0
FOUND_BACKUP=0

for profile in "${PROFILES[@]}"; do
  if [ -f "$profile" ]; then
    FOUND_PROFILE=1
    echo "Checking $profile"
    grep -n ">>> claude-forge >>>" "$profile" && exit 1 || true
    grep -n "$HOME/.forge/bin" "$profile" && exit 1 || true
  fi

  if [ -f "$profile.forge-uninstall-backup" ]; then
    FOUND_BACKUP=1
    echo "Backup exists: $profile.forge-uninstall-backup"
  fi
done

echo "profiles_found=$FOUND_PROFILE backups_found=$FOUND_BACKUP"
if [ "$FOUND_PROFILE" -eq 0 ]; then
  echo "No shell profile exists in this container; profile cleanup is N/A."
fi
```

- [ ] No `>>> claude-forge >>>` block remains in any existing shell profile
- [ ] No `.forge/bin` PATH entry remains in any existing shell profile
- [ ] Backup file exists for any profile that was modified; if no shell profile exists, profile cleanup is N/A

---
