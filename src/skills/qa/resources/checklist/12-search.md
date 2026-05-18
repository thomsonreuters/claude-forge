<!-- prereq: 0.3, 10.1 -->

## 12. Search (`forge search`)

### 12.1 Check Index Status

<!-- auto -->

```bash
forge search status
```

- [ ] Shows index statistics (document count, store health)
- [ ] Shows index location

### 12.2 Build/Rebuild Index

<!-- auto -->

```bash
# Full rebuild from all transcript artifacts
forge search rebuild-index
```

- [ ] Rebuilds index from `.forge/artifacts/`
- [ ] Reports number of documents indexed

### 12.3 Search Transcripts

<!-- auto -->

```bash
# Search for a keyword
forge search -q "hello world"

# Limit results
forge search -q "test" -n 3

# Search all projects
forge search -q "hello world" --scope all
```

- [ ] Returns JSON results with session_name, score, snippet
- [ ] `--scope all` searches indexed projects (including the current project)
- [ ] Results ranked by BM25 relevance

### 12.4 Clean Orphaned Entries

<!-- auto -->

```bash
forge search clean
```

- [ ] Removes entries for deleted transcripts
- [ ] Reports removed/pruned count or "No orphaned entries found."

---
