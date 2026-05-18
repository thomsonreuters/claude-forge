<!-- prereq: 0.3, 2.1, 5.1 -->

## 8. Status Line

### 8.1 Direct Invocation

<!-- human:confirm -->

This is a rendered status-line smoke test. It does not call Claude or an LLM; it feeds a synthetic Claude Code
`statusLine` JSON payload into `forge status-line` and asks you to review the terminal-facing output.

Expected visible shape, with colors/spaces rendered by the terminal:

```text
/workspace (main) | test-session-1
[Opus 4.6] -------- 6%/200K | 3m | +12/-3 | in:28.0K out:17.5K
```

The output may wrap to two physical terminal lines. A proxy template/tier prefix is expected only when
`ANTHROPIC_BASE_URL` points at a live or registered Forge proxy; if `test-session-1` was started without a proxy, no
proxy segment is expected here.

```bash
cd $FORGE_TEST_REPO

# Mirror Claude Code's statusLine JSON contract and the Forge launch env.
BASE_URL=$(jq -r '.intent.proxy.base_url // empty' .forge/sessions/test-session-1/forge.session.json)
mkdir -p .forge/walkthrough
cat > .forge/walkthrough/status-line-transcript.jsonl <<'EOF'
{"requestId":"req-001","message":{"role":"user","content":[{"type":"text","text":"Read the config file."}]}}
{"requestId":"req-001","message":{"role":"assistant","content":[{"type":"text","text":"I'll inspect it."},{"type":"tool_use","id":"tool-001","name":"Read","input":{"file_path":"/workspace/config.yaml"}}]}}
{"requestId":"req-001","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool-001","content":"timeout: 10"}]}}
{"requestId":"req-002","message":{"role":"user","content":[{"type":"text","text":"Update the timeout and run tests."}]}}
{"requestId":"req-002","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool-002","name":"Edit","input":{"file_path":"/workspace/config.yaml"}},{"type":"tool_use","id":"tool-003","name":"Bash","input":{"command":"uv run pytest"}}]}}
EOF
STATUS_INPUT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  --arg transcript "$FORGE_TEST_REPO/.forge/walkthrough/status-line-transcript.jsonl" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Opus 4.6"},
    transcript_path: $transcript,
    context_window: {
      context_window_size: 200000,
      used_percentage: 6,
      total_input_tokens: 28000,
      total_output_tokens: 17500,
      current_usage: {
        input_tokens: 8500,
        cache_creation_input_tokens: 2000,
        cache_read_input_tokens: 1500
      }
    },
    cost: {
      total_duration_ms: 185000,
      total_lines_added: 12,
      total_lines_removed: 3
    }
  }')

echo "$STATUS_INPUT" \
  | FORGE_SESSION=test-session-1 ANTHROPIC_BASE_URL="$BASE_URL" forge status-line
```

- [ ] Shows compact workspace path, git branch, and `test-session-1`
- [ ] Shows `[Opus 4.6]` plus context usage `6%/200K` with a visible progress bar
- [ ] Shows seeded metrics: `3m`, `+12/-3`, `in:28.0K`, and `out:17.5K`
- [ ] If `ANTHROPIC_BASE_URL` belongs to a created/running proxy, also shows proxy template/tier info
- [ ] Does not print raw JSON, a Python traceback, or `[Error: ...]`
- [ ] ANSI/color and non-breaking-space internals are checked in 8.2, not by this rendered review

### 8.2 Verify Display Elements

<!-- human:confirm -->

The status line uses a category-based layout with 5 categories: Where, Who, What, Metrics, State. This step
intentionally pipes the output through `cat -v`, so the output will look ugly on purpose:

- non-breaking spaces show up as `M-BM-`
- ANSI escapes show up as `^[[...`
- colorized line-change segments and dimmed `in:` / `out:` / `cache:` labels still show their raw escapes

Rendered output is covered in 8.1. This step is only checking that the raw escapes and hardened spacing are present.

```bash
cd $FORGE_TEST_REPO

BASE_URL=$(jq -r '.intent.proxy.base_url // empty' .forge/sessions/test-session-1/forge.session.json)
mkdir -p .forge/walkthrough
cat > .forge/walkthrough/status-line-transcript.jsonl <<'EOF'
{"requestId":"req-001","message":{"role":"user","content":[{"type":"text","text":"Read the config file."}]}}
{"requestId":"req-001","message":{"role":"assistant","content":[{"type":"text","text":"I'll inspect it."},{"type":"tool_use","id":"tool-001","name":"Read","input":{"file_path":"/workspace/config.yaml"}}]}}
{"requestId":"req-001","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool-001","content":"timeout: 10"}]}}
{"requestId":"req-002","message":{"role":"user","content":[{"type":"text","text":"Update the timeout and run tests."}]}}
{"requestId":"req-002","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool-002","name":"Edit","input":{"file_path":"/workspace/config.yaml"}},{"type":"tool_use","id":"tool-003","name":"Bash","input":{"command":"uv run pytest"}}]}}
EOF
STATUS_INPUT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  --arg transcript "$FORGE_TEST_REPO/.forge/walkthrough/status-line-transcript.jsonl" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Opus 4.6 (200k context)"},
    transcript_path: $transcript,
    context_window: {
      context_window_size: 200000,
      used_percentage: 6,
      total_input_tokens: 28000,
      total_output_tokens: 17500,
      current_usage: {
        input_tokens: 8500,
        cache_creation_input_tokens: 2000,
        cache_read_input_tokens: 1500
      }
    },
    cost: {
      total_duration_ms: 185000,
      total_lines_added: 12,
      total_lines_removed: 3
    }
  }')

# Pipe through cat -v to inspect raw ANSI escapes and NBSP rendering.
# Expected: ugly raw output, not a pretty status line.
echo "$STATUS_INPUT" \
  | FORGE_SESSION=test-session-1 ANTHROPIC_BASE_URL="$BASE_URL" forge status-line 2>&1 | cat -v
# Check for non-breaking spaces (M-BM-), ANSI codes (^[[...),
# and ASCII indicators/progress-bar text rather than rendered terminal styling.
```

- [ ] Shows ANSI-colored ASCII segments in raw `cat -v` form
- [ ] Shows model name (cleaned, without redundant context info)
- [ ] Uses non-breaking spaces (prevents VSCode trimming)
- [ ] ANSI reset prefix present

### 8.3 Breadcrumb Display (for resumed sessions)

<!-- human:confirm -->

```bash
cd $FORGE_TEST_REPO

# Create a disposable derived-looking session so this step does not depend on section 10.
forge session delete test-session-breadcrumb --force 2>/dev/null || true
forge session start test-session-breadcrumb --no-launch >/dev/null

cat .forge/sessions/test-session-breadcrumb/forge.session.json \
  | jq '.confirmed.derivation = {
      "parent_session": "test-session-1",
      "parent_transcript": ".forge/artifacts/test-session-1/transcript.jsonl",
      "inherited_proxy": null,
      "strategy": "minimal",
      "depth": 1,
      "resumed_at": "2026-03-16T00:00:00Z",
      "lineage": ["test-session-1"]
    }' > /tmp/test-session-breadcrumb.json && \
  mv /tmp/test-session-breadcrumb.json .forge/sessions/test-session-breadcrumb/forge.session.json

BASE_URL=$(jq -r '.intent.proxy.base_url // empty' .forge/sessions/test-session-breadcrumb/forge.session.json)
STATUS_INPUT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Opus 4.6"}
  }')

echo "$STATUS_INPUT" \
  | FORGE_SESSION=test-session-breadcrumb ANTHROPIC_BASE_URL="$BASE_URL" forge status-line 2>/dev/null

forge session delete test-session-breadcrumb --force >/dev/null
```

- [ ] Shows session lineage breadcrumb (for example `test-session-1 > test-session-breadcrumb`)

### 8.4 Cost Display (Direct vs Proxy Format)

<!-- human:confirm -->

The status line shows cost differently in direct vs proxy mode. Direct mode reads `cost.total_cost_usd` from the input
JSON; proxy mode reads live metrics from `GET /` on the proxy. This step verifies direct-mode rendering through the CLI
and proxy-mode rendering through a tiny local endpoint that returns the proxy metrics shape.

```bash
cd $FORGE_TEST_REPO

# Direct mode cost (no tilde prefix) -- feed total_cost_usd via cost data
STATUS_DIRECT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Opus 4.6"},
    cost: {
      total_cost_usd: 0.05,
      total_duration_ms: 60000
    }
  }')

echo "=== Direct mode ==="
echo "$STATUS_DIRECT" \
  | FORGE_SESSION=test-session-1 forge status-line 2>&1 | cat -v

echo "---"

echo "=== Proxy formatter ==="
PORT_FILE=$(mktemp)
python3 - "$PORT_FILE" <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_port
        body = {
            "is_proxy": True,
            "proxy": {
                "proxy_id": "qa-status-proxy",
                "template": "qa-status",
                "port": port,
                "base_url": f"http://127.0.0.1:{port}",
            },
            "runtime": {
                "active_tier": "sonnet",
                "active_context_window": 200000,
                "context_windows": {"sonnet": 200000},
                "tier_mappings": {"sonnet": "qa/model"},
            },
            "metrics": {"costs": {"total_usd": 0.05}},
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w") as f:
    f.write(str(server.server_port))
server.serve_forever()
PY
SERVER_PID=$!
for i in {1..20}; do test -s "$PORT_FILE" && break; sleep 0.1; done
PORT=$(cat "$PORT_FILE")
echo "$STATUS_DIRECT" \
  | ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" FORGE_SESSION=test-session-1 forge status-line 2>&1 | cat -v
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
rm -f "$PORT_FILE"
```

- [ ] Direct mode shows cost without tilde prefix (e.g., `$0.05`)
- [ ] Proxy-mode cost formatting uses tilde prefix for estimated proxy spend (e.g., `~$0.05`) and shows duration (`1m`)

### 8.5 Sub-Dollar Cost Formatting

<!-- human:confirm -->

```bash
cd $FORGE_TEST_REPO

# Direct mode sub-cent cost (< $0.01 triggers cents format)
STATUS_SUBCENT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Haiku 4.5"},
    cost: {
      total_cost_usd: 0.005,
      total_duration_ms: 30000
    }
  }')

echo "=== Direct sub-cent ==="
echo "$STATUS_SUBCENT" \
  | FORGE_SESSION=test-session-1 forge status-line 2>&1 | cat -v

echo "---"

# Direct mode above-cent cost (>= $0.01 uses dollar format)
STATUS_ABOVECENT=$(jq -nc \
  --arg cwd "$FORGE_TEST_REPO" \
  '{
    workspace: {current_dir: $cwd},
    model: {display_name: "Haiku 4.5"},
    cost: {
      total_cost_usd: 0.03,
      total_duration_ms: 30000
    }
  }')

echo "=== Direct above-cent ==="
echo "$STATUS_ABOVECENT" \
  | FORGE_SESSION=test-session-1 forge status-line 2>&1 | cat -v

echo "---"

echo "=== Proxy sub-cent formatter ==="
PORT_FILE=$(mktemp)
python3 - "$PORT_FILE" <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_port
        body = {
            "is_proxy": True,
            "proxy": {
                "proxy_id": "qa-status-proxy",
                "template": "qa-status",
                "port": port,
                "base_url": f"http://127.0.0.1:{port}",
            },
            "runtime": {
                "active_tier": "sonnet",
                "active_context_window": 200000,
                "context_windows": {"sonnet": 200000},
                "tier_mappings": {"sonnet": "qa/model"},
            },
            "metrics": {"costs": {"total_usd": 0.005}},
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w") as f:
    f.write(str(server.server_port))
server.serve_forever()
PY
SERVER_PID=$!
for i in {1..20}; do test -s "$PORT_FILE" && break; sleep 0.1; done
PORT=$(cat "$PORT_FILE")
echo "$STATUS_SUBCENT" \
  | ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" FORGE_SESSION=test-session-1 forge status-line 2>&1 | cat -v
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
rm -f "$PORT_FILE"
```

- [ ] Sub-cent cost (`< $0.01`) displays in cents format for direct and proxy modes (e.g., `0c` and `~0.5c`)
- [ ] Above-cent cost (`>= $0.01`) displays in dollar format (e.g., `$0.03`)

---
