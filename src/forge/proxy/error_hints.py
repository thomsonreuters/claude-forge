"""Error hint enrichment for client-side tool failures.

Appends targeted hints to tool_result error content before forwarding
to the LLM, helping non-Claude models recover from common mistakes.
"""

from typing import Optional

# Sentinel prefix to prevent double-appending hints
_HINT_PREFIX = "\n\nHINT: "

# Each rule: (tool_name_or_None, list_of_required_substrings, hint_text)
# tool_name=None means match any tool. First matching rule wins.
_HINT_RULES: list[tuple[Optional[str], list[str], str]] = [
    # Edit: no-op (old_string == new_string) -- 57% of all failures
    (
        "Edit",
        ["old_string and new_string are exactly the same"],
        "Edit requires old_string \u2260 new_string. To view code, use Read instead of Edit.",
    ),
    # Edit: not unique match
    (
        "Edit",
        ["matches", "replace_all is false"],
        "Include more surrounding context in old_string to uniquely identify the target, or set replace_all=true.",
    ),
    # Bash: ruff F401 unused import
    (
        "Bash",
        ["F401", "imported but unused"],
        "Remove the unused import(s) listed above, then retry.",
    ),
    # Bash: ruff F811 redefinition of unused name
    (
        "Bash",
        ["F811", "redefinition of unused"],
        "Remove the duplicate definition listed above, then retry.",
    ),
    # TaskOutput: hallucinated task ID
    (
        "TaskOutput",
        ["No task found with ID"],
        (
            "Task IDs are short hex strings returned by run_in_background. "
            "Do not append file extensions. If not found, stop retrying the same ID."
        ),
    ),
    # Read: invalid pages parameter (non-PDF files)
    (
        "Read",
        ["Invalid pages parameter"],
        "pages is only for PDF files. For non-PDF files, omit pages entirely. Retry with only file_path.",
    ),
    # Read: file not found
    (
        "Read",
        ["File does not exist"],
        "Verify the absolute file path is correct. Use Glob to search for the file.",
    ),
    # --- Fallback rules (tool_name=None) for when _find_tool_name() fails ---
    (
        None,
        ["old_string and new_string are exactly the same"],
        "Edit requires old_string \u2260 new_string. To view code, use Read instead of Edit.",
    ),
    (
        None,
        ["No task found with ID"],
        (
            "Task IDs are short hex strings returned by run_in_background. "
            "Do not append file extensions. If not found, stop retrying the same ID."
        ),
    ),
]


def enrich_error_content(tool_name: Optional[str], error_content: str) -> str:
    """Append a HINT to error content if a known failure pattern matches.

    First matching rule wins. Returns original content unchanged if no match.
    """
    if _HINT_PREFIX in error_content:
        return error_content

    for rule_tool, required_substrings, hint_text in _HINT_RULES:
        if rule_tool is not None and tool_name != rule_tool:
            continue

        if all(substr in error_content for substr in required_substrings):
            return error_content + _HINT_PREFIX + hint_text

    return error_content
