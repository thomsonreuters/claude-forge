"""Tests for Derivation dataclass in forge.session.models.

This file tests the Derivation dataclass for session resume.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import dacite
import pytest

from forge.session.models import (
    SCHEMA_VERSION,
    Derivation,
    SessionConfirmed,
    SessionState,
    StartedWithProxy,
)


class TestDerivationDataclass:
    """Tests for Derivation dataclass."""

    def test_required_parent_session(self) -> None:
        """parent_session is required."""
        d = Derivation(parent_session="my-parent")
        assert d.parent_session == "my-parent"

    def test_default_values(self) -> None:
        """Optional fields should have sensible defaults."""
        d = Derivation(parent_session="test")
        assert d.parent_transcript is None
        assert d.inherited_proxy is None
        assert d.resume_mode is None
        assert d.strategy == "structured"
        assert d.depth == 1
        assert d.resumed_at is None
        assert d.lineage == []
        assert d.context_file is None

    def test_all_fields(self) -> None:
        """Should accept all fields."""
        d = Derivation(
            parent_session="parent",
            parent_transcript=".forge/artifacts/parent/transcripts/abc.jsonl",
            inherited_proxy="litellm-gemini",
            strategy="minimal",
            depth=3,
            resumed_at="2025-01-15T10:00:00+00:00",
            lineage=["parent", "grandparent", "great-grandparent"],
            context_file=".forge/prev_sessions/parent/children/child.md",
        )
        assert d.parent_session == "parent"
        assert d.parent_transcript == ".forge/artifacts/parent/transcripts/abc.jsonl"
        assert d.inherited_proxy == "litellm-gemini"
        assert d.strategy == "minimal"
        assert d.depth == 3
        assert d.resumed_at == "2025-01-15T10:00:00+00:00"
        assert d.lineage == ["parent", "grandparent", "great-grandparent"]
        assert d.context_file == ".forge/prev_sessions/parent/children/child.md"


class TestSessionConfirmedWithDerivation:
    """Tests for SessionConfirmed.derivation field."""

    def test_default_is_none(self) -> None:
        """derivation should default to None."""
        confirmed = SessionConfirmed()
        assert confirmed.derivation is None

    def test_with_derivation(self) -> None:
        """Should accept a Derivation instance."""
        d = Derivation(parent_session="test-parent", strategy="structured")
        confirmed = SessionConfirmed(derivation=d)
        assert confirmed.derivation is not None
        assert confirmed.derivation.parent_session == "test-parent"
        assert confirmed.derivation.strategy == "structured"

    def test_serializes_to_dict(self) -> None:
        """Should serialize derivation to dict correctly."""
        d = Derivation(
            parent_session="parent",
            inherited_proxy="litellm-openai",
            lineage=["parent", "grandparent"],
        )
        confirmed = SessionConfirmed(derivation=d)
        data = asdict(confirmed)

        assert data["derivation"] is not None
        assert data["derivation"]["parent_session"] == "parent"
        assert data["derivation"]["inherited_proxy"] == "litellm-openai"
        assert data["derivation"]["lineage"] == ["parent", "grandparent"]

    def test_json_roundtrip(self) -> None:
        """Should survive JSON serialization and dacite deserialization."""
        d = Derivation(
            parent_session="parent",
            parent_transcript=".forge/artifacts/parent/transcripts/abc.jsonl",
            inherited_proxy="litellm-gemini",
            strategy="full",
            depth=2,
            resumed_at="2025-01-15T10:00:00+00:00",
            lineage=["parent", "grandparent"],
            context_file=".forge/prev_sessions/parent/children/child.md",
        )
        confirmed = SessionConfirmed(
            derivation=d,
            confirmed_at="2025-01-15T10:00:00+00:00",
            confirmed_by="cli:resume",
        )

        # Serialize to JSON
        data = asdict(confirmed)
        json_str = json.dumps(data)

        # Deserialize with dacite
        parsed_data = json.loads(json_str)
        parsed = dacite.from_dict(
            SessionConfirmed,
            parsed_data,
            config=dacite.Config(strict_unions_match=False),
        )

        assert parsed.derivation is not None
        assert parsed.derivation.parent_session == "parent"
        assert parsed.derivation.parent_transcript == ".forge/artifacts/parent/transcripts/abc.jsonl"
        assert parsed.derivation.inherited_proxy == "litellm-gemini"
        assert parsed.derivation.strategy == "full"
        assert parsed.derivation.depth == 2
        assert parsed.derivation.lineage == ["parent", "grandparent"]


class TestSessionStateWithDerivation:
    """Tests for full SessionState roundtrip with derivation."""

    def test_full_state_roundtrip(self) -> None:
        """Should survive full SessionState JSON roundtrip."""
        d = Derivation(
            parent_session="parent-session",
            parent_transcript=".forge/artifacts/parent-session/transcripts/uuid-123.jsonl",
            inherited_proxy="litellm-openai",
            strategy="structured",
            depth=1,
            resumed_at="2025-01-15T10:30:00+00:00",
            lineage=["parent-session"],
            context_file=".forge/prev_sessions/parent-session/children/child-session.md",
        )

        confirmed = SessionConfirmed(
            claude_session_id="uuid-456",
            transcript_path="~/.claude/projects/test/uuid-456.jsonl",
            started_with_proxy=StartedWithProxy(
                base_url="http://localhost:8084",
                template="litellm-openai",
            ),
            derivation=d,
            confirmed_at="2025-01-15T10:30:00+00:00",
            confirmed_by="cli:resume",
        )

        state = SessionState(
            schema_version=SCHEMA_VERSION,
            name="child-session",
            created_at="2025-01-15T10:30:00+00:00",
            last_accessed_at="2025-01-15T10:30:00+00:00",
            parent_session="parent-session",
            is_fork=True,
            confirmed=confirmed,
        )

        # Serialize to JSON
        data = asdict(state)
        json_str = json.dumps(data)

        # Deserialize
        parsed_data = json.loads(json_str)
        parsed = dacite.from_dict(
            SessionState,
            parsed_data,
            config=dacite.Config(strict_unions_match=False),
        )

        # Verify all fields
        assert parsed.name == "child-session"
        assert parsed.parent_session == "parent-session"
        assert parsed.is_fork is True
        assert parsed.confirmed.derivation is not None
        assert parsed.confirmed.derivation.parent_session == "parent-session"
        assert parsed.confirmed.derivation.strategy == "structured"
        assert (
            parsed.confirmed.derivation.context_file == ".forge/prev_sessions/parent-session/children/child-session.md"
        )

    def test_backwards_compatible_without_derivation(self) -> None:
        """Sessions without derivation should still load correctly."""
        # Simulate an old session file that doesn't have derivation
        data = {
            "schema_version": SCHEMA_VERSION,
            "name": "old-session",
            "created_at": "2025-01-01T00:00:00+00:00",
            "last_accessed_at": "2025-01-01T00:00:00+00:00",
            "is_fork": False,
            "is_incognito": False,
            "intent": {"agent": "claude-code"},
            "overrides": {},
            "confirmed": {
                "claude_session_id": "old-uuid",
            },
        }

        state = dacite.from_dict(
            SessionState,
            data,
            config=dacite.Config(strict_unions_match=False),
        )

        assert state.name == "old-session"
        assert state.confirmed.derivation is None  # Should be None, not error


class TestDerivationStrategies:
    """Tests for strategy field validation."""

    @pytest.mark.parametrize("strategy", ["minimal", "structured", "full"])
    def test_valid_strategies(self, strategy: str) -> None:
        """Should accept valid strategy values."""
        d = Derivation(parent_session="test", strategy=strategy)
        assert d.strategy == strategy

    def test_strategy_not_enum_validated(self) -> None:
        """Strategy is a string, not enum - no runtime validation."""
        # This is intentional - we use string for flexibility
        d = Derivation(parent_session="test", strategy="custom")
        assert d.strategy == "custom"

    def test_strategy_nullable_for_native(self) -> None:
        """Strategy should be None when resume_mode is native (no handoff ran)."""
        d = Derivation(parent_session="test", resume_mode="native", strategy=None)
        assert d.strategy is None
        assert d.resume_mode == "native"

    def test_resume_mode_default_none(self) -> None:
        """resume_mode defaults to None (legacy handoff)."""
        d = Derivation(parent_session="test")
        assert d.resume_mode is None


class TestDerivationNativeResumeRoundtrip:
    """Tests for native resume mode serialization."""

    def test_native_derivation_json_roundtrip(self) -> None:
        """Native derivation (strategy=None, resume_mode='native') survives JSON roundtrip."""
        d = Derivation(
            parent_session="parent",
            parent_transcript=".forge/artifacts/parent/transcripts/abc.jsonl",
            inherited_proxy="litellm-openai",
            resume_mode="native",
            strategy=None,
            depth=0,
            resumed_at="2025-01-15T10:00:00+00:00",
            lineage=["parent"],
            context_file=None,
        )
        confirmed = SessionConfirmed(derivation=d)

        data = asdict(confirmed)
        json_str = json.dumps(data)
        parsed_data = json.loads(json_str)
        parsed = dacite.from_dict(
            SessionConfirmed,
            parsed_data,
            config=dacite.Config(strict_unions_match=False),
        )

        assert parsed.derivation is not None
        assert parsed.derivation.resume_mode == "native"
        assert parsed.derivation.strategy is None
        assert parsed.derivation.context_file is None
        assert parsed.derivation.parent_session == "parent"

    def test_legacy_derivation_without_resume_mode(self) -> None:
        """Old derivation data without resume_mode field loads with None default."""
        data = {
            "parent_session": "old-parent",
            "strategy": "structured",
            "depth": 1,
            "lineage": ["old-parent"],
        }
        d = dacite.from_dict(
            Derivation,
            data,
            config=dacite.Config(strict_unions_match=False),
        )
        assert d.resume_mode is None
        assert d.strategy == "structured"
