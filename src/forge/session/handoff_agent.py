"""Handoff agent for automatic memory doc updates.

The handoff agent runs after session stop (via work queue) to update
designated project memory documents. It spawns ``claude -p`` as a headless
subprocess that reads the session transcript and writes updates to
configured designated docs.

Supports two modes:
- **Direct update (Mode 1)**: Agent edits designated docs in-place.
- **Shadow/propose (Mode 2)**: Agent writes suggestions to a shadow file
  for human review, reading the official doc first for comparison.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from forge.core.reactive.routing import resolve_subprocess_routing
from forge.core.reactive.session_runner import run_claude_session
from forge.core.transcript import parse_jsonl_transcript
from forge.session.claude.invoke import is_claude_available
from forge.session.models import DesignatedDoc, HandoffConfig

logger = logging.getLogger(__name__)


def _default_timeout() -> int:
    from forge.runtime_config import get_runtime_config

    return get_runtime_config().handoff_timeout


# Per-doc strategy instructions.
# Mode 1 (direct update): strictly additive — no removals/rewrites.
# Mode 2 (suggested): self-prunes merged items from shadow file.
DOC_STRATEGIES: dict[str, str] = {
    "project-state": (
        "Update current focus, active work, recent decisions, and handoff notes. "
        "Mark completed items as done rather than removing them. "
        "If the file does not exist, skip it and report that it was missing."
    ),
    "checklist": (
        "Mark completed tasks with [x]. Add newly discovered tasks. "
        "Do NOT remove, rewrite, or restructure existing entries. "
        "If the file does not exist, skip it and report that it was missing."
    ),
    "changelog": (
        "Add accomplishments from this session not already recorded. "
        "Follow the existing entry format. "
        "Do NOT modify or remove existing entries. "
        "If the file does not exist, skip it and report that it was missing."
    ),
    "debugging": (
        "Record error causes, solutions, and workarounds encountered in this session. "
        "Group entries by topic (build errors, runtime errors, test failures, etc.). "
        "Do NOT duplicate entries that are already documented. "
        "If the file does not exist, skip it and report that it was missing."
    ),
    "patterns": (
        "Record architecture patterns, conventions, and recurring techniques observed "
        "in this session. Include code idioms, design patterns, and naming conventions. "
        "Do NOT duplicate patterns that are already documented. "
        "If the file does not exist, skip it and report that it was missing."
    ),
    "suggested": (
        "Propose additions to the official document as `- [ ]` checkboxes, each with "
        "a brief rationale. Remove any checkboxes whose content has already been merged "
        "into the official document (self-prune). "
        "Do NOT duplicate suggestions that are already present in either file."
    ),
    "generic": (
        "Read the file and add any NEW information from this session that is missing. "
        "Do NOT duplicate, rephrase, or remove what is already documented. "
        "If the file does not exist, skip it and report that it was missing."
    ),
}

MULTI_DOC_PROMPT_TEMPLATE = """\
You are a project documentation agent. Your job is to update project documents \
based on a completed Claude Code session.

## Session Information
- Session name: {session_name}
- Transcript: {transcript_path}

## Instructions
1. Read the session transcript at `{transcript_path}`
2. For EACH file listed below, read the existing content first
3. {action_instruction}

IMPORTANT: Read each file BEFORE modifying it.
Only make the minimal edits described in each file's instructions below.
Do not duplicate, rephrase, or remove content beyond what the per-file instructions specify.
If everything is already documented for a file, skip it entirely.

## Files to Update
{file_sections}
"""

MULTI_DOC_AUGMENT_INSTRUCTION = "Apply the specified updates to each file"
MULTI_DOC_REVIEW_INSTRUCTION = "Print to stdout what changes you would make to each file. Do NOT modify any files."


def build_multi_doc_prompt(
    *,
    session_name: str,
    transcript_path: str,
    mode: str = "augment",
    designated_docs: list[DesignatedDoc],
) -> str:
    """Build a multi-doc prompt for the handoff agent.

    Generates a single prompt that instructs ``claude -p`` to update
    multiple designated documents with per-doc strategies. For shadow docs
    (``doc.shadows`` is set), the prompt instructs reading the official
    document first before proposing changes.

    Args:
        session_name: The Forge session name.
        transcript_path: Absolute path to the transcript artifact.
        mode: "augment" (write updates) or "review-only" (print suggestions).
        designated_docs: List of DesignatedDoc entries to update.

    Returns:
        The complete prompt string.
    """
    action_instruction = MULTI_DOC_AUGMENT_INSTRUCTION if mode == "augment" else MULTI_DOC_REVIEW_INSTRUCTION

    sections: list[str] = []
    for doc in designated_docs:
        instructions = DOC_STRATEGIES.get(doc.strategy, DOC_STRATEGIES["generic"])

        if doc.shadows:
            # Shadow/propose mode (Mode 2): read official doc first, then propose
            section = (
                f"### `{doc.path}` (proposes changes to `{doc.shadows}`)\n"
                f"1. Read the OFFICIAL document at `{doc.shadows}` first.\n"
                f"2. Read this shadow document at `{doc.path}` (if it exists).\n"
                f"3. {instructions}"
            )
        else:
            # Direct update mode (Mode 1)
            section = f"### `{doc.path}`\n{instructions}"

        sections.append(section)

    file_sections = "\n\n".join(sections)

    return MULTI_DOC_PROMPT_TEMPLATE.format(
        session_name=session_name,
        transcript_path=transcript_path,
        action_instruction=action_instruction,
        file_sections=file_sections,
    )


def count_conversation_turns(transcript_path: Path) -> int:
    """Count user-initiated conversation turns in a transcript JSONL file.

    For newer format (requestId + message.role): counts unique requestId groups
    that contain at least one user message.
    For older format (type field): counts entries with type 'human'.

    Args:
        transcript_path: Path to the JSONL transcript file.

    Returns:
        Number of conversation turns. 0 if file is missing or empty.
    """
    entries = parse_jsonl_transcript(transcript_path)
    if not entries:
        return 0

    has_request_ids = any(e.get("requestId") for e in entries)

    if has_request_ids:
        user_request_ids: set[str] = set()
        for entry in entries:
            request_id = entry.get("requestId", "")
            if not request_id:
                continue
            message = entry.get("message", {})
            if isinstance(message, dict) and message.get("role") == "user":
                user_request_ids.add(request_id)
        return len(user_request_ids)

    return sum(1 for e in entries if e.get("type") == "human")


def resolve_handoff_base_url(
    proxy_id: str | None,
    confirmed_proxy_base_url: str | None = None,
    env_base_url: str | None = None,
    *,
    direct: bool = False,
    subprocess_proxy: str | None = None,
) -> str | None:
    """Resolve ANTHROPIC_BASE_URL for the handoff agent.

    When direct=True, short-circuits the entire chain and returns None
    (forces direct Anthropic routing regardless of session proxy).

    Delegates to ``resolve_subprocess_routing()`` with fail-open semantics.
    The handoff's proxy_id is soft (preferred, not strict) because handoff
    is async/best-effort — using the session's confirmed proxy is better
    than failing.

    Priority chain (when not direct):
    1. proxy_id -> preferred_proxy (handoff config, soft)
    2. subprocess_proxy -> persisted session subprocess proxy (soft)
    3. confirmed_proxy_base_url -> session's confirmed proxy
    4. env_base_url -> current ANTHROPIC_BASE_URL
    5. None -> Anthropic direct

    Args:
        proxy_id: Optional proxy from HandoffConfig. Soft: falls through
            on miss (unlike workflow's strict --proxy).
        confirmed_proxy_base_url: Base URL from session's confirmed proxy.
        env_base_url: Fallback base URL from environment.
        direct: When True, force direct routing (skip all proxy resolution).
        subprocess_proxy: Session-level subprocess proxy intent.

    Returns:
        base_url string or None.
    """
    if direct:
        return None

    for candidate in (proxy_id, subprocess_proxy):
        if not candidate:
            continue
        result = resolve_subprocess_routing(
            preferred_proxy=candidate,
            require_route=False,
            use_environment=False,
        )

        if result.base_url:
            return result.base_url

    return confirmed_proxy_base_url or env_base_url


# Paths with these characters are rejected to prevent prompt injection when
# interpolated into markdown headings (e.g., backticks break ```...``` blocks,
# newlines inject arbitrary prompt lines, control chars corrupt structure).
_UNSAFE_PATH_RE = re.compile(r"[`\x00-\x1f\x7f]")


def _is_safe_path(path: str, base: Path, resolved_base: Path) -> str | None:
    """Check a single path for safety. Return rejection reason or None if safe."""
    if Path(path).is_absolute():
        return f"absolute path: {path}"
    if _UNSAFE_PATH_RE.search(path):
        return f"unsafe characters: {path!r}"
    abs_path = (base / path).resolve()
    if not abs_path.is_relative_to(resolved_base):
        return f"escapes base directory: {path}"
    return None


_PERMISSION_DENIED_PATTERNS = [
    re.compile(r"(?:need|require|don.t have).{0,30}(?:write|edit|permission)", re.IGNORECASE),
    re.compile(r"(?:not|isn.t|aren.t).{0,20}(?:allowed|permitted).{0,20}(?:write|edit|modify)", re.IGNORECASE),
    re.compile(r"cannot (?:write|edit|modify) files", re.IGNORECASE),
]


def _stdout_indicates_permission_denied(stdout: str) -> bool:
    """Detect permission-denied responses where Claude exits 0 but couldn't write."""
    if not stdout:
        return False
    # Only check the first ~2000 chars — permission messages appear early
    sample = stdout[:2000]
    return any(p.search(sample) for p in _PERMISSION_DENIED_PATTERNS)


def _validate_designated_docs(
    designated_docs: list[DesignatedDoc],
    forge_root: Path,
) -> list[DesignatedDoc]:
    """Validate and filter designated docs.

    Guards (per doc):
    1. Path safety: reject absolute, unsafe chars, traversal
       (applied to both ``path`` and ``shadows``).
    2. Strategy consistency: ``suggested`` requires ``shadows``;
       ``shadows`` requires ``suggested``.

    Args:
        designated_docs: List of docs to validate.
        forge_root: Resolved worktree directory (base for path resolution).

    Returns:
        Filtered list containing only valid docs.
    """
    valid: list[DesignatedDoc] = []
    resolved_base = forge_root.resolve()
    for doc in designated_docs:
        reason = _is_safe_path(doc.path, forge_root, resolved_base)
        if reason:
            logger.warning("Skipping designated_doc (%s): %s", doc.path, reason)
            continue

        if doc.shadows is not None:
            reason = _is_safe_path(doc.shadows, forge_root, resolved_base)
            if reason:
                logger.warning("Skipping designated_doc shadows (%s): %s", doc.shadows, reason)
                continue

        # Strategy consistency: suggested ↔ shadows (non-empty)
        if doc.strategy == "suggested" and not doc.shadows:
            logger.warning(
                "Skipping designated_doc %s: strategy 'suggested' requires non-empty 'shadows'",
                doc.path,
            )
            continue
        if doc.shadows is not None and doc.strategy != "suggested":
            logger.warning(
                "Skipping designated_doc %s: 'shadows' requires strategy 'suggested' " "(got %r)",
                doc.path,
                doc.strategy,
            )
            continue
        if doc.shadows and doc.path == doc.shadows:
            logger.warning(
                "Skipping designated_doc %s: 'path' and 'shadows' must differ",
                doc.path,
            )
            continue

        valid.append(doc)
    return valid


def run_handoff_agent(
    *,
    session_name: str,
    forge_root: Path,
    transcript_snapshot_rel: str,
    config: HandoffConfig,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    designated_docs: list[DesignatedDoc] | None = None,
) -> bool:
    """Run the handoff agent as a ``claude -p`` subprocess.

    This is the main entry point called by ``forge handoff run``.

    Args:
        session_name: Forge session name.
        forge_root: Forge project root (where .forge/ lives). Designated doc paths
                    resolve against this directory. Also used as cwd for the subprocess.
        transcript_snapshot_rel: Forge-root-relative path to transcript artifact.
        config: HandoffConfig with mode, min_turns, proxy_id.
        base_url: Resolved ANTHROPIC_BASE_URL (or None for direct).
        timeout_seconds: Max seconds for the agent to run.
        designated_docs: List of docs to update. If None or empty, the agent
                         has nothing to do and returns True (skip).

    Returns:
        True if agent completed successfully (or skipped), False on error.
    """
    project_root = forge_root

    # Validate transcript path (system boundary: CLI args / marker payload)
    reason = _is_safe_path(transcript_snapshot_rel, project_root, project_root.resolve())
    if reason:
        logger.warning("Handoff agent: unsafe transcript path (%s)", reason)
        return False
    transcript_abs = (project_root / transcript_snapshot_rel).resolve()

    if not transcript_abs.is_file():
        logger.warning("Handoff agent: transcript not found at %s", transcript_abs)
        return False

    turn_count = count_conversation_turns(transcript_abs)
    if turn_count < config.min_turns:
        logger.info(
            "Handoff skipped: session %s had %d turns (min_turns=%d)",
            session_name,
            turn_count,
            config.min_turns,
        )
        return True  # Not a failure — just below threshold

    _VALID_MODES = {"augment", "review-only"}
    if config.mode not in _VALID_MODES:
        logger.warning("Handoff agent: unknown mode %r (expected %s)", config.mode, _VALID_MODES)
        return False

    if not is_claude_available():
        logger.warning("Handoff agent: claude CLI not found in PATH")
        return False

    if not designated_docs:
        logger.info(
            "No designated_docs configured; handoff agent has nothing to update " "(session %s)",
            session_name,
        )
        return True

    safe_docs = _validate_designated_docs(designated_docs, forge_root)

    # Only update files that already exist — handoff never creates new files.
    ready_docs: list[DesignatedDoc] = []
    for doc in safe_docs:
        if not (forge_root / doc.path).is_file():
            logger.info("Skipping missing file: %s", doc.path)
            continue
        # For shadow docs, the official doc must also exist
        if doc.shadows and not (forge_root / doc.shadows).is_file():
            logger.info(
                "Skipping shadow doc %s: official doc %s not found",
                doc.path,
                doc.shadows,
            )
            continue
        ready_docs.append(doc)

    if not ready_docs:
        logger.info(
            "No designated_docs ready after validation/existence checks (session %s)",
            session_name,
        )
        return True

    prompt = build_multi_doc_prompt(
        session_name=session_name,
        transcript_path=str(transcript_abs),
        mode=config.mode,
        designated_docs=ready_docs,
    )

    logger.info(
        "Running handoff agent for session %s (mode=%s, turns=%d)",
        session_name,
        config.mode,
        turn_count,
    )

    # Use forge_root as cwd so designated doc paths (relative) resolve
    # against the correct branch content. Transcript path is absolute.
    from forge.core.reactive.cost_tracking import track_verb_cost

    effective_timeout = timeout_seconds if timeout_seconds is not None else _default_timeout()
    tracking_url = base_url

    with track_verb_cost("handoff", [tracking_url] if tracking_url else []):
        result = run_claude_session(
            prompt,
            base_url=base_url,
            direct=config.direct,
            timeout_seconds=effective_timeout,
            cwd=str(forge_root),
        )

    if not result.success:
        detail = result.error or (result.stderr[:500] if result.stderr else f"exit {result.returncode}")
        logger.warning("Handoff agent for %s failed: %s", session_name, detail)
        return False

    # Only check for permission denial in augment mode. review-only mode
    # explicitly tells Claude "Do NOT modify any files", so a compliant
    # response like "I cannot modify files" is expected, not an error.
    if config.mode == "augment" and _stdout_indicates_permission_denied(result.stdout):
        logger.warning(
            "Handoff agent for %s: Claude lacked Write/Edit permissions — no files modified. "
            "Run 'forge claude preset edit' to add Write/Edit to permissions.allow.",
            session_name,
        )
        return False

    logger.info("Handoff agent completed for session %s", session_name)
    return True
