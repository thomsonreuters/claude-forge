# PR #8 Review Map

Status: discussion artifact for reviewing PR #8, `feat/openrouter-cost-control`.

This PR is an integrated architectural slice. The dependency chain is:

```text
OpenRouter gateway
  -> model catalog/provider refs/templates
       -> system prompt addendums (non-Claude routing quality)
  -> capability-based subprocess routing
  -> proxy request cost logs and verb attribution
  -> per-proxy spend caps
  -> handoff/session primitives for runtime abstraction
```

Use this map for architectural and correctness review of the PR's implementation areas, plus a final documentation
cross-check. Review adversarially: look for what could be wrong, not just what's there.

## 1. OpenRouter Gateway

Design anchors:

- [docs/design_appendix.md](../design_appendix.md) A.2, proxy templates vs user-defined proxies
- [docs/design_appendix.md](../design_appendix.md) A.6, credentials and connection values
- [docs/proposals/runtime_abstraction.md](../proposals/runtime_abstraction.md), "OpenRouter" and "PR #8 Alignment"

Review invariants:

- OpenRouter is a native Forge provider path, not routed through LiteLLM by default.
- `OPENROUTER_API_KEY` is the credential for all `openrouter-*` templates.
- OpenRouter's open model space is handled gracefully; unknown model IDs should not crash proxy startup or `GET /`.
- OpenAI-compatible request/stream/tool-call helpers are shared without making OpenRouter depend on LiteLLM.
- OpenRouter requests keep system prompts and tool calls in the OpenAI-compatible path.

Entry points:

- [src/forge/core/llm/clients/openai_compat.py](../../src/forge/core/llm/clients/openai_compat.py)
- [src/forge/core/llm/clients/openrouter.py](../../src/forge/core/llm/clients/openrouter.py)
- [src/forge/core/llm/credentials.py](../../src/forge/core/llm/credentials.py)
- [src/forge/core/auth/capabilities.py](../../src/forge/core/auth/capabilities.py)
- [src/forge/core/auth/template_secrets.py](../../src/forge/core/auth/template_secrets.py)
- [src/forge/config/schema.py](../../src/forge/config/schema.py)
- [src/forge/config/loader.py](../../src/forge/config/loader.py)
- [src/forge/proxy/client_factory.py](../../src/forge/proxy/client_factory.py)
- [src/forge/proxy/converters.py](../../src/forge/proxy/converters.py)
- [src/forge/proxy/server.py](../../src/forge/proxy/server.py)
- [src/forge/config/defaults/templates/openrouter-\*.yaml](../../src/forge/config/defaults/templates/)

Tests:

- [tests/src/core/llm/test_openrouter.py](../../tests/src/core/llm/test_openrouter.py)
- [tests/src/core/llm/test_credentials.py](../../tests/src/core/llm/test_credentials.py)
- [tests/src/core/llm/test_detection.py](../../tests/src/core/llm/test_detection.py)
- [tests/src/core/auth/test_capabilities.py](../../tests/src/core/auth/test_capabilities.py)
- [tests/src/core/auth/test_template_secrets.py](../../tests/src/core/auth/test_template_secrets.py)
- [tests/src/config/test_loader.py](../../tests/src/config/test_loader.py)
- [tests/src/config/test_schema.py](../../tests/src/config/test_schema.py)
- [tests/src/proxy/test_converters.py](../../tests/src/proxy/test_converters.py)
- [tests/src/proxy/test_routing_invariants.py](../../tests/src/proxy/test_routing_invariants.py)
- [tests/integration/core/llm/test_openrouter_real.py](../../tests/integration/core/llm/test_openrouter_real.py)
- [tests/integration/proxy/test_proxy_openrouter_e2e.py](../../tests/integration/proxy/test_proxy_openrouter_e2e.py)

## 2. Cost Tracking And Spend Caps

Design anchors:

- [docs/design.md](../design.md) 3.14, cost tracking and spend caps
- [docs/design_appendix.md](../design_appendix.md) A.9, proxy cost configuration and logs
- [docs/proposals/runtime_abstraction.md](../proposals/runtime_abstraction.md), "Usage and Cost Visibility" and "Cost
  Caps"

Review invariants:

- Request cost records are append-only JSONL under `~/.forge/costs/requests/`.
- Verb cost records are append-only attribution estimates under `~/.forge/costs/verbs/`.
- Cost math uses integer microdollars, not float accumulation.
- Cost tracking is best effort; pricing/logging failure must not break successful LLM responses.
- Spend caps bootstrap from JSONL on proxy restart and enforce `post` or `strict` mode.
- `reject` returns HTTP 429 with `spend_cap_exceeded`; `warn` forwards and attaches `X-Spend-Warning`.
- Error responses and debug logs should avoid leaking request content or internal exception details.

Entry points:

- [src/forge/core/data/pricing.yaml](../../src/forge/core/data/pricing.yaml)
- [src/forge/core/models/pricing.py](../../src/forge/core/models/pricing.py)
- [src/forge/proxy/cost_logger.py](../../src/forge/proxy/cost_logger.py)
- [src/forge/proxy/cost_tracker.py](../../src/forge/proxy/cost_tracker.py)
- [src/forge/proxy/metrics.py](../../src/forge/proxy/metrics.py)
- [src/forge/proxy/server.py](../../src/forge/proxy/server.py)
- [src/forge/core/reactive/cost_tracking.py](../../src/forge/core/reactive/cost_tracking.py)
- [src/forge/cli/proxy_costs.py](../../src/forge/cli/proxy_costs.py)
- [src/forge/cli/proxy.py](../../src/forge/cli/proxy.py)
- [src/forge/cli/status_line.py](../../src/forge/cli/status_line.py)
- [src/forge/cli/workflow.py](../../src/forge/cli/workflow.py)
- [src/forge/session/handoff_agent.py](../../src/forge/session/handoff_agent.py)
- [src/forge/guard/semantic/supervisor.py](../../src/forge/guard/semantic/supervisor.py)

Tests:

- [tests/src/core/models/test_pricing.py](../../tests/src/core/models/test_pricing.py)
- [tests/src/proxy/test_cost_logger.py](../../tests/src/proxy/test_cost_logger.py)
- [tests/src/proxy/test_cost_tracker.py](../../tests/src/proxy/test_cost_tracker.py)
- [tests/src/proxy/test_metrics_integration.py](../../tests/src/proxy/test_metrics_integration.py)
- [tests/src/core/reactive/test_cost_tracking.py](../../tests/src/core/reactive/test_cost_tracking.py)
- [tests/src/cli/test_proxy_costs.py](../../tests/src/cli/test_proxy_costs.py)
- [tests/src/cli/test_proxy_commands.py](../../tests/src/cli/test_proxy_commands.py)
- [tests/src/cli/test_status_line.py](../../tests/src/cli/test_status_line.py)
- [tests/integration/proxy/test_cost_visibility_e2e.py](../../tests/integration/proxy/test_cost_visibility_e2e.py)
- [tests/regression/test_bug_caps_spend_cap_regressions.py](../../tests/regression/test_bug_caps_spend_cap_regressions.py)
- [tests/regression/test_bug_cap_preflight_wrong_model.py](../../tests/regression/test_bug_cap_preflight_wrong_model.py)
- [tests/regression/test_bug_cross_proxy_cap_bootstrap.py](../../tests/regression/test_bug_cross_proxy_cap_bootstrap.py)
- [tests/regression/test_bug_pricing_fallback_logs.py](../../tests/regression/test_bug_pricing_fallback_logs.py)
- [tests/regression/test_bug_error_detail_leak.py](../../tests/regression/test_bug_error_detail_leak.py)

## 3. Capability-Based Subprocess Routing

Design anchors:

- [docs/design.md](../design.md) 3.6.12, subprocess routing resolution
- [docs/design_appendix.md](../design_appendix.md) L, subprocess routing reference
- [docs/proposals/runtime_abstraction.md](../proposals/runtime_abstraction.md), Phase 0 and Phase 4

Review invariants:

- Subprocess routing follows the single ordered chain: explicit -> subprocess proxy -> preferred proxy -> route scan ->
  session proxy -> unresolved.
- Workflow invocations resolve a frozen `WorkerRoutingPlan` once at invocation start.
- Workflows fail closed on unresolved routing; supervisor and handoff paths fail open where documented.
- Direct workers are intentionally direct; unresolved workers are not silently treated as direct.
- Sidecar constraints are explicit and produce actionable errors when proxy registry data is unavailable.
- Workflow JSON exposes `resolved_models` so fallback routing is visible to users and skills.

Entry points:

- [src/forge/core/reactive/routing.py](../../src/forge/core/reactive/routing.py)
- [src/forge/review/routing.py](../../src/forge/review/routing.py)
- [src/forge/review/models.py](../../src/forge/review/models.py)
- [src/forge/review/engine.py](../../src/forge/review/engine.py)
- [src/forge/review/adversarial.py](../../src/forge/review/adversarial.py)
- [src/forge/review/consensus.py](../../src/forge/review/consensus.py)
- [src/forge/core/reactive/env.py](../../src/forge/core/reactive/env.py)
- [src/forge/core/reactive/session_runner.py](../../src/forge/core/reactive/session_runner.py)
- [src/forge/cli/workflow.py](../../src/forge/cli/workflow.py)
- [src/forge/cli/session_lifecycle.py](../../src/forge/cli/session_lifecycle.py)
- [src/forge/session/direct_model.py](../../src/forge/session/direct_model.py)
- [src/forge/guard/semantic/supervisor.py](../../src/forge/guard/semantic/supervisor.py)
- [src/forge/session/handoff_agent.py](../../src/forge/session/handoff_agent.py)

Tests:

- [tests/src/core/reactive/test_routing.py](../../tests/src/core/reactive/test_routing.py)
- [tests/src/core/reactive/test_subprocess_proxy.py](../../tests/src/core/reactive/test_subprocess_proxy.py)
- [tests/src/core/reactive/test_env.py](../../tests/src/core/reactive/test_env.py)
- [tests/src/review/test_routing.py](../../tests/src/review/test_routing.py)
- [tests/src/review/test_engine.py](../../tests/src/review/test_engine.py)
- [tests/src/review/test_models.py](../../tests/src/review/test_models.py)
- [tests/src/cli/test_workflow.py](../../tests/src/cli/test_workflow.py)
- [tests/src/cli/test_workflow_consensus.py](../../tests/src/cli/test_workflow_consensus.py)
- [tests/src/cli/test_workflow_preflight.py](../../tests/src/cli/test_workflow_preflight.py)
- [tests/src/cli/test_session_subprocess_proxy.py](../../tests/src/cli/test_session_subprocess_proxy.py)
- [tests/src/session/test_subprocess_proxy_inheritance.py](../../tests/src/session/test_subprocess_proxy_inheritance.py)
- [tests/integration/cli/test_workflow_integration.py](../../tests/integration/cli/test_workflow_integration.py)
- [tests/regression/test_bug_sidecar_subprocess_failopen.py](../../tests/regression/test_bug_sidecar_subprocess_failopen.py)
- [tests/regression/test_bug_via_direct_worker_warning.py](../../tests/regression/test_bug_via_direct_worker_warning.py)
- [tests/regression/test_bug_workflow_model_availability_stale_proxy.py](../../tests/regression/test_bug_workflow_model_availability_stale_proxy.py)

## 4. Model Catalog, Templates, And Prompt Addendums

Design anchors:

- [docs/design_appendix.md](../design_appendix.md) A.5, model catalog
- [docs/design_appendix.md](../design_appendix.md) A.10, system prompt addendums
- [docs/design.md](../design.md) 5.5, skills architecture and workflow runners

Review invariants:

- `model_catalog.yaml` remains the single source for model capabilities, context windows, provider refs, pricing hooks,
  Responses API routing, and system prompt addendum references.
- Templates declare `proxy.family`; route derivation uses family metadata for native-family preference.
- Non-Claude addendums are injected at session launch, not inside proxy request handling.
- Addendum lookup fails open for unknown OpenRouter models.
- OpenAI/Gemini tool-discipline addendums should reduce malformed tool calls without changing Claude routes.
- Workflow model expansion should not hide missing auth or silently fall back to Claude-only routing.

Entry points:

- [src/forge/core/data/model_catalog.yaml](../../src/forge/core/data/model_catalog.yaml)
- [src/forge/core/models/catalog.py](../../src/forge/core/models/catalog.py)
- [src/forge/core/models/types.py](../../src/forge/core/models/types.py)
- [src/forge/core/data/system_prompt_addendums/openai.md](../../src/forge/core/data/system_prompt_addendums/openai.md)
- [src/forge/core/data/system_prompt_addendums/gemini.md](../../src/forge/core/data/system_prompt_addendums/gemini.md)
- [src/forge/cli/session_addendum.py](../../src/forge/cli/session_addendum.py)
- [src/forge/cli/session_lifecycle.py](../../src/forge/cli/session_lifecycle.py)
- [src/forge/config/defaults/templates/\*.yaml](../../src/forge/config/defaults/templates/)
- [src/forge/proxy/client_adapter.py](../../src/forge/proxy/client_adapter.py)
- [src/forge/proxy/utils.py](../../src/forge/proxy/utils.py)
- [src/forge/review/models.py](../../src/forge/review/models.py)
- [src/skills/qa/resources/checklist/14-workflow.md](../../src/skills/qa/resources/checklist/14-workflow.md)

Tests:

- [tests/src/core/models/test_model_catalog_validation.py](../../tests/src/core/models/test_model_catalog_validation.py)
- [tests/src/core/models/test_model_catalog_resolution.py](../../tests/src/core/models/test_model_catalog_resolution.py)
- [tests/src/session/test_direct_model.py](../../tests/src/session/test_direct_model.py)
- [tests/src/cli/test_session_commands.py](../../tests/src/cli/test_session_commands.py)
- [tests/integration/docker/test_system_prompt_addendum.py](../../tests/integration/docker/test_system_prompt_addendum.py)
- [tests/regression/test_bug_openai_read_pages_loop.py](../../tests/regression/test_bug_openai_read_pages_loop.py)
- [tests/regression/test_bug_gpt5_responses_api_catalog_drift.py](../../tests/regression/test_bug_gpt5_responses_api_catalog_drift.py)
- [tests/regression/test_bug_minimax_reasoning_drift.py](../../tests/regression/test_bug_minimax_reasoning_drift.py)
- [tests/src/review/test_models.py](../../tests/src/review/test_models.py)
- [tests/src/review/test_skill_content.py](../../tests/src/review/test_skill_content.py)

## 5. Session Handoff And Memory Management

Design anchors:

- [docs/design.md](../design.md) 3.9, session resume and context management
- [docs/design.md](../design.md) 5.6, designated memory docs
- [docs/design_appendix.md](../design_appendix.md) G, memory doc reference
- [docs/proposals/runtime_abstraction.md](../proposals/runtime_abstraction.md), "Curated Handoff as Cross-Runtime
  Substrate"

Review invariants:

- `prev_sessions` uses a parent directory with a regeneratable `generated.md` cache and durable per-child files under
  `children/<child>.md`.
- Regenerating a parent cache must not overwrite an existing child handoff file.
- `forge session resume --fresh --review` opens the per-child handoff file in `$EDITOR`; non-zero editor exit aborts
  launch.
- `forge session handoff show` reads persisted handoff review reports from session artifacts.
- `forge session memory add-doc` validates target file existence and safe paths up front.
- Runtime skip handling still exists for stale manifests or manual JSON edits.
- Garbage collection recognizes the new layout, orphaned parent dirs, unreferenced child files, and legacy flat files.

Entry points:

- [src/forge/session/prev_sessions.py](../../src/forge/session/prev_sessions.py)
- [src/forge/session/handoff.py](../../src/forge/session/handoff.py)
- [src/forge/session/handoff_agent.py](../../src/forge/session/handoff_agent.py)
- [src/forge/session/manager.py](../../src/forge/session/manager.py)
- [src/forge/session/models.py](../../src/forge/session/models.py)
- [src/forge/cli/session_lifecycle.py](../../src/forge/cli/session_lifecycle.py)
- [src/forge/cli/session_fork.py](../../src/forge/cli/session_fork.py)
- [src/forge/cli/session_handoff.py](../../src/forge/cli/session_handoff.py)
- [src/forge/cli/session_memory.py](../../src/forge/cli/session_memory.py)
- [src/forge/cli/session_manage.py](../../src/forge/cli/session_manage.py)
- [src/forge/core/ops/gc.py](../../src/forge/core/ops/gc.py)
- [src/forge/core/ops/session_context.py](../../src/forge/core/ops/session_context.py)
- [src/forge/core/workqueue/queue.py](../../src/forge/core/workqueue/queue.py)

Tests:

- [tests/src/session/test_prev_sessions.py](../../tests/src/session/test_prev_sessions.py)
- [tests/src/session/test_handoff.py](../../tests/src/session/test_handoff.py)
- [tests/src/session/test_handoff_agent.py](../../tests/src/session/test_handoff_agent.py)
- [tests/src/cli/test_session_resume_review.py](../../tests/src/cli/test_session_resume_review.py)
- [tests/src/cli/test_session_handoff_show.py](../../tests/src/cli/test_session_handoff_show.py)
- [tests/src/cli/test_session_memory.py](../../tests/src/cli/test_session_memory.py)
- [tests/src/cli/test_session_commands.py](../../tests/src/cli/test_session_commands.py)
- [tests/src/core/ops/test_gc.py](../../tests/src/core/ops/test_gc.py)
- [tests/src/core/ops/test_session_context.py](../../tests/src/core/ops/test_session_context.py)
- [tests/integration/cli/test_handoff_integration.py](../../tests/integration/cli/test_handoff_integration.py)
- [tests/integration/cli/test_session_commands_integration.py](../../tests/integration/cli/test_session_commands_integration.py)
- [tests/regression/test_bug_handoff_forge_root.py](../../tests/regression/test_bug_handoff_forge_root.py)
- [tests/regression/test_bug_prev_sessions_parent_scope.py](../../tests/regression/test_bug_prev_sessions_parent_scope.py)
- [tests/regression/test_bug_resume_autoname_context_retry.py](../../tests/regression/test_bug_resume_autoname_context_retry.py)

## Suggested Review Order

1. Read the PR description and [docs/proposals/runtime_abstraction.md](../proposals/runtime_abstraction.md) "PR #8
   Alignment".
2. Review OpenRouter gateway support.
3. Review model catalog/template changes needed by OpenRouter and workflows.
4. Review capability-based subprocess routing.
5. Review cost tracking and spend caps.
6. Review session handoff/memory changes.
7. Cross-check docs: verify [docs/proposals/runtime_abstraction.md](../proposals/runtime_abstraction.md) marks Phases
   1-6 as future work, keeps runtime/provider/gateway/auth distinct, and matches the implemented CLI surfaces in
   end-user docs.

Documentation cross-check tests:

- [tests/src/cli/test_workflow.py](../../tests/src/cli/test_workflow.py) covers workflow help, JSON output, `--proxy`,
  `list-models`, and `resolved_models`.
- [tests/src/cli/test_workflow_preflight.py](../../tests/src/cli/test_workflow_preflight.py) covers routing warnings and
  missing-`claude` preflight output.
- [tests/src/cli/test_auth.py](../../tests/src/cli/test_auth.py) covers auth help/status/login command surfaces.
- [tests/src/cli/test_session_resume_review.py](../../tests/src/cli/test_session_resume_review.py) covers
  `session resume --fresh --review`.
- [tests/src/cli/test_session_memory.py](../../tests/src/cli/test_session_memory.py) covers `session memory` command
  behavior.
- [tests/src/cli/test_session_handoff_show.py](../../tests/src/cli/test_session_handoff_show.py) covers
  `session handoff show`.
- [tests/integration/cli/test_handoff_integration.py](../../tests/integration/cli/test_handoff_integration.py) covers
  `memory add-doc` through persisted handoff reports.

## Top-Level Verification Commands

The PR description records these completed checks:

- `make test-unit`
- `make pre-commit`
- `forge proxy create openrouter-openai` plus `forge proxy costs`
- `forge workflow panel src/ --code`
- spend cap enforcement with HTTP 429 on cap hit
- `forge session resume --fresh --review`
- `forge session handoff show`
- `forge session memory add-doc`
- `/forge:smoke-test`
- `/forge:walkthrough`

For targeted local review, use the test files listed in each section before rerunning the full suite.
