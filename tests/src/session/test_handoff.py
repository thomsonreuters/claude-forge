"""Tests for forge.session.handoff module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from forge.core.transcript import parse_jsonl_transcript, truncate
from forge.session.handoff import (
    MAX_TRANSCRIPT_CHARS,
    ResumeStrategy,
    _format_transcript_for_llm,
    _generate_ai_curated_context,
    _generate_minimal_context,
    _generate_structured_context,
    _resolve_plan_content,
    estimate_transcript_tokens,
    process_handoff,
    resolve_lineage,
)
from forge.session.models import SessionState

# -----------------------------------------------------------------------------
# Test fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def sample_transcript(tmp_path: Path) -> Path:
    """Create a sample transcript JSONL file."""
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps(
            {
                "requestId": "r1",
                "timestamp": "2025-01-15T10:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello, please help me."}],
                },
            }
        ),
        json.dumps(
            {
                "requestId": "r1",
                "timestamp": "2025-01-15T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll help you with that."},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "/path/to/file.py"},
                        },
                    ],
                },
            }
        ),
        json.dumps(
            {
                "requestId": "r1",
                "timestamp": "2025-01-15T10:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "file contents here",
                        },
                    ],
                },
            }
        ),
        json.dumps(
            {
                "requestId": "r1",
                "timestamp": "2025-01-15T10:00:03Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I see the file. Let me update it."},
                    ],
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


@pytest.fixture
def empty_transcript(tmp_path: Path) -> Path:
    """Create an empty transcript file."""
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    return transcript


@pytest.fixture
def malformed_transcript(tmp_path: Path) -> Path:
    """Create a transcript with malformed entries."""
    transcript = tmp_path / "malformed.jsonl"
    lines = [
        "not valid json",
        json.dumps({"requestId": "r1", "timestamp": "2025-01-15T10:00:00Z"}),  # Missing message
        json.dumps(
            {
                "requestId": "r2",
                "timestamp": "2025-01-15T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Valid entry"}],
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


# -----------------------------------------------------------------------------
# Test truncate
# -----------------------------------------------------------------------------


class TestTruncate:
    """Tests for truncate helper (forge.core.transcript)."""

    def test_short_string_unchanged(self) -> None:
        """Short strings should not be truncated."""
        assert truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self) -> None:
        """String exactly at limit should not be truncated."""
        assert truncate("hello", 5) == "hello"

    def test_long_string_truncated(self) -> None:
        """Long strings should be truncated with ellipsis."""
        result = truncate("hello world", 5)
        assert result == "hello..."
        assert len(result) == 8  # 5 chars + "..."

    def test_empty_string(self) -> None:
        """Empty string should remain empty."""
        assert truncate("", 10) == ""

    def test_unicode_preserved(self) -> None:
        """Unicode characters should be preserved (string slice, not bytes)."""
        # 5 chars including unicode
        result = truncate("héllo wörld", 5)
        assert result == "héllo..."


# -----------------------------------------------------------------------------
# Test estimate_transcript_tokens
# -----------------------------------------------------------------------------


class TestEstimateTranscriptTokens:
    """Tests for estimate_transcript_tokens."""

    def test_estimates_from_file_size(self, sample_transcript: Path) -> None:
        """Should estimate tokens as file_size / 4."""
        file_size = sample_transcript.stat().st_size
        expected = file_size // 4
        assert estimate_transcript_tokens(sample_transcript) == expected

    def test_estimate_multiplier(self, sample_transcript: Path) -> None:
        """Model-specific tokenizer multipliers adjust the heuristic estimate."""
        file_size = sample_transcript.stat().st_size
        expected = int((file_size // 4) * 1.35)
        assert estimate_transcript_tokens(sample_transcript, multiplier=1.35) == expected

    def test_empty_file(self, empty_transcript: Path) -> None:
        """Empty file should return 0 tokens."""
        assert estimate_transcript_tokens(empty_transcript) == 0


# -----------------------------------------------------------------------------
# Test parse_jsonl_transcript
# -----------------------------------------------------------------------------


class TestParseTranscript:
    """Tests for parse_jsonl_transcript (forge.core.transcript)."""

    def test_parses_valid_entries(self, sample_transcript: Path) -> None:
        """Should parse all valid entries from transcript."""
        entries = parse_jsonl_transcript(sample_transcript)
        assert len(entries) == 4

    def test_skips_malformed_json(self, malformed_transcript: Path) -> None:
        """Should skip malformed JSON lines without failing."""
        entries = parse_jsonl_transcript(malformed_transcript)
        # Only the valid entry with message should be parsed
        assert len(entries) == 1

    def test_sorts_by_timestamp(self, sample_transcript: Path) -> None:
        """Entries should be sorted by timestamp."""
        entries = parse_jsonl_transcript(sample_transcript)
        timestamps = [e.get("timestamp", "") for e in entries]
        assert timestamps == sorted(timestamps)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file should return empty list."""
        nonexistent = tmp_path / "nonexistent.jsonl"
        entries = parse_jsonl_transcript(nonexistent)
        assert entries == []


# -----------------------------------------------------------------------------
# Test resolve_lineage
# -----------------------------------------------------------------------------


def _mock_session(parent: str | None) -> Any:
    """Create a mock object with parent_session attribute for testing resolve_lineage.

    Uses a simple namespace object since resolve_lineage only accesses .parent_session.
    Cast is used at call site to satisfy type checker.
    """
    return type("MockSession", (), {"parent_session": parent})()


class TestResolveLineage:
    """Tests for resolve_lineage."""

    def test_single_parent(self) -> None:
        """depth=1 should return just the parent."""

        def mock_get_session(name: str) -> SessionState | None:
            return None

        lineage = resolve_lineage("parent", depth=1, get_session=mock_get_session)
        assert lineage == ["parent"]

    def test_multiple_ancestors(self) -> None:
        """Should traverse ancestry chain up to depth."""
        # Mock session states with parent chain
        sessions: dict[str, Any] = {
            "child": _mock_session("parent"),
            "parent": _mock_session("grandparent"),
            "grandparent": _mock_session(None),
        }

        # Cast needed: mock objects have .parent_session but aren't SessionState
        get_session = cast("type[SessionState | None]", lambda name: sessions.get(name))

        lineage = resolve_lineage("child", depth=3, get_session=get_session)
        assert lineage == ["child", "parent", "grandparent"]

    def test_stops_at_missing_parent(self) -> None:
        """Should stop when parent's session doesn't exist."""
        # The lineage includes 'child', then 'nonexistent' is added because
        # child.parent_session points to it, but we can't go further because
        # nonexistent returns None from get_session

        def _get(name: str) -> Any:
            if name == "child":
                return _mock_session("nonexistent")
            return None

        get_session = cast("type[SessionState | None]", _get)

        lineage = resolve_lineage("child", depth=5, get_session=get_session)
        # Includes child, then nonexistent (we still add it to lineage)
        # but can't traverse further since get_session(nonexistent) is None
        assert lineage == ["child", "nonexistent"]

    def test_respects_depth_limit(self) -> None:
        """Should stop at depth limit even if more ancestors exist."""
        sessions: dict[str, Any] = {
            "a": _mock_session("b"),
            "b": _mock_session("c"),
            "c": _mock_session("d"),
            "d": _mock_session(None),
        }

        get_session = cast("type[SessionState | None]", lambda name: sessions.get(name))

        lineage = resolve_lineage("a", depth=2, get_session=get_session)
        assert lineage == ["a", "b"]


# -----------------------------------------------------------------------------
# Test _generate_minimal_context
# -----------------------------------------------------------------------------


class TestGenerateMinimalContext:
    """Tests for _generate_minimal_context."""

    def test_includes_parent_name(self) -> None:
        """Should include parent session name."""
        content = _generate_minimal_context(
            parent_name="test-parent",
            lineage=["test-parent"],
            artifacts_path=None,
            proxy_template=None,
        )
        assert "test-parent" in content
        assert "# Session Context: test-parent" in content

    def test_includes_lineage(self) -> None:
        """Should include lineage chain."""
        content = _generate_minimal_context(
            parent_name="child",
            lineage=["child", "parent", "grandparent"],
            artifacts_path=None,
            proxy_template=None,
        )
        assert "child ← parent ← grandparent" in content

    def test_includes_artifacts_path(self) -> None:
        """Should include artifacts path when provided."""
        content = _generate_minimal_context(
            parent_name="test",
            lineage=["test"],
            artifacts_path=".forge/artifacts/test/transcripts/abc.jsonl",
            proxy_template=None,
        )
        assert ".forge/artifacts/test/transcripts/abc.jsonl" in content

    def test_includes_proxy_template(self) -> None:
        """Should include proxy template when provided."""
        content = _generate_minimal_context(
            parent_name="test",
            lineage=["test"],
            artifacts_path=None,
            proxy_template="litellm-gemini",
        )
        assert "litellm-gemini" in content


# -----------------------------------------------------------------------------
# Test _generate_structured_context
# -----------------------------------------------------------------------------


class TestGenerateStructuredContext:
    """Tests for _generate_structured_context."""

    def test_includes_conversation_summary(self, sample_transcript: Path) -> None:
        """Should include conversation summary section."""
        content, warnings = _generate_structured_context(
            parent_name="test",
            lineage=["test"],
            transcript_path=sample_transcript,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )
        assert "## Conversation Summary" in content
        assert warnings == []

    def test_truncates_messages(self, tmp_path: Path) -> None:
        """Should truncate long messages."""
        long_message = "x" * 1000
        transcript = tmp_path / "long.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "requestId": "r1",
                    "timestamp": "2025-01-15T10:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": long_message}],
                    },
                }
            ),
            encoding="utf-8",
        )

        content, warnings = _generate_structured_context(
            parent_name="test",
            lineage=["test"],
            transcript_path=transcript,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )
        # Message should be truncated (500 chars + "...")
        assert long_message not in content
        assert "..." in content

    def test_includes_tool_summaries(self, sample_transcript: Path) -> None:
        """Should include tool call summaries."""
        content, warnings = _generate_structured_context(
            parent_name="test",
            lineage=["test"],
            transcript_path=sample_transcript,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )
        assert "Read" in content
        assert "/path/to/file.py" in content

    def test_warns_when_transcript_missing(self, tmp_path: Path) -> None:
        """Should warn when transcript doesn't exist."""
        nonexistent = tmp_path / "nonexistent.jsonl"
        content, warnings = _generate_structured_context(
            parent_name="test",
            lineage=["test"],
            transcript_path=nonexistent,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )
        assert "*Transcript not available.*" in content
        assert len(warnings) == 1
        assert "not found" in warnings[0]

    def test_handles_requestless_legacy_entries_with_message_content(self, tmp_path: Path) -> None:
        """Request-less legacy entries should still produce a structured turn."""
        transcript = tmp_path / "legacy-message.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "timestamp": "2025-01-15T10:00:00Z",
                            "message": {"content": [{"type": "text", "text": "hello from parent"}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": "2025-01-15T10:00:01Z",
                            "message": {"content": [{"type": "text", "text": "hi from assistant"}]},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        content, warnings = _generate_structured_context(
            parent_name="legacy-message",
            lineage=["legacy-message"],
            transcript_path=transcript,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )

        assert "### Turn 1" in content
        assert "**User**: hello from parent" in content
        assert "**Assistant**: hi from assistant" in content
        assert "*No conversation content found.*" not in content
        assert warnings == []

    def test_handles_older_text_only_entries_without_request_id(self, tmp_path: Path) -> None:
        """Older text-only transcript entries should still be summarized."""
        transcript = tmp_path / "legacy-text.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "human",
                            "timestamp": "2025-01-15T10:00:00Z",
                            "text": "legacy hello",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "ai",
                            "timestamp": "2025-01-15T10:00:01Z",
                            "text": "legacy response",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        content, warnings = _generate_structured_context(
            parent_name="legacy-text",
            lineage=["legacy-text"],
            transcript_path=transcript,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )

        assert "### Turn 1" in content
        assert "**User**: legacy hello" in content
        assert "**Assistant**: legacy response" in content
        assert "*No conversation content found.*" not in content
        assert warnings == []


# -----------------------------------------------------------------------------
# Test ResumeStrategy enum
# -----------------------------------------------------------------------------


class TestResumeStrategy:
    """Tests for ResumeStrategy enum."""

    def test_values(self) -> None:
        """Should have expected values."""
        assert ResumeStrategy.MINIMAL.value == "minimal"
        assert ResumeStrategy.STRUCTURED.value == "structured"
        assert ResumeStrategy.FULL.value == "full"
        assert ResumeStrategy.AI_CURATED.value == "ai-curated"

    def test_from_string(self) -> None:
        """Should be constructible from string."""
        assert ResumeStrategy("minimal") == ResumeStrategy.MINIMAL
        assert ResumeStrategy("structured") == ResumeStrategy.STRUCTURED
        assert ResumeStrategy("full") == ResumeStrategy.FULL
        assert ResumeStrategy("ai-curated") == ResumeStrategy.AI_CURATED

    def test_invalid_raises(self) -> None:
        """Invalid values should raise ValueError."""
        with pytest.raises(ValueError):
            ResumeStrategy("invalid")


# -----------------------------------------------------------------------------
# Test with fixture file
# -----------------------------------------------------------------------------


class TestWithFixtureFile:
    """Tests using the shared transcript fixture file."""

    @pytest.fixture
    def fixture_transcript(self) -> Path:
        """Return path to the shared fixture file."""
        return Path(__file__).parent.parent.parent / "fixtures" / "transcript_sample.jsonl"

    def test_parses_fixture(self, fixture_transcript: Path) -> None:
        """Should parse the shared fixture file."""
        # Fixture is committed - fail if missing (catches packaging/path issues)
        assert fixture_transcript.exists(), f"Fixture file not found at {fixture_transcript}"

        entries = parse_jsonl_transcript(fixture_transcript)
        assert len(entries) == 10  # 10 entries in fixture

    def test_structured_context_from_fixture(self, fixture_transcript: Path) -> None:
        """Should generate structured context from fixture."""
        # Fixture is committed - fail if missing (catches packaging/path issues)
        assert fixture_transcript.exists(), f"Fixture file not found at {fixture_transcript}"

        content, warnings = _generate_structured_context(
            parent_name="fixture-test",
            lineage=["fixture-test"],
            transcript_path=fixture_transcript,
            artifacts_path=".forge/artifacts/fixture-test/transcripts/abc.jsonl",
            proxy_template="litellm-gemini",
            latest_plan_path=".claude/plans/my-plan.md",
        )

        # Check key elements
        assert "# Session Context: fixture-test" in content
        assert "litellm-gemini" in content
        assert "## Conversation Summary" in content
        assert "## Artifacts" in content
        assert ".claude/plans/my-plan.md" in content


# -----------------------------------------------------------------------------
# Test _format_transcript_for_llm
# -----------------------------------------------------------------------------


class TestFormatTranscriptForLLM:
    """Tests for transcript formatting with input bounding."""

    def test_formats_entries_correctly(self, sample_transcript: Path) -> None:
        """Should format transcript entries as [ROLE] text lines."""
        entries = parse_jsonl_transcript(sample_transcript)
        formatted, was_truncated = _format_transcript_for_llm(entries)

        assert "[USER]" in formatted or "[ASSISTANT]" in formatted
        assert was_truncated is False

    def test_respects_max_chars_limit(self, tmp_path: Path) -> None:
        """Should truncate transcript at MAX_TRANSCRIPT_CHARS."""
        # Create large transcript that exceeds limit
        large_transcript = tmp_path / "large.jsonl"
        entries = [
            json.dumps(
                {
                    "requestId": f"r{i}",
                    "timestamp": f"2025-01-{(i % 28) + 1:02d}T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "x" * 1000}],
                    },
                }
            )
            for i in range(100)  # 100 entries × 1000 chars = ~100K chars
        ]
        large_transcript.write_text("\n".join(entries), encoding="utf-8")

        parsed = parse_jsonl_transcript(large_transcript)
        formatted, was_truncated = _format_transcript_for_llm(parsed)

        # Should be truncated and include marker
        assert was_truncated is True
        assert "...(transcript truncated for length)" in formatted
        assert len(formatted) <= MAX_TRANSCRIPT_CHARS + 100  # +100 for marker

    def test_empty_entries_returns_empty(self) -> None:
        """Empty entries should return empty string and no truncation."""
        formatted, was_truncated = _format_transcript_for_llm([])
        assert formatted == ""
        assert was_truncated is False


# -----------------------------------------------------------------------------
# Test _generate_ai_curated_context
# -----------------------------------------------------------------------------


class TestAICuratedStrategy:
    """Tests for AI-curated strategy with mocked LLM."""

    def test_ai_curated_calls_llm_and_returns_highlights(self, sample_transcript: Path) -> None:
        """AI-curated should call LLM and include highlights in output."""
        from unittest.mock import MagicMock, patch

        mock_adapter = MagicMock()
        mock_adapter.ask.return_value = "- Key decision made\n- Files modified"

        # Patch at source module since lazy import is used
        with (
            patch("forge.core.llm.SyncAdapter", return_value=mock_adapter),
            patch("forge.core.llm.get_client"),
        ):
            content, warnings = _generate_ai_curated_context(
                parent_name="test-parent",
                lineage=["test-parent"],
                transcript_path=sample_transcript,
                artifacts_path=None,
                proxy_template=None,
                latest_plan_path=None,
            )

        # Assert LLM was called
        mock_adapter.ask.assert_called_once()
        # Assert output contains strategy marker
        assert "ai-curated" in content
        # Assert LLM output is included
        assert "Key decision made" in content
        # Assert security warning is present
        assert any("for processing" in w for w in warnings)

    def test_ai_curated_fallback_on_llm_error(self, sample_transcript: Path) -> None:
        """Should fall back to structured on LLM error."""
        from unittest.mock import MagicMock, patch

        mock_adapter = MagicMock()
        mock_adapter.ask.side_effect = Exception("API timeout")

        # Patch at source module since lazy import is used
        with (
            patch("forge.core.llm.SyncAdapter", return_value=mock_adapter),
            patch("forge.core.llm.get_client"),
        ):
            content, warnings = _generate_ai_curated_context(
                parent_name="test",
                lineage=["test"],
                transcript_path=sample_transcript,
                artifacts_path=None,
                proxy_template=None,
                latest_plan_path=None,
            )

        # Assert fallback warning
        assert any("using structured" in w.lower() for w in warnings)
        # Assert output is NOT ai-curated (should be structured)
        assert "ai-curated" not in content
        # Structured output has Conversation Summary section
        assert "Conversation Summary" in content

    def test_ai_curated_no_transcript_uses_minimal(self) -> None:
        """Should use minimal strategy if no transcript."""
        content, warnings = _generate_ai_curated_context(
            parent_name="test",
            lineage=["test"],
            transcript_path=None,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )

        # Assert fallback warning
        assert any("using minimal" in w.lower() for w in warnings)
        # Assert output is NOT ai-curated
        assert "ai-curated" not in content
        # Minimal output has Lineage section
        assert "## Lineage" in content

    def test_ai_curated_empty_transcript_uses_minimal(self, tmp_path: Path) -> None:
        """Should use minimal strategy if transcript is empty."""
        empty_transcript = tmp_path / "empty.jsonl"
        empty_transcript.write_text("", encoding="utf-8")

        content, warnings = _generate_ai_curated_context(
            parent_name="test",
            lineage=["test"],
            transcript_path=empty_transcript,
            artifacts_path=None,
            proxy_template=None,
            latest_plan_path=None,
        )

        # Assert fallback warning
        assert any("using minimal" in w.lower() for w in warnings)
        assert "ai-curated" not in content

    def test_transcript_truncation_adds_warning(self, tmp_path: Path) -> None:
        """Should warn when transcript is truncated."""
        from unittest.mock import MagicMock, patch

        # Create oversized transcript
        large_transcript = tmp_path / "large.jsonl"
        entries = [
            json.dumps(
                {
                    "requestId": f"r{i}",
                    "timestamp": f"2025-01-{(i % 28) + 1:02d}T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "x" * 1000}],
                    },
                }
            )
            for i in range(100)  # Exceeds MAX_TRANSCRIPT_CHARS
        ]
        large_transcript.write_text("\n".join(entries), encoding="utf-8")

        # Mock LLM (patch at source module since lazy import is used)
        mock_adapter = MagicMock()
        mock_adapter.ask.return_value = "- Highlights"

        with (
            patch("forge.core.llm.SyncAdapter", return_value=mock_adapter),
            patch("forge.core.llm.get_client"),
        ):
            content, warnings = _generate_ai_curated_context(
                parent_name="test",
                lineage=["test"],
                transcript_path=large_transcript,
                artifacts_path=None,
                proxy_template=None,
                latest_plan_path=None,
            )

        # Should have truncation warning
        assert any("truncated" in w.lower() for w in warnings)
        # But should still succeed with ai-curated
        assert "ai-curated" in content


class TestResolvePlanContent:
    """Tests for _resolve_plan_content plan resolution."""

    def test_approved_snapshot_preferred(self, tmp_path: Path) -> None:
        """Approved plan snapshot wins over latest_plan_path."""
        # Create snapshot (repo-root-relative)
        snapshot_dir = tmp_path / ".forge" / "artifacts" / "planner" / "plans"
        snapshot_dir.mkdir(parents=True)
        snapshot = snapshot_dir / "plan_20260325.md"
        snapshot.write_text("# The Approved Plan\nStep 1: do the thing")

        # Create a different file at latest_plan_path
        draft = tmp_path / ".claude" / "plans" / "draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# Draft (should not be used)")

        confirmed = cast(
            Any,
            type(
                "C",
                (),
                {
                    "artifacts": {
                        "plans": [
                            {
                                "kind": "approved",
                                "snapshot_path": ".forge/artifacts/planner/plans/plan_20260325.md",
                            }
                        ]
                    },
                    "latest_plan_path": ".claude/plans/draft.md",
                },
            )(),
        )

        result = _resolve_plan_content(confirmed, tmp_path)
        assert result is not None
        assert "The Approved Plan" in result
        assert "Draft" not in result

    def test_latest_plan_path_fallback(self, tmp_path: Path) -> None:
        """Falls back to latest_plan_path when no approved snapshots exist."""
        plan_file = tmp_path / ".claude" / "plans" / "my-plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# Fallback Plan")

        confirmed = cast(
            Any,
            type(
                "C",
                (),
                {
                    "artifacts": {},
                    "latest_plan_path": ".claude/plans/my-plan.md",
                },
            )(),
        )

        # latest_plan_path resolves against parent_worktree_root
        result = _resolve_plan_content(confirmed, Path("/nonexistent"), parent_worktree_root=tmp_path)
        assert result is not None
        assert "Fallback Plan" in result

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Returns None when plan files don't exist on disk."""
        confirmed = cast(
            Any,
            type(
                "C",
                (),
                {
                    "artifacts": {"plans": [{"kind": "approved", "snapshot_path": "nonexistent.md"}]},
                    "latest_plan_path": "also-nonexistent.md",
                },
            )(),
        )

        result = _resolve_plan_content(confirmed, tmp_path)
        assert result is None

    def test_no_plan_at_all(self) -> None:
        """Returns None when no plan path is configured."""
        confirmed = cast(
            Any,
            type(
                "C",
                (),
                {
                    "artifacts": {},
                    "latest_plan_path": None,
                },
            )(),
        )

        result = _resolve_plan_content(confirmed, Path("/tmp"))
        assert result is None


class TestInlinePlan:
    """Tests for inline_plan parameter in process_handoff and strategy generators."""

    def _make_parent_state(self, tmp_path: Path, *, with_plan: bool = False) -> SessionState:
        """Create a minimal parent SessionState for testing."""
        from forge.session.models import create_session_state

        state = create_session_state(
            name="parent",
            worktree_path=str(tmp_path),
        )
        if with_plan:
            plan_dir = tmp_path / ".forge" / "artifacts" / "parent" / "plans"
            plan_dir.mkdir(parents=True)
            plan_file = plan_dir / "plan_test.md"
            plan_file.write_text("# Test Plan\n\n1. Do X\n2. Do Y")
            state.confirmed.artifacts["plans"] = [
                {"kind": "approved", "snapshot_path": ".forge/artifacts/parent/plans/plan_test.md"}
            ]
        return state

    def test_inline_plan_false_shows_path_only(self, tmp_path: Path) -> None:
        """Default inline_plan=False shows path reference, not content."""
        state = self._make_parent_state(tmp_path, with_plan=True)
        state.confirmed.latest_plan_path = ".claude/plans/draft.md"

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.STRUCTURED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=False,
        )
        content = result.context_file.read_text() if result.context_file else ""
        assert "## Approved Plan" not in content
        assert "Test Plan" not in content

    def test_inline_plan_true_includes_content(self, tmp_path: Path) -> None:
        """inline_plan=True inlines the approved plan content."""
        state = self._make_parent_state(tmp_path, with_plan=True)

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.STRUCTURED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        content = result.context_file.read_text() if result.context_file else ""
        assert "## Approved Plan" in content
        assert "Do X" in content
        assert "Do Y" in content

    def test_inline_plan_missing_file_warns(self, tmp_path: Path) -> None:
        """inline_plan=True with missing plan file adds warning."""
        state = self._make_parent_state(tmp_path, with_plan=False)
        state.confirmed.latest_plan_path = "nonexistent/plan.md"

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.STRUCTURED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        assert any("not found" in w.lower() for w in result.warnings)

    def test_inline_plan_no_plan_configured_warns(self, tmp_path: Path) -> None:
        """inline_plan=True with no plan path at all still warns."""
        state = self._make_parent_state(tmp_path, with_plan=False)

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.STRUCTURED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        assert any("not found" in w.lower() for w in result.warnings)
        assert any("no plan path" in w.lower() for w in result.warnings)

    def test_inline_plan_two_root_resolution(self, tmp_path: Path) -> None:
        """Approved snapshot resolves against project_root; latest_plan_path against worktree root."""
        # project_root (main repo) has the approved snapshot
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        snapshot_dir = main_repo / ".forge" / "artifacts" / "parent" / "plans"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "plan.md").write_text("# From Main Repo Snapshot")

        # parent worktree is a different directory
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = self._make_parent_state(worktree, with_plan=False)
        state.confirmed.artifacts["plans"] = [
            {"kind": "approved", "snapshot_path": ".forge/artifacts/parent/plans/plan.md"}
        ]

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=main_repo,
            parent_worktree_root=worktree,
            strategy=ResumeStrategy.STRUCTURED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        content = result.context_file.read_text() if result.context_file else ""
        assert "From Main Repo Snapshot" in content

    def test_inline_plan_fallback_uses_worktree_root(self, tmp_path: Path) -> None:
        """latest_plan_path fallback resolves against parent_worktree_root, not project_root."""
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        plan_file = worktree / ".claude" / "plans" / "draft.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# Plan From Worktree")

        state = self._make_parent_state(worktree, with_plan=False)
        state.confirmed.latest_plan_path = ".claude/plans/draft.md"

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=main_repo,
            parent_worktree_root=worktree,
            strategy=ResumeStrategy.STRUCTURED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        content = result.context_file.read_text() if result.context_file else ""
        assert "Plan From Worktree" in content

    def test_inline_plan_with_full_strategy(self, tmp_path: Path) -> None:
        """inline_plan works with full strategy too."""
        state = self._make_parent_state(tmp_path, with_plan=True)

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.FULL,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        content = result.context_file.read_text() if result.context_file else ""
        assert "## Approved Plan" in content
        assert "Do X" in content

    def test_inline_plan_with_minimal_strategy(self, tmp_path: Path) -> None:
        """inline_plan works with minimal strategy."""
        state = self._make_parent_state(tmp_path, with_plan=True)

        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.MINIMAL,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        content = result.context_file.read_text() if result.context_file else ""
        assert "## Approved Plan" in content
        assert "Do X" in content

    def test_inline_plan_with_ai_curated_strategy(self, tmp_path: Path) -> None:
        """inline_plan works with ai-curated strategy (falls back to structured on LLM error)."""
        state = self._make_parent_state(tmp_path, with_plan=True)

        # AI curation will fail (no LLM configured), falling back to structured
        result = process_handoff(
            parent_name="parent",
            parent_state=state,
            forge_root=tmp_path,
            strategy=ResumeStrategy.AI_CURATED,
            depth=1,
            get_session=lambda _: None,
            inline_plan=True,
        )
        content = result.context_file.read_text() if result.context_file else ""
        # Plan should be inlined regardless of which strategy runs
        assert "## Approved Plan" in content
        assert "Do X" in content
