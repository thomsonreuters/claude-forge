"""System prompt addendum resolution and file writing for proxy-routed sessions.

Non-Claude models (GPT, Gemini) misuse Claude Code's tools. This module
resolves the correct addendum from the model catalog and writes it to a
file for injection via --append-system-prompt-file.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_addendum_content_for_proxy(proxy_id: str | None) -> str | None:
    """Resolve system prompt addendum content for a proxy's model family.

    Inspects all configured tier models. If all non-empty tiers resolve to the
    same addendum, use it. If mixed, use default_tier and log the choice.
    Returns None for direct mode, unknown models, or Claude-family proxies.
    """
    if not proxy_id:
        return None
    try:
        from forge.config.loader import load_proxy_instance_config
        from forge.core.models import get_system_prompt_addendum

        config = load_proxy_instance_config(proxy_id)
        if config is None:
            return None

        tier_models = {
            "haiku": config.tiers.haiku,
            "sonnet": config.tiers.sonnet,
            "opus": config.tiers.opus,
        }
        addendums: dict[str, str | None] = {}
        for tier, model in tier_models.items():
            if not model:
                continue
            canonical = model.split("/")[-1] if "/" in model else model
            addendums[tier] = get_system_prompt_addendum(canonical)

        unique_values = set(addendums.values())
        if unique_values == {None}:
            return None
        if len(unique_values) == 1:
            return unique_values.pop()
        default_tier = config.default_tier or "sonnet"
        logger.debug(
            "Mixed addendums across tiers for proxy %s; using %s tier",
            proxy_id,
            default_tier,
        )
        default_model = tier_models.get(default_tier, "")
        if default_model:
            canonical = default_model.split("/")[-1] if "/" in default_model else default_model
            return get_system_prompt_addendum(canonical)
        return None
    except Exception:
        logger.debug("Addendum resolution failed for proxy %s", proxy_id, exc_info=True)
        return None


def write_managed_addendum(forge_root: Path, session_name: str, content: str) -> Path:
    """Write addendum to .forge/launch-context/{session_name}.addendum.md."""
    launch_dir = forge_root / ".forge" / "launch-context"
    launch_dir.mkdir(parents=True, exist_ok=True)
    path = launch_dir / f"{session_name}.addendum.md"
    path.write_text(content, encoding="utf-8")
    return path


def write_bare_addendum(content: str) -> Path:
    """Write addendum to a temp file (caller must keep alive and clean up)."""
    f = tempfile.NamedTemporaryFile(
        suffix=".md", prefix="forge-addendum-", delete=False, mode="w", encoding="utf-8"
    )
    f.write(content)
    f.close()
    return Path(f.name)
