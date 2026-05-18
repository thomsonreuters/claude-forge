#!/usr/bin/env -S uv run --group provider-check python
"""Count tokens in documents.

Calls the provider's free token-counting API when available, falls back to
local tiktoken estimation.

Provider detection from --model:
    claude-*         -> Anthropic count_tokens (free, needs ANTHROPIC_API_KEY)
    gemini-*         -> Gemini count_tokens (free, needs GEMINI_API_KEY)
    gpt-*, o1-*, ... -> tiktoken (local, no key needed)
    (unknown)        -> tiktoken cl100k_base fallback

Usage:
    count-tokens docs/design.md                        # default: claude-opus-4-6
    count-tokens --model gemini-2.5-flash file.md      # Gemini API
    count-tokens --model gpt-4 file.md                 # tiktoken (local)
    cat file.txt | count-tokens                        # stdin
    count-tokens -q file.txt                           # quiet: number only
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

DEFAULT_MODEL = "claude-opus-4-6"


def _detect_provider(model: str) -> str:
    """Detect provider from model name prefix."""
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith(("gpt-", "o1-", "o3-", "o4-", "chatgpt-")):
        return "openai"
    return "unknown"


def _count_anthropic(text: str, model: str) -> int | None:
    """Count tokens via Anthropic API (free endpoint). Returns None on failure."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        result = client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        return result.input_tokens
    except Exception:
        return None


def _count_gemini(text: str, model: str) -> int | None:
    """Count tokens via Gemini API (free endpoint). Returns None on failure."""
    try:
        from google import genai
    except ImportError:
        return None
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        client = genai.Client(api_key=key)
        result = client.models.count_tokens(model=model, contents=text)
        return result.total_tokens
    except Exception:
        return None


def _count_tiktoken(text: str, model: str | None = None) -> int:
    """Count tokens locally with tiktoken."""
    import tiktoken

    if model:
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
    else:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def count_tokens(text: str, model: str) -> tuple[int, str]:
    """Count tokens using the best available method for the model.

    Returns (count, method_description). Falls back to tiktoken if
    the provider API is unavailable.
    """
    provider = _detect_provider(model)

    if provider == "anthropic":
        result = _count_anthropic(text, model)
        if result is not None:
            return result, f"anthropic API ({model})"
    elif provider == "gemini":
        result = _count_gemini(text, model)
        if result is not None:
            return result, f"gemini API ({model})"
    elif provider == "openai":
        return _count_tiktoken(text, model), f"tiktoken ({model})"

    return _count_tiktoken(text), "tiktoken (cl100k_base fallback)"


def _read_input(files: list[str]) -> tuple[str, str]:
    """Read input from files or stdin. Returns (text, source_description)."""
    if not files or files == ["-"]:
        return sys.stdin.read(), "stdin"

    all_text = []
    sources = []
    for file_path in files:
        path = Path(file_path)
        if not path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        all_text.append(path.read_text())
        sources.append(path.name)

    return "\n".join(all_text), ", ".join(sources)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate token count for documents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "files",
        nargs="*",
        default=["-"],
        help="Files to count (default: stdin)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=DEFAULT_MODEL,
        help=f"Model for token counting (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Output only the number",
    )

    args = parser.parse_args()
    text, _ = _read_input(args.files)

    if not text.strip():
        if args.quiet:
            print("0")
        else:
            print("0 tokens (empty input)")
        return

    token_count, method = count_tokens(text, args.model)

    if args.quiet:
        print(token_count)
    else:
        chars = len(text)
        lines = text.count("\n") + 1
        print(f"{token_count:,} tokens | {chars:,} chars | {lines:,} lines")
        print(f"  method: {method}")


if __name__ == "__main__":
    main()
