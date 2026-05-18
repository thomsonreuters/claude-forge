"""Tests for the handoff agent core module.

Covers: turn counting, prompt building, proxy resolution, agent invocation,
multi-doc strategies, shadow/propose mode, containment guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.core.reactive.session_runner import SessionResult
from forge.session.handoff_agent import (
    DOC_STRATEGIES,
    _stdout_indicates_permission_denied,
    _validate_designated_docs,
    build_multi_doc_prompt,
    count_conversation_turns,
    resolve_handoff_base_url,
    run_handoff_agent,
)
from forge.session.models import DesignatedDoc, HandoffConfig

# ---------------------------------------------------------------------------
# Transcript fixtures
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, entries: list[dict]) -> Path:
    """Write entries as JSONL to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


def _make_newer_entry(request_id: str, role: str, text: str = "hello", timestamp: str = "") -> dict:
    """Create a newer-format transcript entry (requestId + message.role)."""
    return {
        "requestId": request_id,
        "timestamp": timestamp,
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    }


def _make_older_entry(entry_type: str, text: str = "hello") -> dict:
    """Create an older-format transcript entry (type field)."""
    return {"type": entry_type, "text": text}


# ---------------------------------------------------------------------------
# count_conversation_turns
# ---------------------------------------------------------------------------


class TestCountConversationTurns:
    """Tests for counting conversation turns in transcript files."""

    def test_empty_file(self, tmp_path: Path) -> None:
        """Empty transcript returns 0 turns."""
        path = _write_transcript(tmp_path / "t.jsonl", [])
        assert count_conversation_turns(path) == 0

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing transcript file returns 0 turns."""
        assert count_conversation_turns(tmp_path / "nonexistent.jsonl") == 0

    def test_newer_format_single_turn(self, tmp_path: Path) -> None:
        """Single user+assistant pair counts as 1 turn."""
        entries = [
            _make_newer_entry("req-1", "user", "hello"),
            _make_newer_entry("req-1", "assistant", "hi there"),
        ]
        path = _write_transcript(tmp_path / "t.jsonl", entries)
        assert count_conversation_turns(path) == 1

    def test_newer_format_multi_turn(self, tmp_path: Path) -> None:
        """Multiple request groups each count as a turn."""
        entries = [
            _make_newer_entry("req-1", "user"),
            _make_newer_entry("req-1", "assistant"),
            _make_newer_entry("req-2", "user"),
            _make_newer_entry("req-2", "assistant"),
            _make_newer_entry("req-3", "user"),
            _make_newer_entry("req-3", "assistant"),
        ]
        path = _write_transcript(tmp_path / "t.jsonl", entries)
        assert count_conversation_turns(path) == 3

    def test_newer_format_assistant_only_not_counted(self, tmp_path: Path) -> None:
        """Request groups with only assistant messages don't count."""
        entries = [
            _make_newer_entry("req-1", "user"),
            _make_newer_entry("req-1", "assistant"),
            # req-2 has only assistant (e.g., tool result without user prompt)
            _make_newer_entry("req-2", "assistant"),
        ]
        path = _write_transcript(tmp_path / "t.jsonl", entries)
        assert count_conversation_turns(path) == 1

    def test_older_format_counts_human_entries(self, tmp_path: Path) -> None:
        """Older format counts entries with type='human'."""
        entries = [
            _make_older_entry("human"),
            _make_older_entry("ai"),
            _make_older_entry("human"),
            _make_older_entry("ai"),
        ]
        path = _write_transcript(tmp_path / "t.jsonl", entries)
        assert count_conversation_turns(path) == 2

    def test_older_format_no_human_entries(self, tmp_path: Path) -> None:
        """Older format with no human entries returns 0."""
        entries = [
            _make_older_entry("ai"),
            _make_older_entry("tool"),
        ]
        path = _write_transcript(tmp_path / "t.jsonl", entries)
        assert count_conversation_turns(path) == 0


# ---------------------------------------------------------------------------
# DOC_STRATEGIES
# ---------------------------------------------------------------------------


class TestDocStrategies:
    """Tests for the per-doc strategy constants."""

    def test_all_built_in_strategies_defined(self) -> None:
        """All built-in strategies have instruction text.

        Intentionally exact (not subset): forces conscious strategy additions
        and ensures removed strategies don't linger.
        """
        expected = {
            "project-state",
            "checklist",
            "changelog",
            "debugging",
            "patterns",
            "suggested",
            "generic",
        }
        assert set(DOC_STRATEGIES.keys()) == expected

    def test_strategies_are_non_empty_strings(self) -> None:
        """Each strategy instruction is a non-empty string."""
        for name, instruction in DOC_STRATEGIES.items():
            assert isinstance(instruction, str), f"{name} is not a string"
            assert len(instruction) > 0, f"{name} is empty"

    def test_no_remove_instructions(self) -> None:
        """Strategy instructions must not encourage destructive edits.

        Exception: 'suggested' strategy explicitly removes merged items (self-prune).
        """
        for name, instruction in DOC_STRATEGIES.items():
            if name == "suggested":
                continue  # self-prune is intentional for shadow docs
            lower = instruction.lower()
            assert "remove them" not in lower, f"{name} contains 'remove them'"
            assert "delete" not in lower, f"{name} contains 'delete'"

    def test_debugging_strategy_defined(self) -> None:
        """Debugging strategy records error causes and solutions."""
        assert "error" in DOC_STRATEGIES["debugging"].lower()
        assert "solutions" in DOC_STRATEGIES["debugging"].lower()

    def test_patterns_strategy_defined(self) -> None:
        """Patterns strategy records architecture patterns and conventions."""
        assert "pattern" in DOC_STRATEGIES["patterns"].lower()
        assert "convention" in DOC_STRATEGIES["patterns"].lower()

    def test_suggested_strategy_defined(self) -> None:
        """Suggested strategy proposes checkboxes and self-prunes merged items."""
        text = DOC_STRATEGIES["suggested"]
        assert "- [ ]" in text
        assert "self-prune" in text.lower() or "remove" in text.lower()


# ---------------------------------------------------------------------------
# build_multi_doc_prompt
# ---------------------------------------------------------------------------


class TestBuildMultiDocPrompt:
    """Tests for multi-doc prompt construction."""

    def test_contains_all_doc_paths(self) -> None:
        """Prompt lists all designated document paths."""
        docs = [
            DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
            DesignatedDoc(path="docs/changelog.md", strategy="changelog"),
        ]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "docs/checklist.md" in prompt
        assert "docs/changelog.md" in prompt

    def test_checklist_strategy_content(self) -> None:
        """Checklist strategy includes mark-completed instruction."""
        docs = [DesignatedDoc(path="docs/checklist.md", strategy="checklist")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "Mark completed tasks" in prompt
        assert "Do NOT remove" in prompt

    def test_changelog_strategy_content(self) -> None:
        """Changelog strategy includes add-accomplishments instruction."""
        docs = [DesignatedDoc(path="docs/log.md", strategy="changelog")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "accomplishments" in prompt
        assert "Do NOT modify or remove" in prompt

    def test_generic_strategy_content(self) -> None:
        """Generic strategy includes read-and-add instruction."""
        docs = [DesignatedDoc(path="docs/notes.md", strategy="generic")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "NEW information" in prompt

    def test_unknown_strategy_falls_back_to_generic(self) -> None:
        """Unknown strategy name uses generic instructions without crashing."""
        docs = [DesignatedDoc(path="docs/foo.md", strategy="unknown-strategy")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "NEW information" in prompt
        assert "docs/foo.md" in prompt

    def test_review_only_mode(self) -> None:
        """Review-only mode instructs no file modifications."""
        docs = [DesignatedDoc(path="docs/foo.md")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            mode="review-only",
            designated_docs=docs,
        )
        assert "Do NOT modify any files" in prompt

    def test_contains_session_info(self) -> None:
        """Prompt includes session name and transcript path."""
        docs = [DesignatedDoc(path="docs/foo.md")]
        prompt = build_multi_doc_prompt(
            session_name="my-session",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "my-session" in prompt
        assert "/abs/path/t.jsonl" in prompt

    def test_multiple_strategies_combined(self) -> None:
        """Multiple docs with different strategies all appear in prompt."""
        docs = [
            DesignatedDoc(path=".forge/memory/project-state.md", strategy="project-state"),
            DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
            DesignatedDoc(path="docs/changelog.md", strategy="changelog"),
        ]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert ".forge/memory/project-state.md" in prompt
        assert "docs/checklist.md" in prompt
        assert "docs/changelog.md" in prompt

    def test_global_rule_allows_per_file_edits(self) -> None:
        """Global prompt rule defers to per-file instructions (no contradiction)."""
        docs = [DesignatedDoc(path="docs/checklist.md", strategy="checklist")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "Only ADD information" not in prompt
        assert "per-file instructions" in prompt or "minimal edits" in prompt

    # Shadow/propose mode (Mode 2)

    def test_shadow_prompt_includes_official_doc(self) -> None:
        """Shadow doc prompt references the official document path."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested_standards.md",
                strategy="suggested",
                shadows="docs/developer/coding-standards.md",
            )
        ]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "docs/developer/coding-standards.md" in prompt
        assert ".forge/memory/suggested_standards.md" in prompt

    def test_shadow_prompt_reads_official_first(self) -> None:
        """Shadow doc prompt instructs reading the official doc first."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="OFFICIAL.md",
            )
        ]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "Read the OFFICIAL document at `OFFICIAL.md` first" in prompt

    def test_direct_doc_no_shadow_section(self) -> None:
        """Non-shadow doc has no 'proposes changes to' text."""
        docs = [DesignatedDoc(path="docs/checklist.md", strategy="checklist")]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        assert "proposes changes to" not in prompt

    def test_mixed_shadow_and_direct(self) -> None:
        """Prompt handles both shadow and direct docs in one invocation."""
        docs = [
            DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="STANDARDS.md",
            ),
        ]
        prompt = build_multi_doc_prompt(
            session_name="test",
            transcript_path="/abs/path/t.jsonl",
            designated_docs=docs,
        )
        # Direct doc: no shadow language
        assert "docs/checklist.md" in prompt
        assert "Mark completed tasks" in prompt
        # Shadow doc: has shadow language
        assert "proposes changes to `STANDARDS.md`" in prompt
        assert "Read the OFFICIAL document" in prompt


# ---------------------------------------------------------------------------
# resolve_handoff_base_url
# ---------------------------------------------------------------------------


def _unresolved_result():
    """RoutingResult with no base_url (unresolved)."""
    from forge.core.reactive.routing import RoutingResult

    return RoutingResult(
        base_url=None,
        proxy_id=None,
        template=None,
        source="unresolved",
        route=None,
        credential=None,
    )


def _resolved_result(base_url: str = "http://proxy:8080"):
    """RoutingResult with a resolved base_url."""
    from forge.core.reactive.routing import RoutingResult

    return RoutingResult(
        base_url=base_url,
        proxy_id="my-proxy",
        template="litellm-openai",
        source="preferred_proxy",
        route=None,
        credential=None,
    )


class TestResolveHandoffBaseUrl:
    """Tests for proxy base URL resolution via shared resolver."""

    def test_proxy_id_takes_priority(self) -> None:
        """When proxy_id resolves via shared resolver, it takes priority."""
        with patch(
            "forge.session.handoff_agent.resolve_subprocess_routing",
            return_value=_resolved_result("http://proxy-from-registry:8080"),
        ):
            result = resolve_handoff_base_url(
                proxy_id="my-proxy",
                confirmed_proxy_base_url="http://session-proxy:8084",
                env_base_url="http://env-proxy:8085",
            )
        assert result == "http://proxy-from-registry:8080"

    def test_confirmed_proxy_over_env(self) -> None:
        """When no proxy_id, confirmed proxy URL is used over env."""
        result = resolve_handoff_base_url(
            proxy_id=None,
            confirmed_proxy_base_url="http://session-proxy:8084",
            env_base_url="http://env-proxy:8085",
        )
        assert result == "http://session-proxy:8084"

    def test_env_fallback(self) -> None:
        """When no proxy_id or confirmed proxy, uses env ANTHROPIC_BASE_URL."""
        result = resolve_handoff_base_url(
            proxy_id=None,
            confirmed_proxy_base_url=None,
            env_base_url="http://env-proxy:8085",
        )
        assert result == "http://env-proxy:8085"

    def test_none_when_no_sources(self) -> None:
        """Returns None when all sources are empty (Anthropic direct)."""
        result = resolve_handoff_base_url(
            proxy_id=None,
            confirmed_proxy_base_url=None,
            env_base_url=None,
        )
        assert result is None

    def test_proxy_id_lookup_failure_falls_through(self) -> None:
        """When proxy_id lookup fails, falls through to confirmed proxy."""
        with patch(
            "forge.session.handoff_agent.resolve_subprocess_routing",
            return_value=_unresolved_result(),
        ):
            result = resolve_handoff_base_url(
                proxy_id="nonexistent-proxy",
                confirmed_proxy_base_url="http://session-proxy:8084",
                env_base_url=None,
            )
        assert result == "http://session-proxy:8084"

    def test_proxy_miss_prefers_confirmed_proxy_over_ambient_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ambient ANTHROPIC_BASE_URL must not beat the session's confirmed proxy."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://ambient-env-proxy:8080")

        result = resolve_handoff_base_url(
            proxy_id="definitely-missing-proxy-for-handoff-test",
            confirmed_proxy_base_url="http://session-proxy:8084",
            env_base_url="http://ambient-env-proxy:8080",
        )

        assert result == "http://session-proxy:8084"

    def test_subprocess_proxy_used_before_confirmed_proxy(self) -> None:
        """Persisted subprocess proxy is tried before falling back to the session proxy."""
        with patch(
            "forge.session.handoff_agent.resolve_subprocess_routing",
            return_value=_resolved_result("http://subprocess-proxy:8080"),
        ) as mock_resolver:
            result = resolve_handoff_base_url(
                proxy_id=None,
                subprocess_proxy="openrouter-subprocess",
                confirmed_proxy_base_url="http://session-proxy:8084",
                env_base_url=None,
            )

        assert result == "http://subprocess-proxy:8080"
        mock_resolver.assert_called_once_with(
            preferred_proxy="openrouter-subprocess",
            require_route=False,
            use_environment=False,
        )

    def test_config_proxy_takes_priority_over_subprocess_proxy(self) -> None:
        """Handoff-specific proxy remains the highest-priority handoff route."""
        with patch(
            "forge.session.handoff_agent.resolve_subprocess_routing",
            return_value=_resolved_result("http://handoff-config-proxy:8080"),
        ) as mock_resolver:
            result = resolve_handoff_base_url(
                proxy_id="handoff-config-proxy",
                subprocess_proxy="openrouter-subprocess",
                confirmed_proxy_base_url="http://session-proxy:8084",
            )

        assert result == "http://handoff-config-proxy:8080"
        mock_resolver.assert_called_once_with(
            preferred_proxy="handoff-config-proxy",
            require_route=False,
            use_environment=False,
        )

    def test_subprocess_proxy_miss_falls_back_to_confirmed_proxy(self) -> None:
        """Async handoff remains best-effort if the subprocess proxy is unavailable."""
        with patch(
            "forge.session.handoff_agent.resolve_subprocess_routing",
            return_value=_unresolved_result(),
        ):
            result = resolve_handoff_base_url(
                proxy_id=None,
                subprocess_proxy="missing-subprocess-proxy",
                confirmed_proxy_base_url="http://session-proxy:8084",
            )

        assert result == "http://session-proxy:8084"

    def test_direct_short_circuits_all_resolution(self) -> None:
        """direct=True should return None regardless of other sources."""
        result = resolve_handoff_base_url(
            proxy_id="my-proxy",
            confirmed_proxy_base_url="http://session-proxy:8084",
            env_base_url="http://env-proxy:8085",
            direct=True,
        )
        assert result is None

    def test_delegates_to_shared_resolver(self) -> None:
        """Verifies resolve_subprocess_routing is called with correct params."""
        with patch(
            "forge.session.handoff_agent.resolve_subprocess_routing",
            return_value=_resolved_result(),
        ) as mock_resolver:
            resolve_handoff_base_url(proxy_id="my-proxy")
        mock_resolver.assert_called_once_with(
            preferred_proxy="my-proxy",
            require_route=False,
            use_environment=False,
        )


# ---------------------------------------------------------------------------
# run_handoff_agent
# ---------------------------------------------------------------------------


class TestRunHandoffAgent:
    """Tests for the main agent invocation function."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """Create a minimal workspace with real git repo."""
        import subprocess as sp

        sp.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        sp.run(
            ["git", "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
            cwd=str(tmp_path),
        )
        sp.run(
            ["git", "config", "user.name", "Test"],
            capture_output=True,
            check=True,
            cwd=str(tmp_path),
        )
        # Create transcript
        transcript_rel = ".forge/artifacts/test/transcripts/uuid-123.jsonl"
        transcript_abs = tmp_path / transcript_rel
        entries = [_make_newer_entry(f"req-{i}", "user") for i in range(10)] + [
            _make_newer_entry(f"req-{i}", "assistant") for i in range(10)
        ]
        _write_transcript(transcript_abs, entries)
        # Create a default designated doc so basic tests have something to update
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "state.md").write_text("# State\n")
        return tmp_path

    def _default_docs(self) -> list[DesignatedDoc]:
        return [DesignatedDoc(path="docs/state.md", strategy="project-state")]

    def test_skips_below_min_turns(self, workspace: Path) -> None:
        """Sessions below min_turns threshold are skipped (returns True)."""
        transcript_rel = ".forge/artifacts/test/transcripts/short.jsonl"
        transcript_abs = workspace / transcript_rel
        entries = [
            _make_newer_entry("req-1", "user"),
            _make_newer_entry("req-1", "assistant"),
            _make_newer_entry("req-2", "user"),
            _make_newer_entry("req-2", "assistant"),
        ]
        _write_transcript(transcript_abs, entries)

        config = HandoffConfig(enabled=True, min_turns=5)
        result = run_handoff_agent(
            session_name="test",
            forge_root=workspace,
            transcript_snapshot_rel=transcript_rel,
            config=config,
            designated_docs=self._default_docs(),
        )
        assert result is True  # Skip is not a failure

    @patch("forge.session.handoff_agent.is_claude_available", return_value=False)
    def test_returns_false_when_claude_not_available(self, mock_claude: MagicMock, workspace: Path) -> None:
        """Returns False when claude CLI is not in PATH."""
        config = HandoffConfig(enabled=True, min_turns=1)
        result = run_handoff_agent(
            session_name="test",
            forge_root=workspace,
            transcript_snapshot_rel=".forge/artifacts/test/transcripts/uuid-123.jsonl",
            config=config,
            designated_docs=self._default_docs(),
        )
        assert result is False

    def _run_with_mock_claude(
        self,
        workspace: Path,
        mock_run: MagicMock,
        *,
        project_root: Path | None = None,
        **kwargs: object,
    ) -> bool:
        """Helper: run_handoff_agent with mocked claude."""
        root = project_root if project_root is not None else workspace
        with patch("forge.session.handoff_agent.is_claude_available", return_value=True):
            return run_handoff_agent(
                session_name=kwargs.get("session_name", "test"),  # type: ignore[arg-type]
                forge_root=root,
                transcript_snapshot_rel=kwargs.get(
                    "transcript_snapshot_rel",
                    ".forge/artifacts/test/transcripts/uuid-123.jsonl",
                ),  # type: ignore[arg-type]
                config=kwargs.get("config", HandoffConfig(enabled=True, min_turns=1)),  # type: ignore[arg-type]
                base_url=kwargs.get("base_url"),  # type: ignore[arg-type]
                timeout_seconds=kwargs.get("timeout_seconds", 300),  # type: ignore[arg-type]
                designated_docs=kwargs.get("designated_docs", self._default_docs()),  # type: ignore[arg-type]
            )

    def test_invokes_claude_p_with_correct_args(self, workspace: Path) -> None:
        """Verifies run_claude_session is called with correct prompt, cwd, and timeout."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(
                workspace,
                mock_run,
                timeout_seconds=120,
            )

            assert result is True
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert "test" in args[0]  # prompt is first positional arg
            assert kwargs["cwd"] == str(workspace)
            assert kwargs["timeout_seconds"] == 120

    def test_sets_base_url_when_provided(self, workspace: Path) -> None:
        """Passes base_url to run_claude_session when provided."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            self._run_with_mock_claude(
                workspace,
                mock_run,
                base_url="http://my-proxy:8084",
            )

            _, kwargs = mock_run.call_args
            assert kwargs["base_url"] == "http://my-proxy:8084"

    def test_no_base_url_when_none(self, workspace: Path) -> None:
        """Does not set base_url when not provided."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            self._run_with_mock_claude(workspace, mock_run, base_url=None)

            _, kwargs = mock_run.call_args
            assert kwargs.get("base_url") is None

    def test_handles_timeout(self, workspace: Path) -> None:
        """Returns False when claude -p times out."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(
                stdout="",
                stderr="",
                returncode=-1,
                timed_out=True,
                error="Timed out after 300s",
            )
            result = self._run_with_mock_claude(workspace, mock_run)
            assert result is False

    def test_handles_nonzero_exit(self, workspace: Path) -> None:
        """Returns False when claude -p exits with non-zero code."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="error", returncode=1)

            result = self._run_with_mock_claude(workspace, mock_run)
            assert result is False

    def test_no_fallback_when_no_designated_docs(self, workspace: Path) -> None:
        """Empty/None designated_docs returns True without calling subprocess."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(workspace, mock_run, designated_docs=None)

            assert result is True
            mock_run.assert_not_called()

    def test_no_fallback_when_empty_designated_docs(self, workspace: Path) -> None:
        """Empty list returns True without calling subprocess."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(workspace, mock_run, designated_docs=[])

            assert result is True
            mock_run.assert_not_called()

    def test_transcript_path_absolute_in_prompt(self, workspace: Path) -> None:
        """Transcript path in prompt is absolute (not repo-relative)."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            self._run_with_mock_claude(workspace, mock_run)

            args, _ = mock_run.call_args
            prompt = args[0]
            # Transcript path should be absolute (starts with /)
            assert str(workspace) in prompt

    def test_rejects_unsafe_transcript_path(self, workspace: Path) -> None:
        """Transcript path with unsafe characters is rejected."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(
                workspace,
                mock_run,
                transcript_snapshot_rel=".forge/artifacts/t.jsonl`\nINJECT",
            )

            assert result is False
            mock_run.assert_not_called()

    def test_rejects_traversal_transcript_path(self, workspace: Path) -> None:
        """Transcript path with ../ traversal is rejected."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(
                workspace,
                mock_run,
                transcript_snapshot_rel="../../etc/passwd",
            )

            assert result is False
            mock_run.assert_not_called()

    def test_returns_false_when_transcript_missing(self, workspace: Path) -> None:
        """Returns False when transcript file doesn't exist on disk."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(
                workspace,
                mock_run,
                transcript_snapshot_rel=".forge/artifacts/nonexistent.jsonl",
            )

            assert result is False
            mock_run.assert_not_called()

    def test_rejects_unknown_mode(self, workspace: Path) -> None:
        """Unknown config.mode is rejected (not silently treated as review-only)."""
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)

            result = self._run_with_mock_claude(
                workspace,
                mock_run,
                config=HandoffConfig(enabled=True, min_turns=1, mode="review_only"),
            )

            assert result is False
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _validate_designated_docs
# ---------------------------------------------------------------------------


class TestValidateDesignatedDocs:
    """Tests for the containment guard + strategy consistency checks."""

    def test_accepts_valid_relative_paths(self, tmp_path: Path) -> None:
        """Valid worktree-relative paths pass through."""
        docs = [
            DesignatedDoc(path="docs/checklist.md"),
            DesignatedDoc(path=".forge/memory/project-state.md"),
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 2

    def test_rejects_absolute_paths(self, tmp_path: Path) -> None:
        """Absolute paths are rejected."""
        docs = [DesignatedDoc(path="/etc/passwd")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_traversal_paths(self, tmp_path: Path) -> None:
        """Paths with ../ traversal that escape worktree are rejected."""
        docs = [DesignatedDoc(path="../../etc/passwd")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_mixed_valid_and_invalid(self, tmp_path: Path) -> None:
        """Only valid paths are retained; invalid paths are filtered out."""
        docs = [
            DesignatedDoc(path="docs/good.md"),
            DesignatedDoc(path="/absolute/bad.md"),
            DesignatedDoc(path="../../escape/bad.md"),
            DesignatedDoc(path=".forge/memory/good.md"),
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 2
        assert result[0].path == "docs/good.md"
        assert result[1].path == ".forge/memory/good.md"

    def test_nested_relative_path_within_root(self, tmp_path: Path) -> None:
        """Nested path that stays within worktree is accepted."""
        docs = [DesignatedDoc(path="docs/../docs/checklist.md")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 1

    def test_rejects_prefix_sibling_directory(self, tmp_path: Path) -> None:
        """Path in a sibling directory whose name shares a prefix is rejected.

        Tests the classic str.startswith() footgun: /repo/root2/file
        starts with /repo/root as a string but is NOT contained within it.
        """
        sibling = tmp_path.parent / (tmp_path.name + "2")
        sibling.mkdir(exist_ok=True)
        docs = [DesignatedDoc(path=f"../{sibling.name}/evil.md")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_path_with_backticks(self, tmp_path: Path) -> None:
        """Paths with backticks are rejected (prompt injection via markdown)."""
        docs = [DesignatedDoc(path="docs/a.md`\nINJECT")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_path_with_newlines(self, tmp_path: Path) -> None:
        """Paths with newlines are rejected (prompt injection)."""
        docs = [DesignatedDoc(path="docs/a.md\n## Ignore above")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_path_with_control_chars(self, tmp_path: Path) -> None:
        """Paths with control characters are rejected."""
        docs = [DesignatedDoc(path="docs/a\x00b.md")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_valid_path_with_hyphens_dots_underscores(self, tmp_path: Path) -> None:
        """Normal path characters (hyphens, dots, underscores) are accepted."""
        docs = [DesignatedDoc(path="docs/my-file_v2.0.md")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 1

    # Shadow path validation

    def test_validates_shadows_path_traversal(self, tmp_path: Path) -> None:
        """Traversal in shadows paths is rejected."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="../../etc/passwd",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_validates_shadows_path_absolute(self, tmp_path: Path) -> None:
        """Absolute shadows paths are rejected."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="/etc/passwd",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_validates_shadows_path_unsafe_chars(self, tmp_path: Path) -> None:
        """Unsafe characters in shadows paths are rejected."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="STANDARDS`\nINJECT.md",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_valid_shadow_doc(self, tmp_path: Path) -> None:
        """Valid suggested + shadows combination passes."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested_standards.md",
                strategy="suggested",
                shadows="docs/developer/coding-standards.md",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 1

    # Strategy consistency

    def test_rejects_suggested_without_shadows(self, tmp_path: Path) -> None:
        """strategy=suggested without shadows is rejected."""
        docs = [DesignatedDoc(path=".forge/memory/suggested.md", strategy="suggested")]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_suggested_with_empty_shadows(self, tmp_path: Path) -> None:
        """strategy=suggested with shadows="" is rejected (must be non-empty)."""
        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_shadows_with_non_suggested_strategy(self, tmp_path: Path) -> None:
        """shadows set with a non-suggested strategy is rejected."""
        docs = [
            DesignatedDoc(
                path="docs/checklist.md",
                strategy="checklist",
                shadows="STANDARDS.md",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0

    def test_rejects_self_shadowing(self, tmp_path: Path) -> None:
        """path == shadows is rejected (redundant self-reference)."""
        docs = [
            DesignatedDoc(
                path="docs/standards.md",
                strategy="suggested",
                shadows="docs/standards.md",
            )
        ]
        result = _validate_designated_docs(docs, tmp_path)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Permission-denied detection (QA-038)
# ---------------------------------------------------------------------------


class TestPermissionDeniedDetection:
    """Regression for QA-040: handoff should detect permission-denied stdout."""

    @pytest.mark.parametrize(
        "stdout",
        [
            "I need write permission to modify the file.",
            "I don't have access to edit files in this environment.",
            "I require write permissions to update the document.",
            "I'm not allowed to write or modify files directly.",
            "I cannot write files without the appropriate permissions.",
        ],
    )
    def test_detects_permission_denied(self, stdout):
        assert _stdout_indicates_permission_denied(stdout) is True

    @pytest.mark.parametrize(
        "stdout",
        [
            "Updated docs/state.md with session takeaways.",
            "I wrote the debugging notes to the designated doc.",
            "",
            "No changes needed for this session.",
        ],
    )
    def test_passes_normal_output(self, stdout):
        assert _stdout_indicates_permission_denied(stdout) is False

    def _make_workspace(self, tmp_path):
        import subprocess as sp

        sp.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        sp.run(["git", "config", "user.email", "t@t"], capture_output=True, check=True, cwd=str(tmp_path))
        sp.run(["git", "config", "user.name", "T"], capture_output=True, check=True, cwd=str(tmp_path))
        transcript_rel = ".forge/artifacts/test/transcripts/t.jsonl"
        entries = [_make_newer_entry(f"r-{i}", r) for i in range(5) for r in ("user", "assistant")]
        _write_transcript(tmp_path / transcript_rel, entries)
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "state.md").write_text("# State\n")
        return transcript_rel

    def test_run_handoff_returns_false_on_permission_denied(self, tmp_path):
        """run_handoff_agent returns False when Claude can't write in augment mode."""
        transcript_rel = self._make_workspace(tmp_path)
        mock_result = SessionResult(
            stdout="I need write permission to modify docs/state.md.",
            stderr="",
            returncode=0,
        )
        config = HandoffConfig(enabled=True, min_turns=1, mode="augment")
        with (
            patch("forge.session.handoff_agent.is_claude_available", return_value=True),
            patch("forge.session.handoff_agent.run_claude_session", return_value=mock_result),
        ):
            result = run_handoff_agent(
                session_name="test",
                forge_root=tmp_path,
                transcript_snapshot_rel=transcript_rel,
                config=config,
                designated_docs=[DesignatedDoc(path="docs/state.md", strategy="project-state")],
            )
        assert result is False

    def test_review_only_mode_ignores_permission_patterns(self, tmp_path):
        """review-only mode should not false-fail on 'cannot modify files' responses."""
        transcript_rel = self._make_workspace(tmp_path)
        mock_result = SessionResult(
            stdout="I cannot modify files in this mode. Here are the changes I would make...",
            stderr="",
            returncode=0,
        )
        config = HandoffConfig(enabled=True, min_turns=1, mode="review-only")
        with (
            patch("forge.session.handoff_agent.is_claude_available", return_value=True),
            patch("forge.session.handoff_agent.run_claude_session", return_value=mock_result),
        ):
            result = run_handoff_agent(
                session_name="test",
                forge_root=tmp_path,
                transcript_snapshot_rel=transcript_rel,
                config=config,
                designated_docs=[DesignatedDoc(path="docs/state.md", strategy="project-state")],
            )
        assert result is True


# ---------------------------------------------------------------------------
# run_handoff_agent with designated_docs
# ---------------------------------------------------------------------------


class TestRunHandoffAgentMultiDoc:
    """Tests for run_handoff_agent with designated_docs."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """Create a minimal workspace with transcript."""
        import subprocess as sp

        sp.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        sp.run(
            ["git", "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
            cwd=str(tmp_path),
        )
        sp.run(
            ["git", "config", "user.name", "Test"],
            capture_output=True,
            check=True,
            cwd=str(tmp_path),
        )
        transcript_rel = ".forge/artifacts/test/transcripts/uuid-123.jsonl"
        transcript_abs = tmp_path / transcript_rel
        entries = [_make_newer_entry(f"req-{i}", "user") for i in range(10)] + [
            _make_newer_entry(f"req-{i}", "assistant") for i in range(10)
        ]
        _write_transcript(transcript_abs, entries)
        return tmp_path

    def _run_with_mock_claude(
        self,
        workspace: Path,
        mock_run: MagicMock,
        *,
        project_root: Path | None = None,
        **kwargs: object,
    ) -> bool:
        """Helper: run_handoff_agent with mocked claude."""
        root = project_root if project_root is not None else workspace
        with patch("forge.session.handoff_agent.is_claude_available", return_value=True):
            return run_handoff_agent(
                session_name=kwargs.get("session_name", "test"),  # type: ignore[arg-type]
                forge_root=root,
                transcript_snapshot_rel=kwargs.get(
                    "transcript_snapshot_rel",
                    ".forge/artifacts/test/transcripts/uuid-123.jsonl",
                ),  # type: ignore[arg-type]
                config=kwargs.get("config", HandoffConfig(enabled=True, min_turns=1)),  # type: ignore[arg-type]
                base_url=kwargs.get("base_url"),  # type: ignore[arg-type]
                timeout_seconds=kwargs.get("timeout_seconds", 300),  # type: ignore[arg-type]
                designated_docs=kwargs.get("designated_docs"),  # type: ignore[arg-type]
            )

    def test_uses_multi_doc_prompt_when_designated_docs_provided(self, workspace: Path) -> None:
        """When designated_docs is non-empty, uses build_multi_doc_prompt."""
        (workspace / "docs").mkdir(parents=True, exist_ok=True)
        (workspace / "docs" / "checklist.md").write_text("# Checklist\n")
        (workspace / "docs" / "changelog.md").write_text("# Change Log\n")
        docs = [
            DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
            DesignatedDoc(path="docs/changelog.md", strategy="changelog"),
        ]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            args, _ = mock_run.call_args
            prompt = args[0]
            assert "docs/checklist.md" in prompt
            assert "docs/changelog.md" in prompt
            assert "Mark completed tasks" in prompt

    def test_skips_missing_docs(self, workspace: Path) -> None:
        """Docs whose files don't exist on disk are filtered out."""
        # Only create one of the two files
        (workspace / "docs").mkdir(parents=True, exist_ok=True)
        (workspace / "docs" / "checklist.md").write_text("# Checklist\n")
        docs = [
            DesignatedDoc(path="docs/checklist.md", strategy="checklist"),
            DesignatedDoc(path="docs/missing_checklist.md", strategy="checklist"),
        ]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            args, _ = mock_run.call_args
            prompt = args[0]
            assert "docs/checklist.md" in prompt
            assert "docs/missing_checklist.md" not in prompt

    def test_no_file_creation_for_project_state(self, workspace: Path) -> None:
        """project-state doc that doesn't exist is skipped (no mkdir, no creation)."""
        docs = [DesignatedDoc(path=".forge/memory/project-state.md", strategy="project-state")]
        memory_dir = workspace / ".forge" / "memory"
        assert not memory_dir.exists()

        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            result = self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            # Returns True (skip) and does NOT call subprocess (no docs ready)
            assert result is True
            mock_run.assert_not_called()
            # No directory created
            assert not memory_dir.exists()

    def test_containment_guard_rejects_traversal(self, workspace: Path) -> None:
        """Traversal paths in designated_docs are rejected; returns True (skip)."""
        docs = [DesignatedDoc(path="../../etc/passwd")]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            result = self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            assert result is True
            mock_run.assert_not_called()

    def test_forge_root_cwd_used_for_subprocess(self, workspace: Path) -> None:
        """cwd in run_claude_session uses forge_root."""
        (workspace / "docs").mkdir(parents=True, exist_ok=True)
        (workspace / "docs" / "checklist.md").write_text("# Checklist\n")

        docs = [DesignatedDoc(path="docs/checklist.md", strategy="checklist")]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            _, call_kwargs = mock_run.call_args
            assert call_kwargs["cwd"] == str(workspace)

    def test_doc_existence_checked_against_forge_root(self, workspace: Path) -> None:
        """File existence check uses forge_root — doc missing under forge_root is skipped."""
        # Remove the default checklist file created by workspace fixture
        checklist = workspace / "docs" / "checklist.md"
        if checklist.exists():
            checklist.unlink()

        docs = [DesignatedDoc(path="docs/checklist.md", strategy="checklist")]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            result = self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            # Doc doesn't exist under forge_root → skipped → no subprocess
            assert result is True
            mock_run.assert_not_called()

    def test_skips_shadow_doc_when_official_missing(self, workspace: Path) -> None:
        """Shadow doc is skipped when the official doc (shadows target) doesn't exist."""
        # Create the shadow doc but NOT the official doc
        (workspace / ".forge" / "memory").mkdir(parents=True, exist_ok=True)
        (workspace / ".forge" / "memory" / "suggested.md").write_text("# Suggested\n")
        # STANDARDS.md does NOT exist

        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="STANDARDS.md",
            )
        ]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            result = self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            # Official doc missing → shadow skipped → no subprocess
            assert result is True
            mock_run.assert_not_called()

    def test_shadow_doc_included_when_both_exist(self, workspace: Path) -> None:
        """Shadow doc is included when both shadow and official docs exist."""
        # Create both the shadow doc and the official doc
        (workspace / ".forge" / "memory").mkdir(parents=True, exist_ok=True)
        (workspace / ".forge" / "memory" / "suggested.md").write_text("# Suggested\n")
        (workspace / "STANDARDS.md").write_text("# Standards\n")

        docs = [
            DesignatedDoc(
                path=".forge/memory/suggested.md",
                strategy="suggested",
                shadows="STANDARDS.md",
            )
        ]
        with patch("forge.session.handoff_agent.run_claude_session") as mock_run:
            mock_run.return_value = SessionResult(stdout="", stderr="", returncode=0)
            self._run_with_mock_claude(workspace, mock_run, designated_docs=docs)

            args, _ = mock_run.call_args
            prompt = args[0]
            assert "suggested.md" in prompt
            assert "STANDARDS.md" in prompt
            assert "proposes changes to" in prompt
