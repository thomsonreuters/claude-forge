## 20. Cleanup

### 20.1 Cleanup Test Artifacts

<!-- auto -->

<!-- destructive -->

```bash
# Clean up test sessions and artifacts, but preserve the QA state mount
rm -rf .forge/sessions/ .forge/artifacts/ .forge/prev_sessions/ .forge/search-index/

# Remove shell profile backup (optional)
rm -f ~/.zshrc.forge-uninstall-backup

# Remove QA cost fixture logs (safe: only QA-owned fixture names)
rm -f ~/.forge/costs/requests/qa-fixture_*.jsonl
rm -f ~/.forge/costs/verbs/qa-fixture_*.jsonl
rm -f ~/.forge/costs/requests/*_qa-cap-seed.jsonl

# Remove test repo entirely (optional)
# cd .. && rm -rf manual-testing/walkthrough/test-repo
```

- [ ] `.forge/sessions/` removed (or did not exist)
- [ ] `.forge/qa/` preserved (QA state mount -- do NOT delete)
- [ ] Shell profile backup removed (if existed)
- [ ] QA cost fixture logs removed from `~/.forge/costs/requests/` (no `qa-fixture_*.jsonl`)
- [ ] QA cost fixture logs removed from `~/.forge/costs/verbs/` (no `qa-fixture_*.jsonl`)
- [ ] QA cap seed logs removed from `~/.forge/costs/requests/` (no `*_qa-cap-seed.jsonl`)

---
