"""Semantic supervisor invocation.

The supervisor is an LLM session that validates executor actions against
an approved plan. It uses `claude -p --resume <session_id> --fork-session`
to fork the planning session without polluting its conversation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.core.reactive.proxy import lookup_proxy_base_url
from forge.core.reactive.session_runner import run_claude_session
from forge.core.reactive.throttle import ThrottleCache, compute_cache_key
from forge.guard.deterministic.base import DeterministicPolicy
from forge.guard.semantic.verdict import (
    SupervisorVerdict,
    parse_supervisor_verdict,
    verdict_to_decision,
)
from forge.guard.types import ActionContext, PolicyDecision
from forge.session.models import PolicyIntent, SessionState, SupervisorConfig

_log = logging.getLogger(__name__)

_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-" r"[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{12}$"
)

SUPERVISOR_INTENT = (
    "Ensure implementation stays aligned with the approved plan. The supervisor "
    "checks that code changes match what was agreed upon, catching drift before "
    "it compounds."
)

# Supervisor prompt template
SUPERVISOR_PROMPT = """You are a code alignment supervisor. Evaluate whether this action aligns with the approved plan.

## Action Being Evaluated
Tool: {tool_name}
Target: {target_path}
Content/Diff (truncated):
```
{content}
```

## Instructions
1. Compare this action against the approved plan in your context
2. Determine if the action is ALIGNED or DIVERGENT
3. If divergent, cite the specific plan section being violated
4. Express your confidence level (0.0-1.0)

## Response Format
Respond with JSON in a code fence:
```json
{{
  "verdict": "aligned" | "divergent",
  "confidence": 0.95,
  "violations": [
    {{
      "severity": "high",
      "evidence": "what was done that violates the plan",
      "suggested_fix": "what should be done instead",
      "citations": ["quoted plan section that was violated"]
    }}
  ]
}}
```

If the action aligns with the plan, use an empty violations array:
```json
{{
  "verdict": "aligned",
  "confidence": 0.9,
  "violations": []
}}
```
"""

_PLAN_OVERRIDE_PREAMBLE = """## Updated Plan (supersedes earlier plan in conversation context)

The following plan is MORE RECENT than any plan discussed earlier in this conversation.
Use THIS plan as the authoritative reference for alignment checking. If there are
conflicts between this plan and earlier conversation context, THIS plan takes precedence.

{plan_content}

---"""


def _plan_fingerprint(path: str, forge_root: str | None) -> str:
    """Return a cheap fingerprint for cache key differentiation: path:mtime_ns:size."""
    resolved = Path(path)
    if not resolved.is_absolute() and forge_root:
        resolved = Path(forge_root) / resolved
    try:
        st = resolved.stat()
        return f"{resolved}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return f"{path}:missing"


def _load_plan_override(config: SupervisorConfig) -> str | None:
    """Read the plan override file from disk. Returns None if not set, missing, or empty."""
    if not config.plan_override_path:
        return None
    try:
        resolved = Path(config.plan_override_path)
        if not resolved.is_absolute() and config.forge_root:
            resolved = Path(config.forge_root) / resolved
        if not resolved.is_file():
            _log.warning("Supervisor plan_override_path file not found: %s", resolved)
            return None
        content = resolved.read_text(encoding="utf-8").strip()
        if not content:
            _log.warning("Supervisor plan_override_path file is empty: %s", resolved)
            return None
        return content
    except Exception as e:
        _log.warning("Failed to read supervisor plan_override_path: %s", e)
        return None


class SemanticSupervisorPolicy(DeterministicPolicy):
    """Semantic policy that invokes an LLM supervisor to validate actions.

    Implements StatefulPolicy to manage the supervisor cache via ThrottleCache.
    Cached verdicts are reused within the throttle window to avoid
    excessive LLM calls.

    State tracked:
    - cache: ThrottleCache entries {cache_key: {checked_at, verdict, confidence}}
    """

    def __init__(self, config: SupervisorConfig | None = None) -> None:
        self._config = config
        ttl = config.throttle_seconds if config else 30
        self._cache = ThrottleCache(ttl_seconds=ttl)

    @property
    def policy_id(self) -> str:
        return "semantic.supervisor"

    @property
    def description(self) -> str:
        return "Validate actions against approved plan via LLM supervisor"

    @property
    def intent(self) -> str:
        return SUPERVISOR_INTENT

    def applies_to(self, context: ActionContext) -> bool:
        """Apply to Write/Edit when supervisor is configured and not suspended."""
        if context.tool_name not in ("Write", "Edit"):
            return False
        if self._config is None or self._config.resume_id is None:
            return False
        return not self._config.suspended

    def _evaluate(self, context: ActionContext) -> PolicyDecision:
        """Evaluate action via supervisor (with caching)."""
        if not self._config or not self._config.resume_id:
            return PolicyDecision(
                decision="allow",
                policy_id=self.policy_id,
                warnings=["Supervisor not configured"],
            )
        if self._config.suspended:
            return PolicyDecision(decision="allow", policy_id=self.policy_id)

        # Check cache
        cache_key = compute_cache_key(
            context.tool_name,
            context.target_path,
            context.new_content,
        )
        if self._config.plan_override_path:
            cache_key = (
                cache_key + "|plan:" + _plan_fingerprint(self._config.plan_override_path, self._config.forge_root)
            )

        cached = self._cache.check(cache_key)
        if cached is not None:
            _log.debug("Using cached supervisor verdict for %s", cache_key)
            verdict = SupervisorVerdict(
                verdict=cached.get("verdict", "aligned"),  # type: ignore[arg-type]
                confidence=cached.get("confidence", 1.0),
            )
            decision = verdict_to_decision(verdict, intent=self.intent)
            decision.cached = True
            return decision

        # Invoke supervisor
        decision = invoke_supervisor(self._config, context)

        # Attach intent to deny decisions
        if decision.decision == "deny":
            decision.intent = self.intent

        # Only cache genuinely clean allows. Warns, allow-with-warnings
        # (timeout/failure), and denials are NOT cached so they re-evaluate
        # on the next check.
        if decision.decision == "allow" and not decision.warnings:
            self._cache.update(cache_key, verdict="aligned", confidence=1.0)

        return decision

    def get_state(self) -> dict[str, Any]:
        """Return cache state for persistence."""
        return {"cache": self._cache.get_state()}

    def set_state(self, state: dict[str, Any]) -> None:
        """Restore cache state from persistence."""
        self._cache.set_state(state.get("cache", {}))


@dataclass
class _ResolvedTarget:
    """Result of resolving a supervisor resume target."""

    resume_id: str | None = None
    source_cwd: str | None = None  # Worktree path of source session (for cross-CWD resolution)
    warning: str | None = None


def _resolve_resume_target(resume_target: str, forge_root: str | None = None) -> _ResolvedTarget:
    """Resolve a supervisor resume target to a Claude UUID and source CWD.

    Accepts raw Claude UUIDs as-is. If the value looks like a Forge session name,
    resolve it through the session index and return that session's confirmed
    Claude UUID plus its worktree path (needed for cross-CWD supervisor
    invocations -- Claude Code scopes --resume to the project CWD).
    """
    target = resume_target.strip()
    if not target:
        return _ResolvedTarget(warning="Supervisor not configured (no resume_id)")

    if _UUID_PATTERN.fullmatch(target):
        return _ResolvedTarget(resume_id=target)

    try:
        from forge.session.manager import SessionManager

        state = SessionManager().get_session(target, forge_root=forge_root)
    except Exception:
        return _ResolvedTarget(resume_id=target)

    session_uuid = state.confirmed.claude_session_id
    if not session_uuid:
        return _ResolvedTarget(
            warning=f"Supervisor error: Forge session '{target}' has no confirmed Claude session ID, failing open"
        )

    from forge.session.claude.paths import resolve_claude_project_root

    source_cwd = resolve_claude_project_root(state)
    _log.debug("Resolved supervisor session %s -> %s (cwd=%s)", target, session_uuid[:16], source_cwd)
    return _ResolvedTarget(resume_id=session_uuid, source_cwd=source_cwd)


def invoke_supervisor(
    config: SupervisorConfig,
    context: ActionContext,
    *,
    intent: str | None = None,
) -> PolicyDecision:
    """Invoke the semantic supervisor via claude -p --resume.

    Args:
        config: Supervisor configuration
        context: Action being evaluated
        intent: Policy intent to attach to deny decisions.

    Returns:
        PolicyDecision based on supervisor verdict (fail-open on errors)
    """
    from forge.core.reactive.env import should_spawn_subprocesses

    if not should_spawn_subprocesses():
        _log.debug("Skipping supervisor at FORGE_DEPTH >= %d", 2)
        return PolicyDecision(
            decision="allow",
            policy_id="semantic.supervisor",
            warnings=["Supervisor skipped (FORGE_DEPTH limit reached)"],
        )

    if not config.resume_id:
        return PolicyDecision(
            decision="allow",
            policy_id="semantic.supervisor",
            warnings=["Supervisor not configured (no resume_id)"],
        )

    resolved = _resolve_resume_target(config.resume_id, forge_root=config.forge_root)
    if resolved.warning:
        _log.warning(resolved.warning)
        return PolicyDecision(
            decision="allow",
            policy_id="semantic.supervisor",
            warnings=[resolved.warning],
        )

    assert resolved.resume_id is not None

    prompt = SUPERVISOR_PROMPT.format(
        tool_name=context.tool_name,
        target_path=context.target_path or "N/A",
        content=(context.raw_diff or context.new_content or "")[:2000],
    )

    plan_content = _load_plan_override(config)
    if plan_content:
        prompt = _PLAN_OVERRIDE_PREAMBLE.format(plan_content=plan_content) + "\n\n" + prompt

    try:
        base_url = None if config.direct else (config.base_url or lookup_proxy_base_url(config.proxy))
    except Exception as e:
        _log.warning("Supervisor proxy '%s' not found: %s", config.proxy, e)
        return PolicyDecision(
            decision="warn",
            policy_id="semantic.supervisor",
            warnings=[f"Supervisor proxy '{config.proxy}' not found: {e}"],
        )

    from forge.core.reactive.cost_tracking import resolve_subprocess_proxy_url, track_verb_cost

    tracking_url = base_url
    if tracking_url is None and not config.direct:
        tracking_url = resolve_subprocess_proxy_url()

    with track_verb_cost("supervisor", [tracking_url] if tracking_url else []):
        result = run_claude_session(
            prompt,
            resume_id=resolved.resume_id,
            fork_session=config.fork_session,
            base_url=base_url,
            direct=config.direct,
            timeout_seconds=config.timeout_seconds,
            cwd=resolved.source_cwd,
        )

    if not result.success:
        _log.warning(
            "Supervisor invocation failed: %s",
            result.error or f"exit {result.returncode}",
        )
        return PolicyDecision(
            decision="allow",
            policy_id="semantic.supervisor",
            warnings=[f"Supervisor error: {result.error or f'exit {result.returncode}'}, failing open"],
        )

    verdict = parse_supervisor_verdict(result.stdout)
    return verdict_to_decision(verdict, intent=intent)


# --- Setup-time helpers (used by CLI, direct commands, and --supervise flags) ---


def validate_supervisor_target(target: str, forge_root: str | None = None) -> SessionState:
    """Validate a supervisor target session at setup time.

    Checks that the session exists, has a confirmed Claude UUID, and
    has evidence of a real conversation (hook confirmation or transcript).
    Pre-seeded UUIDs alone are not enough -- the same standard resume uses.

    Raises ValueError with a user-friendly message on failure. This
    runs at wiring time (not at check time) to fail loud on bad config.
    """
    from forge.session.manager import SessionManager

    try:
        state = SessionManager().get_session(target, forge_root=forge_root)
    except Exception as e:
        raise ValueError(f"Supervisor target session '{target}' not found: {e}") from e

    if not state.confirmed.claude_session_id:
        raise ValueError(
            f"Supervisor target session '{target}' has no confirmed Claude session ID. "
            f"Launch the session first so Claude materializes a conversation."
        )

    if not _has_conversation_evidence(state):
        raise ValueError(
            f"Supervisor target session '{target}' has a pre-seeded UUID but no confirmed "
            f"conversation. Launch the session first so Claude materializes a conversation."
        )

    return state


def _has_conversation_evidence(state: SessionState) -> bool:
    """Whether a session has evidence of a real Claude conversation.

    Mirrors the resume-flow's standard: hook confirmation (confirmed_by)
    or a transcript file on disk. Pre-seeded UUIDs without either are
    rejected to prevent silent supervisor degradation.
    """
    from pathlib import Path

    if state.confirmed.confirmed_by is not None:
        return True

    if state.confirmed.transcript_path and Path(state.confirmed.transcript_path).is_file():
        return True

    session_id = state.confirmed.claude_session_id
    if session_id:
        from forge.session.claude.paths import (
            get_transcript_path,
            resolve_claude_project_root,
        )

        try:
            return get_transcript_path(resolve_claude_project_root(state), session_id).is_file()
        except Exception:
            pass

    return False


def auto_seed_supervisor_proxy(
    source_state: SessionState,
    current_proxy_id: str | None,
    current_template: str | None,
    current_direct: bool,
) -> str | None:
    """Return proxy to seed on SupervisorConfig when routing differs.

    When the source session used a different proxy/routing than the current
    session, the supervisor needs to reach the source's model. Compares full
    routing tuple (proxy_id, template, direct) to detect mismatches.

    Returns source's proxy_id or template for seeding, or None if routing
    matches or source has no confirmed proxy. Best-effort: returns None on
    any error.
    """
    try:
        swp = source_state.confirmed.started_with_proxy
        if not swp:
            return None

        source_routing = (swp.proxy_id, swp.template, False)
        current_routing = (current_proxy_id, current_template, current_direct)

        if source_routing == current_routing:
            return None

        return swp.proxy_id or swp.template
    except Exception:
        return None


def should_supervisor_use_direct(source_state: SessionState) -> bool:
    """Whether the supervisor should use direct Anthropic routing.

    Returns True when the source (planner) session ran in direct mode
    (no proxy). Without this, a proxied executor supervising a direct
    planner would route the supervisor through the executor's proxy
    via inherited ANTHROPIC_BASE_URL.
    """
    return not source_state.confirmed.started_with_proxy


def preflight_supervisor_proxy(supervisor_proxy: str) -> str:
    """Validate supervisor proxy against the registry before state mutation.

    Checks registry presence only, not liveness — a registered-but-stopped
    proxy passes. Use ``forge proxy clean`` to prune stale entries.

    Returns the resolved proxy_id. Raises ValueError if the proxy is not found.
    Call this before creating sessions/forks so a bad proxy name doesn't leave
    half-created state.
    """
    # Lazy import: guard → proxy dependency; kept lazy to avoid circular imports
    from forge.proxy.proxies import (
        ProxyRegistryStore,
        ProxyResolutionError,
        resolve_proxy,
    )

    registry = ProxyRegistryStore().read()
    try:
        entry = resolve_proxy(registry, supervisor_proxy)
    except ProxyResolutionError:
        raise ValueError(f"Supervisor proxy '{supervisor_proxy}' not found in registry")
    return entry.proxy_id or supervisor_proxy


def apply_supervisor_routing(
    sup_config: SupervisorConfig,
    source_state: SessionState,
    *,
    supervisor_proxy: str | None = None,
    supervisor_direct: bool = False,
    current_proxy_id: str | None = None,
    current_template: str | None = None,
    current_direct: bool = False,
) -> str | None:
    """Apply explicit or auto-seeded supervisor routing to sup_config.

    When supervisor_proxy is given, stores it directly (caller must have
    already validated via preflight_supervisor_proxy). When supervisor_direct
    is given, sets direct routing. Otherwise falls through to
    auto_seed_supervisor_proxy().

    Returns a display string for the routing choice (for CLI output), or None
    when routing matched and no override was needed.
    """
    if supervisor_proxy:
        sup_config.proxy = supervisor_proxy
        return supervisor_proxy
    elif supervisor_direct:
        sup_config.direct = True
        return "direct"
    else:
        seeded = auto_seed_supervisor_proxy(
            source_state,
            current_proxy_id=current_proxy_id,
            current_template=current_template,
            current_direct=current_direct,
        )
        if seeded:
            sup_config.proxy = seeded
        if should_supervisor_use_direct(source_state):
            sup_config.direct = True
            return seeded or "direct"
        return seeded


def apply_supervisor_to_intent(
    manifest: SessionState,
    sup_config: SupervisorConfig,
) -> None:
    """Apply supervisor config to manifest intent (not overrides).

    Also enables policy enforcement, which is required for the hook to
    evaluate supervisor checks (commands.py:1049 exits early otherwise).
    Clears any ``policy.enabled`` override so a prior ``%guard disable``
    doesn't shadow the intent (overrides take precedence in effective.py).

    Writes to intent rather than overrides so that supervision persists
    through ``resume --fresh`` which deepcopies ``intent.policy`` into
    child sessions (manager.py:712, 886).
    """
    from forge.session.overrides import delete_override

    if manifest.intent.policy is None:
        manifest.intent.policy = PolicyIntent(enabled=True, supervisor=sup_config)
    else:
        manifest.intent.policy.enabled = True
        manifest.intent.policy.supervisor = sup_config

    # Clear conflicting override so intent.policy.enabled takes effect.
    if manifest.overrides:
        delete_override(manifest.overrides, "policy.enabled")


# --- Plan reload resolution ---


@dataclass
class ResolvedReloadPlan:
    """Result of auto-resolving the latest approved plan for supervisor reload."""

    path: str
    source: str  # "self" | "fork" | "target"
    session_name: str
    captured_at: str


def resolve_supervisor_reload_plan_path(
    sup: SupervisorConfig,
    current_manifest: SessionState,
) -> ResolvedReloadPlan | None:
    """Search the supervision graph for the latest approved plan.

    Search order: current session -> related forks -> supervisor target.
    Only approved snapshots (ExitPlanMode artifacts) are considered.
    """
    from forge.guard.queries import read_scoped_supervisor_target
    from forge.session.index import IndexStore
    from forge.session.plan_resolution import latest_snapshot_path, resolve_plan_info
    from forge.session.store import SessionStore

    current_fr = current_manifest.forge_root
    if not current_fr:
        return None

    # Pre-step: resolve supervisor target identity (name + forge_root)
    target_name: str | None = None
    target_state: SessionState | None = None
    if sup.resume_id:
        target_state = read_scoped_supervisor_target(sup.resume_id, sup.forge_root, current_fr)
        if target_state is not None:
            target_name = sup.resume_id
            if _UUID_PATTERN.fullmatch(sup.resume_id):
                try:
                    match = IndexStore().find_session_by_uuid(sup.resume_id)
                    if match:
                        target_name = match[0]
                except Exception:
                    pass

    # Step 1: current supervised session (own approved plans only)
    info = resolve_plan_info(current_manifest, current_forge_root=current_fr)
    if info.source == "self" and info.approved_snapshots:
        snap_rel = latest_snapshot_path(info.approved_snapshots)
        if snap_rel:
            snap_abs = Path(current_fr) / snap_rel
            if snap_abs.is_file():
                captured = info.approved_snapshots[-1].get("captured_at", "")
                return ResolvedReloadPlan(
                    path=str(snap_abs),
                    source="self",
                    session_name=current_manifest.name,
                    captured_at=captured,
                )

    # Step 2: related forks in the same forge_root
    if target_name:
        best: ResolvedReloadPlan | None = None
        try:
            entries = IndexStore().list_sessions(forge_root_filter=current_fr)
            for name, _entry in entries:
                if name == current_manifest.name:
                    continue
                try:
                    fork_state = SessionStore(current_fr, name).read()
                except Exception:
                    continue
                # Check parent relationship
                parent = None
                if fork_state.confirmed.derivation:
                    parent = fork_state.confirmed.derivation.parent_session
                if not parent:
                    parent = fork_state.parent_session
                if parent != target_name:
                    continue
                # Check for approved plan snapshots
                plans = fork_state.confirmed.artifacts.get("plans", [])
                if not isinstance(plans, list):
                    continue
                for entry in reversed(plans):
                    if not isinstance(entry, dict) or entry.get("kind") != "approved":
                        continue
                    snap = entry.get("snapshot_path")
                    if not isinstance(snap, str):
                        continue
                    snap_abs = Path(current_fr) / snap
                    if not snap_abs.is_file():
                        continue
                    captured_at = entry.get("captured_at", "")
                    candidate = ResolvedReloadPlan(
                        path=str(snap_abs),
                        source="fork",
                        session_name=name,
                        captured_at=captured_at,
                    )
                    if best is None or captured_at > best.captured_at:
                        best = candidate
                    break  # Latest snapshot in this session found
        except Exception:
            _log.debug("Error scanning related forks for plan reload", exc_info=True)
        if best is not None:
            return best

    # Step 3: supervisor target session
    if target_state is not None and target_name:
        target_fr = target_state.forge_root or current_fr
        target_info = resolve_plan_info(target_state, current_forge_root=target_fr)
        if target_info.source == "self" and target_info.approved_snapshots:
            snap_rel = latest_snapshot_path(target_info.approved_snapshots)
            if snap_rel:
                snap_abs = Path(target_fr) / snap_rel
                if snap_abs.is_file():
                    captured = target_info.approved_snapshots[-1].get("captured_at", "")
                    return ResolvedReloadPlan(
                        path=str(snap_abs),
                        source="target",
                        session_name=target_name,
                        captured_at=captured,
                    )

    return None
