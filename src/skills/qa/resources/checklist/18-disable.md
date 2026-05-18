<!-- prereq: 0.3, 2.4 -->

## 18. Uninstallation (Incremental)

Test uninstalling individual scopes before the complete uninstall.

### 18.1 Uninstall Local Scope Only

<!-- auto -->

<!-- destructive -->

```bash
cd $FORGE_TEST_REPO

# Uninstall only the local scope
forge extension disable --scope local

# (Optional) Uninstall hooks-only path, if you used it
forge hook disable --local

# Verify local removal
ls .claude/commands/   # Should be empty or removed
cat .claude/settings.local.json | jq '.hooks'  # Should have no Forge hooks

# Verify user scope STILL installed
ls ~/.claude/commands/  # Should still have Forge commands
cat ~/.claude/settings.json | jq '.hooks'  # Should still have Forge hooks

# Check tracking
cat ~/.forge/installed.json | jq '.installations | keys'
# Should show only ["user"], not the local:... key
```

- [ ] Local commands removed
- [ ] Local hooks removed from settings.local.json
- [ ] User scope commands still present
- [ ] User scope hooks still present
- [ ] Tracking shows only "user" key

### 18.2 Verify Pre-Existing Settings Restored (Local)

<!-- auto -->

<!-- destructive -->

```bash
# CRITICAL: Check that user's original settings survived uninstall
cat .claude/settings.local.json | jq '.'

# Original permissions should still be there
cat .claude/settings.local.json | jq '.permissions.allow'
# Should show: ["Bash(npm test)", "Bash(uv run pytest*)"]

# Custom env var should still be there
cat .claude/settings.local.json | jq '.env.MY_CUSTOM_VAR'
# Should show: "should-survive-forge"
```

- [ ] Original `permissions.allow` entries preserved
- [ ] `env.MY_CUSTOM_VAR` still present
- [ ] Forge-added hooks removed; Forge-added permissions (Write, Edit) removed
- [ ] User-approved permissions (e.g., `Bash(forge workflow:*)`) may remain -- these are Claude Code auto-learned, not
  Forge-managed

### 18.3 Re-install Local for Complete Test

<!-- auto -->

<!-- destructive -->

```bash
# Re-install local scope so we can test complete uninstall
forge extension enable --scope local

# Verify both scopes installed again
cat ~/.forge/installed.json | jq '.installations | keys'
# Should show: ["user", "local:/Users/..."]
```

- [ ] Local scope re-installed
- [ ] Both installations tracked

---
