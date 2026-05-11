# Change Log

## 2026-05-11

### Follow-up: Make OpenRouter the OSS default for QA and workflows

**Goal**: Remove remote/shared LiteLLM as the default path for OSS QA and workflow setup while preserving it for the
remaining internal maintainer.

**Key changes**:

- Added an explicit QA provider profile split: default `openrouter`, opt-in `remote-litellm`.
- Moved workflow GPT/Gemini defaults to `openrouter-openai` and `openrouter-gemini`.
- Replaced QA checklist runtime proxy references with role-based `FORGE_QA_*` variables.
- Added `remote_litellm` as the infrastructure-specific test marker and dropped the unused `paid` marker.

**Verification**:
`uv run pytest tests/src/review/test_models.py tests/src/review/test_engine.py tests/src/cli/test_workflow.py tests/src/review/test_skill_content.py tests/src/skills/test_walkthrough_state.py -q`;
`uv run pytest tests/integration/cli/test_workflow_integration.py -q`;
`uv run pytest tests/integration/proxy/test_proxy_remote_litellm_e2e.py --collect-only -q`; `uv run ruff check` on
touched Python files; `bash -n src/skills/qa/scripts/start-container.sh`; `git diff --check`.

## 2026-05-08

### c74ccdc: Add verb cost attribution, CLI, and status line cost display

**Goal**: Track Forge-initiated subprocess spend by user-visible verb and expose costs in CLI/status surfaces.

**Key changes**:

- Added verb-level cost snapshotting.
- Added `forge proxy costs`.
- Added status line total-cost display backed by `~/.forge/costs/verbs/`.

**Verification**: Covered by unit tests for verb cost tracking and CLI cost display.

### 2d892c7: Fix proxy-scoped verb costs and failure-path tracking

**Goal**: Prevent cross-proxy cost attribution and avoid losing verb records when subprocess work raises.

**Key changes**:

- Scoped before/after snapshots to the resolved proxy.
- Wrapped tracked verb execution in `try/finally`.

**Verification**: Covered by proxy-scoped verb cost tests and failure-path tracking tests.

### 7412bcb: Add spend caps with JSONL bootstrap and pre-request enforcement

**Goal**: Add persistent proxy request costs and enforce user-configured spend caps across proxy restarts.

**Key changes**:

- Added request JSONL logs under `~/.forge/costs/requests/`.
- Added `CostTracker` bootstrap from current and previous month logs.
- Added `post` and `strict` cap modes with pre-request HTTP enforcement.

**Verification**: Covered by proxy cost tracker tests and proxy metrics/cap integration tests.

### ad3be1a: Fix spend cap config load, coercion, rollover, strict, and warn paths

**Goal**: Stabilize spend caps in the real proxy load/enforcement paths.

**Key changes**:

- Fixed proxy.yaml costs flowing into runtime `ProxyConfig`.
- Fixed numeric cap coercion.
- Fixed stale monthly rollover rejection.
- Fixed strict-mode projected-cost checks.
- Fixed warn-mode response headers.

**Verification**: Covered by unit tests plus `tests/regression/test_bug_caps_spend_cap_regressions.py`.

### 263b8e6: Add subprocess proxy and cost visibility E2E coverage

**Goal**: Support direct main sessions with proxied subprocesses and verify cost visibility in realistic flows.

**Key changes**:

- Added `intent.subprocess_proxy`.
- Added `FORGE_SUBPROCESS_PROXY` environment plumbing.
- Added subprocess proxy inheritance through resume, fork, and relaunch.
- Added guarding for unavailable subprocess proxies.
- Added E2E coverage for cost visibility.

**Verification**:
`uv run pytest tests/src/core/reactive/test_subprocess_proxy.py tests/src/session/test_subprocess_proxy_inheritance.py tests/src/cli/test_session_subprocess_proxy.py tests/src/proxy/test_metrics_integration.py -q`

### Follow-up: Document and pin cost/subprocess proxy contracts

**Goal**: Close documentation and regression-test gaps identified after review.

**Key changes**:

- Documented cost tracking, spend caps, `spend_cap_exceeded`, subprocess proxy launch semantics, and `SessionIntent`.
- Added bug-pinning spend cap coverage in `tests/regression/`.
- Normalized `ProxyInstanceConfig.costs` to `CostConfig`.

**Verification**: `uv run pytest tests/regression/test_bug_caps_spend_cap_regressions.py -q`;
`uv run pytest tests/src/config/test_schema.py tests/src/config/test_loader.py tests/src/core/reactive/test_env.py tests/src/core/reactive/test_subprocess_proxy.py -q`
