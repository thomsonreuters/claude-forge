<!-- prereq: 0.3 -->

## 7. Cost Tracking & Spend Caps

### 7.1 Cost CLI (Empty State)

<!-- auto -->

```bash
# Use a guaranteed-empty proxy_id for empty-state tests.
# Other sections (e.g., section 4 guided sessions) may have created real cost logs,
# so we cannot assume global cost logs are empty.
forge proxy costs qa-no-such-proxy 2>&1
echo "---"
forge proxy costs qa-no-such-proxy --period all 2>&1
echo "---"
forge proxy costs qa-no-such-proxy --json
```

- [ ] `forge proxy costs qa-no-such-proxy` shows `No cost data for today (qa-no-such-proxy).`
- [ ] `--period all` shows `No cost data for all (qa-no-such-proxy).`
- [ ] `--json` returns valid JSON with `total_cost_micros: 0` and `total_requests: 0`

### 7.2 Cost CLI (JSON Structure)

<!-- auto -->

```bash
# Verify JSON output schema using the empty-proxy filter (guaranteed empty)
forge proxy costs qa-no-such-proxy --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
fields = {'period','proxy_id','total_cost_micros','total_cost_usd','total_requests','interactive_cost_micros','by_verb','by_model','estimated'}
missing = fields - set(d.keys())
print(f'MISSING={missing}' if missing else 'ALL_FIELDS_PRESENT')
print(f'period={d[\"period\"]}')
print(f'estimated={d[\"estimated\"]}')
"
```

- [ ] JSON contains all required fields: `period`, `proxy_id`, `total_cost_micros`, `total_cost_usd`, `total_requests`, `interactive_cost_micros`, `by_verb`, `by_model`, `estimated`
- [ ] `period` is `today`
- [ ] `estimated` is `true`

### 7.3 Seed Fixture Request Logs

<!-- auto -->

```bash
# Seed QA-prefixed fixture request logs matching cost_logger.py record schema.
# Uses qa-fixture prefix and PID 99999 to avoid collision with real proxy logs.
mkdir -p ~/.forge/costs/requests
cat > ~/.forge/costs/requests/qa-fixture_99999.jsonl <<'EOF'
{"ts":"2026-05-01T00:00:00Z","proxy_id":"qa-fixture","model":"test/gemini-2.5-flash","tier":"haiku","input_tokens":200,"output_tokens":80,"cached_tokens":0,"cost_micros":300,"estimated":true,"pricing_source":"catalog","latency_ms":120.0,"failed":false,"request_id":"req-qa-001"}
{"ts":"2026-05-01T00:01:00Z","proxy_id":"qa-fixture","model":"test/gemini-2.5-pro","tier":"sonnet","input_tokens":500,"output_tokens":150,"cached_tokens":50,"cost_micros":1200,"estimated":true,"pricing_source":"catalog","latency_ms":350.0,"failed":false,"request_id":"req-qa-002"}
{"ts":"2026-05-01T00:02:00Z","proxy_id":"qa-fixture","model":"test/gemini-2.5-pro","tier":"opus","input_tokens":1000,"output_tokens":400,"cached_tokens":100,"cost_micros":3500,"estimated":true,"pricing_source":"catalog","latency_ms":800.0,"failed":false,"request_id":"req-qa-003"}
EOF

# Verify fixture is readable -- filter by qa-fixture to isolate from real proxy logs
forge proxy costs qa-fixture --period all --json
```

- [ ] Fixture file created at `~/.forge/costs/requests/qa-fixture_99999.jsonl`
- [ ] `forge proxy costs qa-fixture --period all --json` shows `total_cost_micros` of 5000 (300 + 1200 + 3500)
- [ ] `total_requests` is 3
- [ ] `by_model` contains both `test/gemini-2.5-flash` and `test/gemini-2.5-pro`

### 7.4 Seed Fixture Verb Logs

<!-- auto -->

```bash
# Seed QA-prefixed fixture verb logs matching cost_tracking.py verb record schema.
mkdir -p ~/.forge/costs/verbs
cat > ~/.forge/costs/verbs/qa-fixture_99999.jsonl <<'EOF'
{"ts":"2026-05-01T00:05:00Z","verb":"qa-fixture-panel","total_cost_micros":1500,"estimated":true,"input_tokens":700,"output_tokens":230,"cached_tokens":50,"request_count":2,"duration_ms":1200.0,"per_proxy":[{"base_url":"http://localhost:8084","cost_micros":1500,"input_tokens":700,"output_tokens":230,"cached_tokens":50,"request_count":2}]}
EOF

# Verify verb attribution appears. Do not proxy-filter this check: verb logs are scoped
# by resolved proxy base_url, while qa-fixture is only a request-log proxy_id fixture.
forge proxy costs --period all 2>&1
```

- [ ] Fixture file created at `~/.forge/costs/verbs/qa-fixture_99999.jsonl`
- [ ] `forge proxy costs --period all` shows `qa-fixture-panel` verb in output
- [ ] Verb cost attributed to `qa-fixture-panel` (1500 micros)

### 7.5 Cost CLI Breakdowns

<!-- auto -->

```bash
# By-model breakdown -- filter to qa-fixture to isolate from real proxy logs
forge proxy costs qa-fixture --by-model --period all 2>&1

echo "---"

# JSON with proxy_id filter
forge proxy costs qa-fixture --period all --json
```

- [ ] `--by-model` table shows model names with cost and token columns
- [ ] JSON output has `proxy_id: "qa-fixture"`
- [ ] Filtered `total_requests` is 3 (only qa-fixture records)
- [ ] Rich table output captured via `2>&1` (console uses stderr)

### 7.6 Malformed Log Resilience

<!-- auto -->

```bash
# Append non-JSON garbage lines to the fixture request log
echo 'THIS_IS_NOT_JSON' >> ~/.forge/costs/requests/qa-fixture_99999.jsonl
echo '<<<CORRUPT>>>' >> ~/.forge/costs/requests/qa-fixture_99999.jsonl

# Cost CLI should skip malformed lines -- filter to qa-fixture for deterministic count
forge proxy costs qa-fixture --period all --json 2>&1
echo "EXIT=$?"
```

- [ ] Command succeeds (exit 0) despite malformed lines
- [ ] Valid records still returned (`total_requests` is 3, not 5)
- [ ] No traceback or error on stderr

### 7.7 Spend Cap Configuration via CLI

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# Set spend caps on the test proxy from section 4
forge proxy set litellm-gemini costs.caps.per_day=20.00
forge proxy set litellm-gemini costs.caps.per_month=100.00
forge proxy set litellm-gemini costs.cap_mode=post
forge proxy set litellm-gemini costs.on_cap_hit=reject

# Validate config is healthy after cap changes
forge proxy validate litellm-gemini

# Show raw YAML to verify caps appear
forge proxy show litellm-gemini --raw
```

- [ ] `costs.caps.per_day` appears in raw YAML as `20.0` (float, not string `"20.00"`)
- [ ] `costs.caps.per_month` appears as `100.0`
- [ ] `cap_mode` is `post`
- [ ] `on_cap_hit` is `reject`
- [ ] Config validates successfully after setting caps
- [ ] Raw YAML shows complete `costs:` section with `caps`, `cap_mode`, `on_cap_hit`

### 7.8 Spend Cap Config Validation (Invalid Values)

<!-- prereq: 4.2 -->

<!-- auto -->

```bash
# Invalid cap_mode -- should be rejected
forge proxy set litellm-gemini costs.cap_mode=invalid 2>&1; echo "EXIT=$?"

# Invalid on_cap_hit -- should be rejected
forge proxy set litellm-gemini costs.on_cap_hit=invalid 2>&1; echo "EXIT=$?"
```

- [ ] Invalid `cap_mode` rejected with validation error (exit non-zero)
- [ ] Invalid `on_cap_hit` rejected with validation error (exit non-zero)
- [ ] Error messages reference valid values (`post`/`strict` and `reject`/`warn`)

### 7.9 Spend Cap Enforcement (Reject Mode)

<!-- prereq: 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

Seed a current-timestamp cost log so the proxy's cost tracker bootstraps above the cap, then make a request to verify
rejection. This avoids depending on a real request landing above a tiny cap (which is non-deterministic for cheap
models).

```
# Set a low daily cap
forge proxy set litellm-gemini costs.caps.per_day=0.01
forge proxy set litellm-gemini costs.on_cap_hit=reject
forge proxy set litellm-gemini costs.cap_mode=post

# Seed a cost log with a current timestamp so the tracker bootstraps above the cap.
# The tracker reads YYYY-MM_*.jsonl files on startup (bootstrap_from_logs).
mkdir -p ~/.forge/costs/requests
MONTH=$(date -u +%Y-%m)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "{\"ts\":\"$TS\",\"proxy_id\":\"litellm-gemini\",\"model\":\"seed\",\"tier\":\"sonnet\",\"input_tokens\":0,\"output_tokens\":0,\"cached_tokens\":0,\"cost_micros\":50000,\"estimated\":true,\"pricing_source\":\"catalog\",\"latency_ms\":0,\"failed\":false,\"request_id\":\"req-qa-cap-seed\"}" \
  > ~/.forge/costs/requests/${MONTH}_qa-cap-seed.jsonl

# Restart proxy so it bootstraps from the seeded log
forge proxy stop litellm-gemini 2>/dev/null || true
forge proxy start litellm-gemini

# Make a request -- should be rejected immediately
forge claude start --proxy litellm-gemini
# Say "hello" -- expect rejection or error about spend cap, then exit (/exit)

# Clean up seeded log
rm -f ~/.forge/costs/requests/${MONTH}_qa-cap-seed.jsonl
```

- [ ] After proxy restart, the seeded cost triggers the daily cap
- [ ] Proxy returns HTTP 429 or Claude reports a `spend_cap_exceeded` error
- [ ] Error message includes current spend and limit amounts
- [ ] Error message suggests `forge proxy set <id> costs.caps.per_day=<amount>` to adjust

### 7.10 Spend Cap Enforcement (Warn Mode)

<!-- prereq: 4.2 -->

<!-- requires: api_key -->

<!-- human:guided -->

Switch to warn mode and verify requests succeed with a warning header instead of being blocked. Uses the same seeded
cost log approach for deterministic cap triggering.

```
# Switch to warn mode
forge proxy set litellm-gemini costs.on_cap_hit=warn

# Re-seed the cost log (cleanup from 7.9 removed it)
MONTH=$(date -u +%Y-%m)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "{\"ts\":\"$TS\",\"proxy_id\":\"litellm-gemini\",\"model\":\"seed\",\"tier\":\"sonnet\",\"input_tokens\":0,\"output_tokens\":0,\"cached_tokens\":0,\"cost_micros\":50000,\"estimated\":true,\"pricing_source\":\"catalog\",\"latency_ms\":0,\"failed\":false,\"request_id\":\"req-qa-cap-warn\"}" \
  > ~/.forge/costs/requests/${MONTH}_qa-cap-seed.jsonl

# Restart proxy so it bootstraps with the seeded cost
forge proxy stop litellm-gemini 2>/dev/null || true
forge proxy start litellm-gemini

# Make a request through the proxy
forge claude start --proxy litellm-gemini
# Say "hello", then exit (/exit)

# Clean up seeded log
rm -f ~/.forge/costs/requests/${MONTH}_qa-cap-seed.jsonl
```

- [ ] Request succeeds (not blocked) in warn mode
- [ ] `X-Spend-Warning` header present in response (visible in proxy debug output or Claude error display)

### 7.11 Cleanup Fixture Cost Logs

<!-- auto -->

```bash
# Remove only QA fixture files -- do not touch real proxy cost logs
rm -f ~/.forge/costs/requests/qa-fixture_*.jsonl
rm -f ~/.forge/costs/verbs/qa-fixture_*.jsonl

# Remove cap-seed logs from 7.9/7.10 (in case cleanup within those steps failed)
rm -f ~/.forge/costs/requests/*_qa-cap-seed.jsonl

# Verify cleanup: no QA-owned cost fixture files remain
ls ~/.forge/costs/requests/qa-fixture_*.jsonl 2>&1 || echo "QA_REQUEST_LOGS_CLEAN"
ls ~/.forge/costs/verbs/qa-fixture_*.jsonl 2>&1 || echo "QA_VERB_LOGS_CLEAN"
ls ~/.forge/costs/requests/*_qa-cap-seed.jsonl 2>&1 || echo "QA_CAP_SEED_LOGS_CLEAN"

# Reset spend caps on test proxy
forge proxy set litellm-gemini costs.caps.per_day=none 2>/dev/null || true
forge proxy set litellm-gemini costs.caps.per_month=none 2>/dev/null || true
forge proxy set litellm-gemini costs.on_cap_hit=reject 2>/dev/null || true
forge proxy set litellm-gemini costs.cap_mode=post 2>/dev/null || true
```

- [ ] QA fixture request logs removed (no `qa-fixture_*.jsonl` in `requests/`)
- [ ] QA fixture verb logs removed (no `qa-fixture_*.jsonl` in `verbs/`)
- [ ] QA cap seed logs removed (no `*_qa-cap-seed.jsonl` in `requests/`)
- [ ] Spend caps reset on test proxy

---
