<!-- prereq: 0.3 -->

## 17. System Info

### 17.1 `forge info`

<!-- auto -->

```bash
forge info
```

- [ ] Shows Forge version
- [ ] Shows installation status
- [ ] Shows proxy status
- [ ] Shows active session (if any)

### 17.2 Debug Logging and `forge logs`

<!-- human:confirm -->

Run a Forge command with debug logging enabled, then use `forge logs` to inspect and clean up log files.

```bash
# Run a command with debug logging
FORGE_DEBUG=1 forge info

# Show log locations and file counts
forge logs

# Verify logs were written
forge logs
# Expected: shows log directory path and file count > 0

# Clean up logs
forge logs --clean

# Verify cleanup
forge logs
# Expected: reports 0 log files when no Forge processes are running.
# If QA proxies are still running, active proxy logs may be retained.
```

- [ ] `FORGE_DEBUG=1` enables debug logging (no crash, no error)
- [ ] `forge logs` shows log directory location and file counts
- [ ] Log files were actually written (count > 0 after debug run)
- [ ] `forge logs --clean` removes stale log files
- [ ] After cleanup, `forge logs` reports 0 files, or only logs for currently running Forge proxy processes

---
