# Forge Search — Transcript Search Guide

Search past session transcripts by keyword. Forge indexes transcripts automatically via the Stop hook and stores a
per-project BM25 index for fast lookup.

- Canonical architecture: [`docs/design.md`](../design.md)
- Sessions (unit of work): [`sessions.md`](sessions.md)
- Hooks (lifecycle events): [`hooks.md`](hooks.md)

---

## Quick start

```bash
# Search for a keyword or phrase
forge search -q "timeout config"

# Limit results
forge search -q "proxy routing" -n 5

# Search across all indexed projects (not just current)
forge search -q "auth refactor" --scope all
```

Results are JSON output with `session_name`, `session_id`, `score`, `snippet`, `transcript_path`, and `metadata`
(including timestamps).

---

## How indexing works

Forge indexes transcripts **automatically** via the Stop hook pipeline:

```
Session stops
  → Stop hook copies transcript to <forge_root>/.forge/artifacts/
  → Stop hook enqueues "index" work marker
  → (session ends)

Next forge command (any)
  → Work queue processes pending markers
  → Transcript is extracted, tokenized, and added to the BM25 index
```

Indexing is **incremental** — only new or modified transcripts are processed. The index is per-project, stored at
`<forge_root>/.forge/search-index/`.

---

## CLI reference

### Search

```bash
forge search -q <query> [-n <limit>] [--scope project|all]
```

- `-q` / `--query` — search terms (required)
- `-n` / `--limit` — max results to return
- `--scope` — `project` (default, current project only) or `all` (all indexed projects)

### Index management

```bash
# Show index statistics (document count, last indexed, index health)
forge search status

# Full rebuild from all transcript artifacts
forge search rebuild-index

# Remove orphaned entries (transcripts that were deleted)
forge search clean
```

---

## What gets indexed

| Content                              | Indexing               |
| ------------------------------------ | ---------------------- |
| User messages                        | Fully indexed          |
| Assistant messages                   | Fully indexed          |
| Tool inputs (commands, paths)        | Truncated to 100 chars |
| Tool results (file contents, output) | Truncated to 500 chars |

Truncation yields ~20-50x compression over raw transcripts while keeping terms searchable. Full transcripts remain in
`<forge_root>/.forge/artifacts/` for direct inspection.

---

## Index location

The search index is **per-project**, stored at:

```
<project_root>/.forge/search-index/
├── documents.json      # Document metadata (session names, timestamps)
├── bm25_index.json     # Precomputed BM25 term frequencies + tokenizer version
└── content.json        # Extracted text snippets (loaded only for top-K results)
```

The three-file split keeps queries fast (~300KB loaded vs ~5MB for full content).

---

## Troubleshooting

### "No results" for a term you know exists

- Check if the index is built: `forge search status`
- If not built or stale: `forge search rebuild-index`
- Search uses BM25 keyword matching — exact terms work best. Try shorter or more specific queries.

### "Index not built"

The index builds automatically when sessions end (via Stop hook). If you haven't ended any sessions since installing
Forge, build manually:

```bash
forge search rebuild-index
```

### "Orphaned documents"

If you deleted transcript files but the index still references them:

```bash
forge search clean
```

### "Index corrupted"

If search returns errors about store mismatches:

```bash
forge search rebuild-index
```

A full rebuild reconstructs all three store files from the source transcripts.

---

## Files to inspect (debugging)

| File                                               | Purpose                                    |
| -------------------------------------------------- | ------------------------------------------ |
| `<forge_root>/.forge/search-index/documents.json`  | Indexed document metadata                  |
| `<forge_root>/.forge/search-index/bm25_index.json` | BM25 term index                            |
| `<forge_root>/.forge/search-index/content.json`    | Extracted text content                     |
| `<forge_root>/.forge/artifacts/*/transcripts/`     | Source transcripts (index input)           |
| `~/.forge/pending-work/`                           | Work queue markers (index markers pending) |
