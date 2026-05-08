"""Status line command for Claude Code.

Invoked by Claude Code's statusLine setting. Reads JSON from stdin,
produces a formatted status line to stdout.

Layout (5 categories):
  Where | Who | What | Metrics | State
  path (branch) | breadcrumb | template [Model] ctx_bar | cost dur | +12/-3 | in:12K out:3K cache:8K | THINK | LOOP N/M | SC
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, NamedTuple

import click

# Set up minimal logging for status line (stderr to avoid polluting stdout)
logger = logging.getLogger(__name__)

# ANSI color codes
RED = "\033[31m"
RED_BOLD = "\033[31;1m"
LIGHT_RED = "\033[91m"
YELLOW = "\033[33m"
YELLOW_BOLD = "\033[33;1m"
GREEN = "\033[32m"
GREEN_BOLD = "\033[32;1m"
PURPLE = "\033[35m"
BLUE = "\033[94m"
BREADCRUMB_COLOR = "\033[38;5;139m"  # dusty plum
TEMPLATE_COLOR = "\033[38;5;60m"  # deep blue-gray
METRICS_COLOR = "\033[38;5;145m"  # cool grey

# Context bar gradient (Gradient E: soft green → warm → hot)
CTX_LOW = "\033[38;5;115m"  # soft green (<25%)
CTX_MED = "\033[38;5;150m"  # light olive (25-49%)
CTX_HIGH = "\033[38;5;179m"  # warm gold (50-74%)
CTX_WARN = "\033[38;5;173m"  # burnt orange (75-89%)
CTX_CRIT = "\033[38;5;167m"  # hot coral (90-100%)
BOLD = "\033[1m"

# Per-tier model colors (Option 4: navy family)
# 1M variants use a deeper shade of the same hue
TIER_HAIKU = "\033[38;5;67m"  # steel blue
TIER_SONNET = "\033[38;5;69m"  # cornflower
TIER_SONNET_DEEP = "\033[38;5;26m"  # deeper cornflower (1M context)
TIER_OPUS = "\033[38;5;75m"  # vivid blue
TIER_OPUS_DEEP = "\033[38;5;32m"  # deeper vivid blue (1M context)
DARK_GRAY = "\033[90m"
DIM = "\033[2m"
RESET = "\033[0m"

# ASCII display characters
PROGRESS_FILLED = "#"
PROGRESS_EMPTY = "-"

# Separator
SEP = f"{DARK_GRAY}|{RESET}"

# ASCII status indicators
THINKING_INDICATOR = "THINK"
VERIFICATION_INDICATOR = "LOOP"
SIDECAR_INDICATOR = "SC"
TOKEN_INPUT_LABEL = "in:"
TOKEN_OUTPUT_LABEL = "out:"
TOKEN_CACHE_LABEL = "cache:"
LINE_ADD_COLOR = "\033[38;5;28m"
LINE_REMOVE_COLOR = "\033[38;5;124m"

# Trailing margin width (non-breaking spaces) to prevent merging with Claude Code's
# native status display when rendered adjacent to custom statusLine output
TRAILING_MARGIN = 3

# Reserve for Claude Code's native token display (e.g., " 97595 tokens") appended
# to line 1. ccstatusline defaults to subtracting 40; we use a tighter estimate.
NATIVE_DISPLAY_RESERVE = 15

# Fallback terminal width when /dev/tty and COLUMNS are both unavailable.
# Conservative: "too narrow = mild truncation" is better than "too wide = wrapping bug".
DEFAULT_TERM_WIDTH = 80

# Separator as it appears in hardened output (spaces → NBSPs)
_HARDENED_SEP = f"\u00a0{SEP}\u00a0"


def _get_terminal_width() -> int:
    """Get terminal width, even when stdout is piped.

    Claude Code always pipes to statusLine commands, so os.get_terminal_size()
    on stdout fails. Instead, open /dev/tty (the controlling terminal) directly
    to query the real width. Falls back to COLUMNS env var, then DEFAULT_TERM_WIDTH.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        try:
            return os.get_terminal_size(fd).columns
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass
    return shutil.get_terminal_size(fallback=(DEFAULT_TERM_WIDTH, 24)).columns


def _status_timeout() -> float:
    from forge.runtime_config import get_runtime_config

    return get_runtime_config().status_timeout


def compact_model_name(model: str) -> str:
    """Strip provider prefix and shorten model names for display.

    Delegates to the model catalog for short_name overrides, with generic
    rules (prefix stripping, -preview removal) for models not in the catalog.
    """
    from forge.core.models import get_compact_name

    return get_compact_name(model)


class ProxyRuntimeTruth:
    """Structured proxy runtime truth from GET / endpoint."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.is_proxy = raw.get("is_proxy", False)

        # Proxy identity (B2.1)
        proxy = raw.get("proxy", {})
        self.proxy_id = proxy.get("proxy_id")
        self.template = proxy.get("template") or raw.get("template", "unknown")
        self.port = proxy.get("port")
        self.base_url = proxy.get("base_url")

        # Runtime truth
        runtime = raw.get("runtime", {})
        self.active_tier = runtime.get("active_tier")
        self.active_context_window = runtime.get("active_context_window")
        self.context_windows = runtime.get("context_windows", {})
        self.tier_mappings = runtime.get("tier_mappings", {})

        # Older proxy response shape (system boundary: proxy HTTP response)
        self.tiers = raw.get("tiers", {})

    def get_context_window_for_tier(self, tier: str) -> int | None:
        """Get context window for a tier, preferring runtime truth."""
        # Prefer runtime.context_windows (authoritative)
        if tier in self.context_windows:
            return self.context_windows[tier]
        # Fallback: older proxy response shape (system boundary)
        tier_info = self.tiers.get(tier, {})
        return tier_info.get("context_window")

    @property
    def proxy_cost_usd(self) -> float:
        """Total estimated proxy cost in USD from metrics snapshot."""
        metrics = self.raw.get("metrics", {})
        costs = metrics.get("costs", {})
        return costs.get("total_usd", 0.0)


def detect_proxy() -> tuple[bool, ProxyRuntimeTruth | None, bool]:
    """Detect if using a proxy and fetch its runtime truth.

    Returns:
        Tuple of (is_proxy, runtime_truth_or_none, is_authoritative).
        - is_authoritative=True means live proxy GET / succeeded
        - is_authoritative=False means we fell back to registry lookup
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not base_url:
        return False, None, False

    # Parse as URL — works for any host, not just localhost (CR-016)
    from urllib.parse import urlparse

    # Normalize scheme-less URLs (e.g., "localhost:8085" → "http://localhost:8085")
    normalized = base_url if "://" in base_url else f"http://{base_url}"
    parsed = urlparse(normalized)
    if not parsed.hostname:
        return False, None, False

    # Try live proxy query first (authoritative)
    # Use scheme://netloc/ to strip any path (proxy serves identity at /)
    try:
        import urllib.request

        query_url = f"{parsed.scheme}://{parsed.netloc}/"
        with urllib.request.urlopen(query_url, timeout=_status_timeout()) as response:
            proxy_info = json.loads(response.read())

        if proxy_info.get("is_proxy") is True:
            return True, ProxyRuntimeTruth(proxy_info), True  # authoritative
    except Exception:
        pass

    # Fallback: reverse lookup from proxy registry (non-authoritative)
    try:
        from forge.proxy.proxies import ProxyRegistryStore

        store = ProxyRegistryStore()
        registry = store.read()

        # Match by port when available, or by full netloc
        target_port = parsed.port
        for proxy_id, entry in registry.proxies.items():
            entry_normalized = entry.base_url if "://" in (entry.base_url or "") else f"http://{entry.base_url or ''}"
            entry_parsed = urlparse(entry_normalized)
            match = (target_port is not None and entry_parsed.port == target_port) or (
                target_port is None and parsed.netloc == entry_parsed.netloc
            )
            if match:
                runtime_dict: dict[str, Any] = {}
                try:
                    from forge.config.loader import load_proxy_instance_config
                    from forge.core.models import get_context_window_tokens

                    proxy_config = load_proxy_instance_config(proxy_id)
                    if proxy_config is not None:
                        tier_models = {
                            t: m
                            for t, m in [
                                ("haiku", proxy_config.tiers.haiku),
                                ("sonnet", proxy_config.tiers.sonnet),
                                ("opus", proxy_config.tiers.opus),
                            ]
                            if m
                        }
                        context_windows: dict[str, int] = {}
                        for tier, model in tier_models.items():
                            try:
                                context_windows[tier] = get_context_window_tokens(model)
                            except Exception:
                                pass
                        active_tier = proxy_config.default_tier or "sonnet"
                        active_cw = context_windows.get(active_tier) or context_windows.get("sonnet")
                        runtime_dict = {
                            "tier_mappings": tier_models,
                            "context_windows": context_windows,
                            "active_tier": active_tier,
                            "active_context_window": active_cw,
                        }
                except Exception:
                    pass

                fallback_info = {
                    "is_proxy": True,
                    "proxy": {
                        "proxy_id": proxy_id,
                        "template": entry.template,
                        "port": entry.port,
                        "base_url": entry.base_url,
                    },
                    "runtime": runtime_dict,
                    "tiers": {},
                }
                return (
                    True,
                    ProxyRuntimeTruth(fallback_info),
                    False,
                )  # non-authoritative
    except Exception:
        pass

    return False, None, False


def _tier_color(tier: str, runtime: ProxyRuntimeTruth | None) -> str:
    """Pick color for a tier, using deep variant for extended context (>200K)."""
    extended = False
    if runtime:
        ctx = runtime.get_context_window_for_tier(tier)
        if ctx and ctx > 200_000:
            extended = True

    if tier == "opus":
        return TIER_OPUS_DEEP if extended else TIER_OPUS
    elif tier == "sonnet":
        return TIER_SONNET_DEEP if extended else TIER_SONNET
    return TIER_HAIKU


def get_tier_display(runtime: ProxyRuntimeTruth | None) -> str | None:
    """Get tier display string showing all mappings.

    Format: "O:model S:model H:model" with per-tier coloring.
    """
    if runtime is None:
        return None

    # Prefer runtime.tier_mappings (authoritative), fallback to legacy tiers
    tier_mappings = runtime.tier_mappings
    if not tier_mappings:
        tier_mappings = {k: v.get("model", "") for k, v in runtime.tiers.items()}

    if not tier_mappings:
        return None

    h_model = tier_mappings.get("haiku", "")
    s_model = tier_mappings.get("sonnet", "")
    o_model = tier_mappings.get("opus", "")

    if not any([h_model, s_model, o_model]):
        return None

    h_name = compact_model_name(h_model)
    s_name = compact_model_name(s_model)
    o_name = compact_model_name(o_model)

    oc = _tier_color("opus", runtime)
    sc = _tier_color("sonnet", runtime)
    hc = _tier_color("haiku", runtime)

    return f"{oc}O:{o_name}{RESET} {sc}S:{s_name}{RESET} {hc}H:{h_name}{RESET}"


# Context window info is sourced from:
# 1. Proxy runtime truth (GET /) when using proxy - authoritative from core.models catalog
# 2. Claude Code's JSON input (context_window field) when not using proxy
# No hardcoded fallback tables - unknown models will show context from Claude Code's input


def get_tier_from_display_name(display_name: str) -> str:
    """Map Claude Code's display name to tier."""
    display_lower = display_name.lower()
    if "opus" in display_lower:
        return "opus"
    elif "sonnet" in display_lower:
        return "sonnet"
    elif "haiku" in display_lower:
        return "haiku"
    return "sonnet"


class TranscriptStats(NamedTuple):
    """Results from single-pass transcript scan."""

    has_thinking: bool = False
    user_count: int = 0
    tool_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


_EMPTY_STATS = TranscriptStats()

# Cache transcript stats by (path, mtime_ns, size) to skip re-scanning unchanged files (CR-017).
_transcript_cache: dict[str, tuple[int, int, TranscriptStats]] = {}


def _cached_scan_transcript(transcript_path: str) -> TranscriptStats:
    """Scan transcript with file-identity caching.

    Returns cached stats if the file's mtime_ns and size haven't changed.
    """
    if not transcript_path:
        return _EMPTY_STATS

    try:
        st = Path(transcript_path).stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _EMPTY_STATS

    cached = _transcript_cache.get(transcript_path)
    if cached is not None and (cached[0], cached[1]) == key:
        return cached[2]

    stats = scan_transcript(transcript_path)
    _transcript_cache[transcript_path] = (key[0], key[1], stats)
    return stats


def _resolve_entry_role(entry: dict[str, Any]) -> str | None:
    """Resolve entry role from either transcript format.

    Old format: top-level "type" field ("user" | "assistant")
    New format: "message.role" field ("user" | "assistant")
    """
    # Old format: entry.type
    entry_type = entry.get("type")
    if entry_type in ("user", "assistant"):
        return entry_type
    # New format: entry.message.role
    return entry.get("message", {}).get("role")


def scan_transcript(transcript_path: str) -> TranscriptStats:
    """Single-pass transcript scan for thinking, counts, and token metrics.

    Supports both transcript formats:
    - Old: top-level "type" field ("user" | "assistant")
    - New: "message.role" field (requestId-based, newer Claude Code)

    Extracts in one pass: thinking indicator, user turn count, tool call count,
    and cumulative token usage (input/output/cached) from message.usage fields.
    """
    if not transcript_path:
        return _EMPTY_STATS

    path = Path(transcript_path)
    if not path.is_file():
        return _EMPTY_STATS

    user_count = 0
    tool_count = 0
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    last_assistant_content: list[Any] | None = None

    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    role = _resolve_entry_role(entry)

                    if role == "user":
                        # In new format, tool_result messages also have role=user;
                        # only count actual human turns (no tool_result content)
                        content = entry.get("message", {}).get("content", [])
                        is_tool_result = isinstance(content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                        )
                        if not is_tool_result:
                            user_count += 1
                    elif role == "assistant":
                        content = entry.get("message", {}).get("content", [])
                        last_assistant_content = content
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_count += 1

                    # Accumulate token usage from any entry with message.usage
                    usage = entry.get("message", {}).get("usage")
                    if usage:
                        input_tokens += usage.get("input_tokens", 0)
                        output_tokens += usage.get("output_tokens", 0)
                        cached_tokens += usage.get("cache_read_input_tokens", 0)
                        cached_tokens += usage.get("cache_creation_input_tokens", 0)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return _EMPTY_STATS

    has_thinking = False
    if last_assistant_content:
        for block in last_assistant_content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                has_thinking = True
                break

    return TranscriptStats(
        has_thinking=has_thinking,
        user_count=user_count,
        tool_count=tool_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
    )


def parse_context_from_json(data: dict[str, Any]) -> dict[str, Any] | None:
    """Parse context usage from Claude Code's JSON input.

    Uses the official context_window field from Claude Code's status line contract.

    Expected format:
        context_window:
            context_window_size: 200000
            current_usage:
                input_tokens: 8500
                cache_creation_input_tokens: 5000
                cache_read_input_tokens: 2000
    """
    context_window_data = data.get("context_window")
    if not context_window_data:
        return None

    # Claude Code sends context_window as int (just the size) or dict (size + usage).
    # When it's an int there's no usage breakdown to display.
    if isinstance(context_window_data, (int, float)):
        return None

    context_window_size = context_window_data.get("context_window_size", 0)
    if not context_window_size or context_window_size <= 0:
        return None

    current_usage = context_window_data.get("current_usage") or {}

    # Calculate current context from current_usage fields
    input_tokens = current_usage.get("input_tokens", 0)
    cache_creation = current_usage.get("cache_creation_input_tokens", 0)
    cache_read = current_usage.get("cache_read_input_tokens", 0)
    total_tokens = input_tokens + cache_creation + cache_read

    used_percentage = context_window_data.get("used_percentage")
    if used_percentage is None and total_tokens <= 0:
        return None

    if used_percentage is not None:
        percent_used = min(100, int(used_percentage))
        # Back-compute tokens from percentage so proxy override path stays consistent
        if total_tokens <= 0:
            total_tokens = int(context_window_size * used_percentage / 100)
    else:
        percent_used = min(100, int((total_tokens / context_window_size) * 100))

    return {
        "percent": percent_used,
        "tokens": total_tokens,
        "context_window": context_window_size,
    }


def get_effective_context_window(
    data: dict[str, Any], runtime: ProxyRuntimeTruth | None, context_info: dict[str, Any] | None
) -> int | None:
    """Resolve the best-known context window size for display."""
    if runtime and runtime.active_context_window:
        return runtime.active_context_window

    if context_info:
        context_window = context_info.get("context_window", 0)
        if context_window > 0:
            return context_window

    context_window_data = data.get("context_window")
    if isinstance(context_window_data, dict):
        context_window_size = context_window_data.get("context_window_size", 0)
        if context_window_size > 0:
            return context_window_size
    if isinstance(context_window_data, (int, float)) and context_window_data > 0:
        return int(context_window_data)

    return None


def format_model_label(display_name: str, context_window: int | None) -> str:
    """Clean Claude's display name and append non-default context size when useful."""
    base_name = re.sub(r"\s*\([^)]*context[^)]*\)", "", display_name).strip()
    if context_window and context_window > 200_000:
        return f"{base_name} ({format_context_size(context_window)})"
    return base_name


def format_context_size(size: int) -> str:
    """Format context window size for display (e.g., 2097152 -> "2M")."""
    if size >= 1_000_000:
        millions = size // 1_000_000
        remainder = (size % 1_000_000) // 100_000
        if remainder > 0:
            return f"{millions}.{remainder}M"
        return f"{millions}M"
    elif size >= 1000:
        return f"{size // 1000}K"
    return str(size)


def get_context_display(context_info: dict[str, Any] | None) -> str:
    """Generate context display with progress bar."""
    if not context_info:
        return f"{DARK_GRAY}---{RESET}"

    percent = context_info.get("percent", 0)
    warning = context_info.get("warning", "")
    context_window = context_info.get("context_window", 0)

    # 5-step gradient with wider bands at extremes (2/7, 1/7, 1/7, 1/7, 2/7).
    # Auto-compact fires around 80% so the warning zone starts early at 57%.
    if percent >= 72:
        color = CTX_CRIT
        alert = "!"
    elif percent >= 57:
        color = CTX_WARN
        alert = ""
    elif percent >= 43:
        color = CTX_HIGH
        alert = ""
    elif percent >= 29:
        color = CTX_MED
        alert = ""
    else:
        color = CTX_LOW
        alert = ""

    segments = 8
    filled = percent * segments // 100
    empty = segments - filled
    bar = PROGRESS_FILLED * filled + PROGRESS_EMPTY * empty

    # Warning overrides
    if warning == "auto-compact":
        alert = "AC"
    elif warning == "low":
        alert = "!"

    if context_window > 0:
        size_str = format_context_size(context_window)
        alert_str = f" {alert}" if alert else ""
        return f"{color}{bar} {percent}%/{BOLD}{size_str}{alert_str}{RESET}"
    else:
        alert_str = f" {alert}" if alert else ""
        return f"{color}{bar} {percent}%{BOLD}{alert_str}{RESET}"


def get_session_metrics(
    cost_data: dict[str, Any],
    is_proxy: bool,
    proxy_cost_usd: float = 0.0,
) -> str | None:
    """Get session metrics (cost, duration). Returns bare string or None."""
    if not cost_data and proxy_cost_usd <= 0:
        return None

    metrics: list[str] = []

    if is_proxy and proxy_cost_usd > 0:
        cost_color = METRICS_COLOR
        if proxy_cost_usd < 0.01:
            cost_str = f"~{int(proxy_cost_usd * 10000) / 100}c"
        else:
            cost_str = f"~${proxy_cost_usd:.2f}"
        metrics.append(f"{cost_color}{cost_str}{RESET}")
    elif not is_proxy:
        cost_usd = (cost_data or {}).get("total_cost_usd", 0)
        if cost_usd > 0:
            cost_color = METRICS_COLOR

            if cost_usd < 0.01:
                cost_str = f"{int(cost_usd * 100)}c"
            else:
                cost_str = f"${cost_usd:.2f}"

            metrics.append(f"{cost_color}{cost_str}{RESET}")

    # Duration
    duration_ms = cost_data.get("total_duration_ms", 0)
    if duration_ms > 0:
        minutes = duration_ms // 60000

        if minutes >= 30:
            duration_color = YELLOW
        else:
            duration_color = METRICS_COLOR

        if duration_ms < 60000:
            duration_str = f"{duration_ms // 1000}s"
        else:
            duration_str = f"{minutes}m"

        metrics.append(f"{duration_color}{duration_str}{RESET}")

    return " ".join(metrics) if metrics else None


def get_git_branch(current_dir: str) -> str | None:
    """Get git branch name for directory."""
    if not current_dir:
        return None

    try:
        # Try symbolic-ref first (for normal branches)
        timeout = _status_timeout()
        result = subprocess.run(
            ["git", "-C", current_dir, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()

        # Fall back to rev-parse for detached HEAD
        result = subprocess.run(
            ["git", "-C", current_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return None


def get_compact_path(current_dir: str) -> str:
    """Create compact path: project/.../dir."""
    if not current_dir:
        return ""

    home = str(Path.home())
    workspace_path = os.path.join(home, "workspace")

    if current_dir.startswith(workspace_path + "/"):
        rel_path = current_dir[len(workspace_path) + 1 :]
        parts = rel_path.split("/")
        num_parts = len(parts)

        if num_parts == 1:
            return parts[0]
        elif num_parts == 2:
            return f"{parts[0]}/{parts[-1]}"
        else:
            return f"{parts[0]}/.../{parts[-1]}"
    else:
        # Outside workspace, use ~ substitution
        if current_dir.startswith(home):
            return "~" + current_dir[len(home) :]
        return current_dir


# --- Formatting helpers ---

# Breadcrumb separator
BREADCRUMB_SEP = " > "
BREADCRUMB_ELISION = "..."

# Terminal states where verification loop has ended (no indicator needed).
# "error" is intentionally excluded — a broken verifier is actionable info.
_VERIFICATION_TERMINAL = {
    "passed",
    "max_iterations",
    "max_minutes",
    "bypassed",
    "warned",
}

# ANSI escape sequence regex for stripping/preserving color codes
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _char_width(c: str) -> int:
    """Return terminal display width of a single character.

    Handles emoji (2 cols), variation selectors and combining marks (0 cols),
    and East Asian wide/fullwidth characters (2 cols).
    """
    cp = ord(c)
    # Zero-width: variation selectors, ZWJ, ZWNJ
    if cp in (0xFE0E, 0xFE0F, 0x200D, 0x200C):
        return 0
    cat = unicodedata.category(c)
    if cat.startswith("M"):  # Combining marks
        return 0
    # Supplementary characters (most emoji live here)
    if cp >= 0x10000:
        return 2
    eaw = unicodedata.east_asian_width(c)
    if eaw in ("W", "F"):
        return 2
    return 1


def _visible_width(text: str) -> int:
    """Return terminal display width of text, stripping ANSI and counting Unicode correctly.

    Key difference from len(): emoji like 🧠 count as 2 columns,
    and variation selectors (U+FE0F) after BMP characters add 1 extra column
    (BMP char goes from 1-col text to 2-col emoji presentation).
    """
    stripped = _ANSI_RE.sub("", text)
    width = 0
    prev_cp = 0
    for c in stripped:
        cp = ord(c)
        # VS16 after a narrow BMP char → upgrade previous char to emoji width
        if cp == 0xFE0F and 0 < prev_cp < 0x10000:
            eaw = unicodedata.east_asian_width(chr(prev_cp))
            if eaw not in ("W", "F"):
                width += 1  # was counted as 1, should be 2
            prev_cp = cp
            continue
        w = _char_width(c)
        width += w
        if w > 0:
            prev_cp = cp
    return width


def format_tokens(count: int) -> str:
    """Format token count compactly: 1.2M / 12.5K / 42."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


def format_breadcrumb(manifest: dict[str, Any], is_authoritative: bool) -> str | None:
    """Format session lineage as breadcrumb: origin > ... > parent > current.

    Rules (max 3 crumbs):
    - No lineage → session_name
    - 1 ancestor -> parent > current
    - 2 ancestors -> origin > parent > current
    - 3+ ancestors -> origin > ... > parent > current

    lineage field is [parent, grandparent, ...] (nearest first).
    """
    session_name = manifest.get("name", "")
    if not session_name:
        return None

    derivation = manifest.get("confirmed", {}).get("derivation") or {}
    lineage: list[str] = derivation.get("lineage", [])
    suffix = "" if is_authoritative else "(~)"

    if not lineage:
        return f"{session_name}{suffix}"

    # Reverse: [parent, grandparent, origin] → [origin, grandparent, parent]
    ancestors = list(reversed(lineage))

    if len(ancestors) == 1:
        breadcrumb = f"{ancestors[0]}{BREADCRUMB_SEP}{session_name}"
    elif len(ancestors) == 2:
        breadcrumb = BREADCRUMB_SEP.join(ancestors) + f"{BREADCRUMB_SEP}{session_name}"
    else:
        # 3+ ancestors: origin > ... > parent > current
        breadcrumb = (
            f"{ancestors[0]}{BREADCRUMB_SEP}{BREADCRUMB_ELISION}{BREADCRUMB_SEP}"
            f"{ancestors[-1]}{BREADCRUMB_SEP}{session_name}"
        )

    return f"{breadcrumb}{suffix}"


def format_verification(manifest: dict[str, Any]) -> str | None:
    """Format verification status: LOOP N/M when active, None otherwise."""
    confirmed_verif = manifest.get("confirmed", {}).get("verification") or {}
    iterations = confirmed_verif.get("iterations", 0)
    if iterations == 0:
        return None

    last_result = confirmed_verif.get("last_result")
    if last_result in _VERIFICATION_TERMINAL:
        return None

    max_iterations = manifest.get("intent", {}).get("verification", {}).get("max_iterations", 50)
    return f"{VERIFICATION_INDICATOR} {iterations}/{max_iterations}"


def format_sidecar(manifest: dict[str, Any]) -> str | None:
    """Return ASCII indicator when session uses sidecar mode."""
    if manifest.get("confirmed", {}).get("is_sandboxed", False):
        return SIDECAR_INDICATOR
    return None


def format_native_sandbox() -> str | None:
    """Return indicator if Claude Code native sandbox is active.

    TODO: Claude Code does not currently expose a discoverable
    env var for sandbox state (Seatbelt/bubblewrap). Wire this in when
    the detection mechanism is confirmed. Candidates: CLAUDE_SANDBOX,
    CLAUDE_CODE_SANDBOX_MODE, or presence of sandbox-runtime process.
    """
    return None


def format_rate_limits(rate_limits: Any, is_proxy: bool) -> str | None:
    """Format rate limit usage from Claude Code's rate_limits field.

    Only shows the shortest window (5h) since that's the one users hit.
    Skips display in proxy mode (proxy has its own rate limits).

    Color thresholds: green < 50%, yellow 50-80%, red > 80%.
    """
    if is_proxy or not rate_limits:
        return None

    # rate_limits is a list of window objects
    if not isinstance(rate_limits, list):
        logger.debug("rate_limits unexpected type: %s", type(rate_limits).__name__)
        return None

    # Find the shortest window (5h preferred)
    window = None
    for entry in rate_limits:
        if not isinstance(entry, dict):
            continue
        window_type = entry.get("type", "")
        if "5" in str(window_type) or "hour" in str(window_type).lower():
            window = entry
            break
    # Fall back to first entry if no 5h window found
    if window is None and rate_limits:
        first = rate_limits[0]
        if isinstance(first, dict):
            window = first

    if window is None:
        return None

    used_pct = window.get("used_percentage")
    if used_pct is None:
        return None

    try:
        used_pct_float = float(used_pct)
    except (TypeError, ValueError):
        logger.debug("rate_limits used_percentage unexpected value: %r", used_pct)
        return None

    pct = int(used_pct_float)
    if used_pct_float > 80:
        color = RED_BOLD
    elif used_pct_float >= 50:
        color = YELLOW
    else:
        color = GREEN

    return f"{DIM}RL:{RESET}{color}{pct}%{RESET}"


def format_token_breakdown(input_tokens: int, output_tokens: int, cached_tokens: int) -> str | None:
    """Format cumulative token breakdown: in:12K out:3.2K cache:8K."""
    if input_tokens == 0 and output_tokens == 0 and cached_tokens == 0:
        return None
    parts: list[str] = []
    if input_tokens > 0:
        parts.append(f"{DIM}{TOKEN_INPUT_LABEL}{RESET}{METRICS_COLOR}{format_tokens(input_tokens)}{RESET}")
    if output_tokens > 0:
        parts.append(f"{DIM}{TOKEN_OUTPUT_LABEL}{RESET}{METRICS_COLOR}{format_tokens(output_tokens)}{RESET}")
    if cached_tokens > 0:
        parts.append(f"{DIM}{TOKEN_CACHE_LABEL}{RESET}{METRICS_COLOR}{format_tokens(cached_tokens)}{RESET}")
    return " ".join(parts) if parts else None


def _parse_numstat(output: str) -> tuple[int, int]:
    """Parse `git diff --numstat` output into (added, removed) totals."""
    added = 0
    removed = 0

    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        add_str, remove_str = parts[0], parts[1]
        if add_str.isdigit():
            added += int(add_str)
        if remove_str.isdigit():
            removed += int(remove_str)

    return added, removed


# Cache git numstat results with a short TTL to avoid two subprocess calls per refresh
_numstat_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_NUMSTAT_TTL_SECS = 5.0


def _git_numstat(current_dir: str) -> tuple[int, int]:
    """Run git diff --numstat (staged + unstaged) with TTL cache."""
    now = time.monotonic()
    cached = _numstat_cache.get(current_dir)
    if cached is not None and (now - cached[0]) < _NUMSTAT_TTL_SECS:
        return cached[1]

    try:
        timeout = _status_timeout()
        unstaged = subprocess.run(
            ["git", "-C", current_dir, "diff", "--numstat"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        staged = subprocess.run(
            ["git", "-C", current_dir, "diff", "--cached", "--numstat"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if unstaged.returncode != 0 or staged.returncode != 0:
            result = (0, 0)
        else:
            unstaged_added, unstaged_removed = _parse_numstat(unstaged.stdout)
            staged_added, staged_removed = _parse_numstat(staged.stdout)
            result = (unstaged_added + staged_added, unstaged_removed + staged_removed)
    except Exception:
        result = (0, 0)

    _numstat_cache[current_dir] = (now, result)
    return result


def get_line_change_values(cost_data: dict[str, Any], current_dir: str = "") -> tuple[int, int]:
    """Prefer Claude totals, then fall back to cached git diff counts."""
    if cost_data:
        lines_added = int(cost_data.get("total_lines_added", 0) or 0)
        lines_removed = int(cost_data.get("total_lines_removed", 0) or 0)
        if lines_added > 0 or lines_removed > 0:
            return lines_added, lines_removed

    if not current_dir:
        return 0, 0

    return _git_numstat(current_dir)


def format_line_changes(cost_data: dict[str, Any], current_dir: str = "") -> str | None:
    """Format direct line counts as +added/-removed with conventional colors."""
    lines_added, lines_removed = get_line_change_values(cost_data, current_dir)
    if lines_added == 0 and lines_removed == 0:
        return None

    parts: list[str] = []
    if lines_added > 0:
        parts.append(f"{LINE_ADD_COLOR}+{lines_added}{RESET}")
    if lines_removed > 0:
        parts.append(f"{LINE_REMOVE_COLOR}-{lines_removed}{RESET}")

    return f"{DARK_GRAY}/{RESET}".join(parts) if len(parts) == 2 else parts[0]


def get_token_breakdown_values(data: dict[str, Any], stats: TranscriptStats) -> tuple[int, int, int]:
    """Prefer token totals from Claude Code input, with transcript fallback."""
    context_window_data = data.get("context_window")
    if not isinstance(context_window_data, dict):
        return stats.input_tokens, stats.output_tokens, stats.cached_tokens

    input_tokens = context_window_data.get("total_input_tokens")
    output_tokens = context_window_data.get("total_output_tokens")

    # Prefer aggregate key; fall back to sum of breakdown keys to avoid double-counting
    total_cached = context_window_data.get("total_cached_tokens")
    if total_cached is not None:
        cached_tokens: int | None = int(total_cached)
    else:
        read = context_window_data.get("total_cache_read_input_tokens")
        creation = context_window_data.get("total_cache_creation_input_tokens")
        if read is not None or creation is not None:
            cached_tokens = int(read or 0) + int(creation or 0)
        else:
            cached_tokens = None

    return (
        int(input_tokens) if input_tokens is not None else stats.input_tokens,
        int(output_tokens) if output_tokens is not None else stats.output_tokens,
        cached_tokens if cached_tokens is not None else stats.cached_tokens,
    )


def truncate_ansi(text: str, max_width: int) -> str:
    """Truncate text to max_width visible columns, preserving ANSI codes.

    Uses _char_width() for correct emoji/Unicode column counting.
    Appends '...' when limit reached.
    """
    if max_width <= 3:
        return "..."

    visible_len = 0
    result: list[str] = []
    in_ansi = False
    prev_cp = 0

    for char in text:
        if char == "\033":
            in_ansi = True
            result.append(char)
        elif in_ansi:
            result.append(char)
            if char == "m":
                in_ansi = False
        else:
            cp = ord(char)
            # VS16 after BMP char upgrades it to emoji width
            if cp == 0xFE0F and 0 < prev_cp < 0x10000:
                eaw = unicodedata.east_asian_width(chr(prev_cp))
                if eaw not in ("W", "F"):
                    visible_len += 1
                result.append(char)
                prev_cp = cp
                continue

            w = _char_width(char)
            if visible_len + w <= max_width - 3:
                result.append(char)
                visible_len += w
                if w > 0:
                    prev_cp = cp
            else:
                result.append("...")
                break
    else:
        return text

    return "".join(result)


def _wrap_output(output: str, available: int) -> str:
    """Wrap at a separator boundary instead of truncating with '...'.

    Splits at the last | separator that fits within `available` visible columns.
    Line 2 gets an ANSI reset prefix. Falls back to truncate_ansi() when
    there are no separators or the first segment alone exceeds the width.
    """
    segments = output.split(_HARDENED_SEP)
    if len(segments) <= 1:
        return truncate_ansi(output, available)

    sep_visible_width = _visible_width(_HARDENED_SEP)

    line1_parts = [segments[0]]
    line1_visible = _visible_width(segments[0])
    split_idx = 1

    for i in range(1, len(segments)):
        seg_visible = _visible_width(segments[i])
        new_width = line1_visible + sep_visible_width + seg_visible
        if new_width <= available:
            line1_parts.append(segments[i])
            line1_visible = new_width
            split_idx = i + 1
        else:
            break

    if split_idx >= len(segments):
        return output

    if not line1_parts or line1_visible == 0:
        return truncate_ansi(output, available)

    line1 = _HARDENED_SEP.join(line1_parts)
    remaining = segments[split_idx:]
    line2 = "\x1b[0m" + _HARDENED_SEP.join(remaining)

    line2_visible = _visible_width(line2)
    if line2_visible > available:
        line2 = truncate_ansi(line2, available)

    return line1 + "\n" + line2


def render_categories(
    where: list[str],
    who: list[str],
    what: list[str],
    metrics: list[str],
    state: list[str],
) -> str:
    """Join category segments into final status line string.

    Where parts are concatenated directly (path + branch).
    All other segments are flattened with SEP between each — no visual
    distinction between within-category and between-category separators.
    """
    parts: list[str] = []

    if where:
        parts.append("".join(where))

    for category in (who, what, metrics, state):
        for segment in category:
            parts.append(f" {SEP} {segment}")

    return "".join(parts)


def discover_session() -> tuple[dict[str, Any] | None, bool]:
    """Discover session state via FORGE_SESSION env var only.

    No CWD fallback: if FORGE_SESSION is not set, returns (None, False).
    This prevents false positives when running native ``claude`` in a
    directory that happens to have Forge sessions.

    Returns:
        Tuple of (manifest_dict, is_authoritative).
        - is_authoritative=True means FORGE_SESSION env var + index lookup succeeded
        - (None, False) means no Forge session context
    """
    session_name = os.environ.get("FORGE_SESSION")
    if not session_name:
        return None, False

    forge_root = os.environ.get("FORGE_FORGE_ROOT")

    try:
        # Lazy import to avoid slowing down status line startup
        from forge.session.index import IndexStore
        from forge.session.store import get_manifest_path

        index = IndexStore()
        entry = index.get_session(session_name, forge_root=forge_root)
        if entry:
            manifest_path = get_manifest_path(entry.forge_root or entry.worktree_path, session_name)
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                return manifest, True  # authoritative
    except Exception as e:
        logger.debug(f"Index lookup failed for FORGE_SESSION={session_name}: {e}")

    return None, False


@click.command(name="status-line", hidden=True)
def status_line() -> None:
    """Generate status line for Claude Code.

    Reads JSON from stdin (Claude Code's status line contract),
    outputs formatted status line to stdout.

    This command is invoked by Claude Code's statusLine setting.

    Exempt from automatic debug logging (runs every poll cycle).
    Enable via FORGE_DEBUG=1 or config.yaml log_level: debug.
    Logs to $FORGE_HOME/logs/cli/status-line.<PID>.log.
    """
    # Status-line configures its own logging (exempt from main.py auto-config,
    # same pattern as hooks/_group.py).
    from forge.core.logging import configure_debug_logging

    configure_debug_logging(component="status-line", subdirectory="cli")

    try:
        json_data = sys.stdin.read()
        if not json_data.strip():
            click.echo(f"{RED}[Error: No input]{RESET}", color=True)
            return

        data = json.loads(json_data)
    except json.JSONDecodeError:
        click.echo(f"{RED}[Error: Invalid JSON]{RESET}", color=True)
        return

    logger.debug("env: FORGE_HOME=%s", os.environ.get("FORGE_HOME", "<unset>"))
    logger.debug("env: ANTHROPIC_BASE_URL=%s", os.environ.get("ANTHROPIC_BASE_URL", "<unset>"))
    logger.debug("env: FORGE_SESSION=%s", os.environ.get("FORGE_SESSION", "<unset>"))
    logger.debug("input keys: %s", list(data.keys()))
    logger.debug("workspace.current_dir: %s", data.get("workspace", {}).get("current_dir", "<missing>"))

    is_proxy, runtime, is_proxy_authoritative = detect_proxy()

    logger.debug("proxy: is_proxy=%s, authoritative=%s", is_proxy, is_proxy_authoritative)
    if runtime:
        logger.debug("proxy: template=%s, tier_mappings=%s", runtime.template, runtime.tier_mappings)
    else:
        logger.debug("proxy: runtime=None")

    workspace = data.get("workspace", {})
    current_dir = workspace.get("current_dir", "")
    model_data = data.get("model", {})
    raw_model_name = model_data.get("display_name", "Claude")
    transcript_path = data.get("transcript_path", "")
    cost_data = data.get("cost", {})

    # Discover session early (needed for Who + State categories)
    session_manifest, is_session_authoritative = discover_session()

    session_name = session_manifest.get("name") if session_manifest else None
    logger.debug("session: name=%s, authoritative=%s", session_name, is_session_authoritative)

    # === CATEGORY: Where ===
    where: list[str] = []
    where.append(f"{GREEN_BOLD}{get_compact_path(current_dir)}{RESET}")
    git_branch = get_git_branch(current_dir)
    if git_branch:
        where.append(f" ({YELLOW_BOLD}{git_branch}{RESET})")

    # === CATEGORY: Who ===
    who: list[str] = []
    if session_manifest:
        breadcrumb = format_breadcrumb(session_manifest, is_session_authoritative)
        if breadcrumb:
            who.append(f"{BREADCRUMB_COLOR}{breadcrumb}{RESET}")

    # === CATEGORY: What ===
    what: list[str] = []

    # Context info (may be overridden by proxy runtime truth)
    logger.debug(
        "context_window raw: %s (type=%s)", data.get("context_window"), type(data.get("context_window")).__name__
    )
    context_info = parse_context_from_json(data)
    if is_proxy and runtime and runtime.active_context_window:
        if context_info:
            tokens = context_info.get("tokens", 0)
            accurate_window = runtime.active_context_window
            context_info["context_window"] = accurate_window
            context_info["percent"] = min(100, int((tokens / accurate_window) * 100))

    effective_context_window = get_effective_context_window(data, runtime, context_info)
    model_name = format_model_label(raw_model_name, effective_context_window)

    tier_display = get_tier_display(runtime) if is_proxy else None
    if tier_display:
        model_segment = f"[{tier_display}] {get_context_display(context_info)}"
    else:
        detected_tier = get_tier_from_display_name(raw_model_name)
        model_color = _tier_color(detected_tier, runtime)
        model_segment = f"{model_color}[{model_name}]{RESET} {get_context_display(context_info)}"

    if is_proxy and runtime and runtime.template and runtime.template != "unknown":
        suffix = "" if is_proxy_authoritative else "(~)"
        what.append(f"{TEMPLATE_COLOR}{runtime.template}{suffix}{RESET} {model_segment}")
    else:
        what.append(model_segment)

    # === CATEGORY: Metrics ===
    metrics_cat: list[str] = []

    _proxy_cost = runtime.proxy_cost_usd if runtime else 0.0
    session_metrics = get_session_metrics(cost_data, is_proxy, proxy_cost_usd=_proxy_cost)
    if session_metrics:
        metrics_cat.append(session_metrics)

    # Rate limit usage (direct Anthropic sessions only, config-gated)
    from forge.runtime_config import get_runtime_config

    if get_runtime_config().show_rate_limits:
        rate_limits_data = data.get("rate_limits")
        logger.debug("rate_limits: %s", rate_limits_data)
        rate_limit_display = format_rate_limits(rate_limits_data, is_proxy)
        if rate_limit_display:
            metrics_cat.append(rate_limit_display)

    # Transcript stats (mtime-cached to avoid re-scanning unchanged files)
    stats = _cached_scan_transcript(transcript_path)

    line_display = format_line_changes(cost_data, current_dir)
    if line_display:
        metrics_cat.append(line_display)

    input_tokens, output_tokens, cached_tokens = get_token_breakdown_values(data, stats)
    token_display = format_token_breakdown(input_tokens, output_tokens, cached_tokens)
    if token_display:
        metrics_cat.append(token_display)

    # === CATEGORY: State ===
    state: list[str] = []

    if stats.has_thinking:
        state.append(f"{BLUE}{THINKING_INDICATOR}{RESET}")

    if session_manifest:
        verif = format_verification(session_manifest)
        if verif:
            state.append(verif)

        sidecar = format_sidecar(session_manifest)
        if sidecar:
            state.append(sidecar)

    # === RENDER ===
    output = render_categories(where, who, what, metrics_cat, state)

    # Output hardening (from ccstatusline)
    # ANSI reset prefix: override Claude Code's dim default styling
    output = "\x1b[0m" + output
    # Non-breaking spaces: prevent VSCode terminal from trimming
    output = output.replace(" ", "\u00a0")

    # Wrap or truncate to prevent terminal line wrapping (which causes Forge output
    # to overlap Claude Code's native status on the next terminal row). Prefers
    # wrapping at a | separator boundary (preserves all info on two lines) over
    # truncation with '...' (loses info). Always on by default; set
    # FORGE_STATUS_TRUNCATE=0 to disable.
    if os.environ.get("FORGE_STATUS_TRUNCATE") != "0":
        term_width = _get_terminal_width()
        available = term_width - TRAILING_MARGIN - NATIVE_DISPLAY_RESERVE
        if available > 3:
            display_width = _visible_width(output)
            if display_width + TRAILING_MARGIN + NATIVE_DISPLAY_RESERVE > term_width:
                output = _wrap_output(output, available)

    # Trailing margin on each line: RESET prevents color bleed, NBSP padding
    # prevents visual merging with Claude Code's native token display.
    margin = RESET + "\u00a0" * TRAILING_MARGIN
    output = "\n".join(line + margin for line in output.split("\n"))

    logger.debug(
        "output line_count=%d, visible_width=%d, term_width=%d",
        output.count("\n") + 1,
        _visible_width(output.split("\n")[0]),
        _get_terminal_width(),
    )

    # Force color=True since Claude Code pipes output (not a TTY)
    click.echo(output, color=True)
