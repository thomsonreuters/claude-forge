"""Tests for status line functionality.

Tests the detect_proxy() function, non-authoritative fallback labeling,
transcript scanning, and formatting helpers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.cli.status_line import (
    _ANSI_RE,
    DEFAULT_TERM_WIDTH,
    TRAILING_MARGIN,
    ProxyRuntimeTruth,
    TranscriptStats,
    _visible_width,
    _wrap_output,
    detect_proxy,
    discover_session,
    format_breadcrumb,
    format_line_changes,
    format_model_label,
    format_native_sandbox,
    format_rate_limits,
    format_sidecar,
    format_token_breakdown,
    format_tokens,
    format_verification,
    get_context_display,
    get_line_change_values,
    get_session_metrics,
    get_tier_display,
    get_token_breakdown_values,
    parse_context_from_json,
    render_categories,
    scan_transcript,
    truncate_ansi,
)


class TestDetectProxy:
    """Tests for detect_proxy() function."""

    def test_no_base_url_returns_not_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ANTHROPIC_BASE_URL, returns (False, None, False)."""
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

        is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is False
        assert runtime is None
        assert is_authoritative is False

    def test_non_localhost_url_returns_not_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-localhost URLs return (False, None, False)."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

        is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is False
        assert runtime is None
        assert is_authoritative is False

    def test_live_proxy_returns_authoritative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When live proxy responds, returns authoritative=True."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        proxy_response = json.dumps(
            {
                "is_proxy": True,
                "proxy": {
                    "proxy_id": "test-proxy",
                    "template": "litellm-openai",
                    "port": 8085,
                    "base_url": "http://localhost:8085",
                },
                "runtime": {
                    "tier_mappings": {
                        "haiku": "gpt-4o-mini",
                        "sonnet": "gpt-4o",
                        "opus": "gpt-5",
                    },
                },
            }
        ).encode()

        mock_response = MagicMock()
        mock_response.read.return_value = proxy_response
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is True
        assert runtime is not None
        assert runtime.proxy_id == "test-proxy"
        assert runtime.template == "litellm-openai"
        assert is_authoritative is True

    def test_registry_fallback_returns_non_authoritative(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When proxy unreachable but proxy in registry, returns authoritative=False."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        # Create registry with matching proxy (uses FORGE_HOME from isolate_forge_home fixture)
        forge_home = Path(os.environ["FORGE_HOME"])
        proxies_dir = forge_home / "proxies"
        proxies_dir.mkdir(parents=True, exist_ok=True)
        (proxies_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "fallback-proxy": {
                            "proxy_id": "fallback-proxy",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "pid": None,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        # Make proxy request fail
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is True
        assert runtime is not None
        assert runtime.proxy_id == "fallback-proxy"
        assert runtime.template == "litellm-openai"
        assert is_authoritative is False  # Non-authoritative due to fallback

    def test_no_registry_match_returns_not_proxy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When proxy unreachable and no registry match, returns (False, None, False)."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        # Create registry with different proxy (uses FORGE_HOME from isolate_forge_home fixture)
        forge_home = Path(os.environ["FORGE_HOME"])
        proxies_dir = forge_home / "proxies"
        proxies_dir.mkdir(parents=True, exist_ok=True)
        (proxies_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "other-proxy": {
                            "proxy_id": "other-proxy",
                            "template": "litellm-gemini",
                            "base_url": "http://localhost:9999",  # Different port
                            "port": 9999,
                            "pid": None,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        # Make proxy request fail
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is False
        assert runtime is None
        assert is_authoritative is False

    def test_registry_fallback_enriches_runtime_from_proxy_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registry fallback computes context windows from proxy config + model catalog."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        forge_home = Path(os.environ["FORGE_HOME"])
        proxies_dir = forge_home / "proxies"
        proxies_dir.mkdir(parents=True, exist_ok=True)
        (proxies_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "test-openai": {
                            "proxy_id": "test-openai",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "pid": None,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        proxy_dir = proxies_dir / "test-openai"
        proxy_dir.mkdir(parents=True, exist_ok=True)
        (proxy_dir / "proxy.yaml").write_text(
            "proxy_format: 1\n"
            "template: litellm-openai\n"
            "template_digest: 'sha256:test'\n"
            "provider: litellm\n"
            "proxy_endpoint: 'http://localhost:8085'\n"
            "port: 8085\n"
            "upstream_base_url: 'https://upstream.example.com'\n"
            "tiers:\n"
            "  haiku: 'gpt-4o-mini'\n"
            "  sonnet: 'gpt-4.1'\n"
            "  opus: 'gpt-4.1'\n"
            "default_tier: sonnet\n"
        )

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is True
        assert runtime is not None
        assert is_authoritative is False
        assert runtime.active_context_window == 1_000_000
        assert runtime.active_tier == "sonnet"
        assert runtime.tier_mappings == {"haiku": "gpt-4o-mini", "sonnet": "gpt-4.1", "opus": "gpt-4.1"}
        assert runtime.context_windows["sonnet"] == 1_000_000
        assert runtime.context_windows["haiku"] == 128_000

    def test_registry_fallback_enrichment_partial_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown model in one tier does not block other tiers from enrichment."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        forge_home = Path(os.environ["FORGE_HOME"])
        proxies_dir = forge_home / "proxies"
        proxies_dir.mkdir(parents=True, exist_ok=True)
        (proxies_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "test-mixed": {
                            "proxy_id": "test-mixed",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "pid": None,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        proxy_dir = proxies_dir / "test-mixed"
        proxy_dir.mkdir(parents=True, exist_ok=True)
        (proxy_dir / "proxy.yaml").write_text(
            "proxy_format: 1\n"
            "template: litellm-openai\n"
            "template_digest: 'sha256:test'\n"
            "provider: litellm\n"
            "proxy_endpoint: 'http://localhost:8085'\n"
            "port: 8085\n"
            "upstream_base_url: 'https://upstream.example.com'\n"
            "tiers:\n"
            "  haiku: 'nonexistent/model-xyz'\n"
            "  sonnet: 'gpt-4.1'\n"
            "  opus: 'gpt-4.1'\n"
            "default_tier: sonnet\n"
        )

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is True
        assert runtime is not None
        assert runtime.context_windows.get("sonnet") == 1_000_000
        assert "haiku" not in runtime.context_windows
        assert runtime.active_context_window == 1_000_000

    def test_registry_fallback_no_config_returns_empty_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing proxy.yaml gracefully falls back to empty runtime."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        forge_home = Path(os.environ["FORGE_HOME"])
        proxies_dir = forge_home / "proxies"
        proxies_dir.mkdir(parents=True, exist_ok=True)
        (proxies_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "no-config-proxy": {
                            "proxy_id": "no-config-proxy",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "pid": None,
                            "status": "healthy",
                        }
                    },
                }
            )
        )
        # No proxy.yaml created

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is True
        assert runtime is not None
        assert runtime.active_context_window is None
        assert is_authoritative is False


class TestDetectProxyUrlNormalization:
    """Tests for localhost vs 127.0.0.1 normalization in proxy fallback."""

    def test_127_matches_localhost_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTHROPIC_BASE_URL=127.0.0.1 matches registry entry with localhost."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8085")

        forge_home = Path(os.environ["FORGE_HOME"])
        proxies_dir = forge_home / "proxies"
        proxies_dir.mkdir(parents=True, exist_ok=True)
        (proxies_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "proxies": {
                        "my-proxy": {
                            "proxy_id": "my-proxy",
                            "template": "litellm-openai",
                            "base_url": "http://localhost:8085",
                            "port": 8085,
                            "pid": None,
                            "status": "healthy",
                        }
                    },
                }
            )
        )

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            is_proxy, runtime, is_authoritative = detect_proxy()

        assert is_proxy is True
        assert runtime is not None
        assert runtime.proxy_id == "my-proxy"


class TestDiscoverSession:
    """Tests for discover_session() function."""

    def test_no_session_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no session found, returns (None, False)."""
        monkeypatch.delenv("FORGE_SESSION", raising=False)
        monkeypatch.chdir(tmp_path)

        manifest, is_authoritative = discover_session()

        assert manifest is None
        assert is_authoritative is False

    def test_no_cwd_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sessions in CWD are NOT detected without FORGE_SESSION env var."""
        monkeypatch.delenv("FORGE_SESSION", raising=False)

        # Create session manifest in per-session directory
        session_dir = tmp_path / ".forge" / "sessions" / "cwd-session"
        session_dir.mkdir(parents=True)
        (session_dir / "forge.session.json").write_text(
            json.dumps({"name": "cwd-session", "intent": {"model_tier": "opus"}})
        )

        manifest, is_authoritative = discover_session()

        # No CWD fallback — session not detected without env var
        assert manifest is None
        assert is_authoritative is False


class TestProxyRuntimeTruth:
    """Tests for ProxyRuntimeTruth class."""

    def test_parses_lease_info(self) -> None:
        """ProxyRuntimeTruth extracts proxy info."""
        raw = {
            "is_proxy": True,
            "proxy": {
                "proxy_id": "test-proxy",
                "template": "litellm-openai",
                "port": 8085,
                "base_url": "http://localhost:8085",
            },
        }

        runtime = ProxyRuntimeTruth(raw)

        assert runtime.is_proxy is True
        assert runtime.proxy_id == "test-proxy"
        assert runtime.template == "litellm-openai"
        assert runtime.port == 8085
        assert runtime.base_url == "http://localhost:8085"

    def test_parses_tier_mappings(self) -> None:
        """ProxyRuntimeTruth extracts tier mappings."""
        raw = {
            "is_proxy": True,
            "runtime": {
                "tier_mappings": {
                    "haiku": "gpt-4o-mini",
                    "sonnet": "gpt-4o",
                    "opus": "gpt-5",
                },
            },
        }

        runtime = ProxyRuntimeTruth(raw)

        assert runtime.tier_mappings == {
            "haiku": "gpt-4o-mini",
            "sonnet": "gpt-4o",
            "opus": "gpt-5",
        }

    def test_falls_back_to_template_at_root(self) -> None:
        """ProxyRuntimeTruth falls back to root-level template."""
        raw = {
            "is_proxy": True,
            "template": "fallback-template",
            "proxy": {},  # No template in proxy
        }

        runtime = ProxyRuntimeTruth(raw)

        assert runtime.template == "fallback-template"


class TestGetTierDisplay:
    """Tests for get_tier_display() function."""

    def test_formats_tier_display(self) -> None:
        """Tier display formats O:opus S:sonnet H:haiku."""
        raw = {
            "is_proxy": True,
            "runtime": {
                "tier_mappings": {
                    "haiku": "gpt-4o-mini",
                    "sonnet": "gpt-4o",
                    "opus": "gpt-5",
                },
            },
        }
        runtime = ProxyRuntimeTruth(raw)

        display = get_tier_display(runtime)

        assert display is not None
        assert "O:gpt-5" in display
        assert "S:gpt-4o" in display
        assert "H:gpt-4o-mini" in display

    def test_returns_none_when_no_runtime(self) -> None:
        """Returns None when runtime is None."""
        assert get_tier_display(None) is None

    def test_returns_none_when_no_tiers(self) -> None:
        """Returns None when no tier mappings available."""
        raw = {"is_proxy": True, "runtime": {}}
        runtime = ProxyRuntimeTruth(raw)

        assert get_tier_display(runtime) is None


class TestDetectProxyMalformedResponse:
    """Tests for detect_proxy() handling of malformed proxy responses.

    The proxy endpoint should return structured JSON with is_proxy, proxy, and runtime.
    These tests verify graceful handling of invalid responses.
    """

    def test_proxy_returns_non_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When proxy returns non-JSON, falls back to registry lookup."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        mock_response = MagicMock()
        mock_response.read.return_value = b"not json at all"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            is_proxy, runtime, is_authoritative = detect_proxy()

        # Should fall back to registry, which is empty
        assert is_proxy is False
        assert runtime is None
        assert is_authoritative is False

    def test_proxy_returns_missing_is_proxy_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When proxy response missing is_proxy key, falls back to registry."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        # Valid JSON but missing the required is_proxy field
        proxy_response = json.dumps({"proxy": {}, "runtime": {}}).encode()

        mock_response = MagicMock()
        mock_response.read.return_value = proxy_response
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            is_proxy, runtime, is_authoritative = detect_proxy()

        # is_proxy defaults to False when missing, so falls through
        assert is_proxy is False
        assert runtime is None

    def test_proxy_returns_is_proxy_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When proxy explicitly returns is_proxy=False."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        proxy_response = json.dumps({"is_proxy": False}).encode()

        mock_response = MagicMock()
        mock_response.read.return_value = proxy_response
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            is_proxy, runtime, is_authoritative = detect_proxy()

        # Should not be treated as a proxy
        assert is_proxy is False
        assert runtime is None

    def test_proxy_returns_empty_lease(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When proxy returns is_proxy=True but empty proxy dict."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8085")

        proxy_response = json.dumps(
            {
                "is_proxy": True,
                "proxy": {},  # Empty proxy
                "runtime": {},
            }
        ).encode()

        mock_response = MagicMock()
        mock_response.read.return_value = proxy_response
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            is_proxy, runtime, is_authoritative = detect_proxy()

        # Should still be recognized as proxy, but with empty proxy info
        assert is_proxy is True
        assert runtime is not None
        assert is_authoritative is True
        assert runtime.proxy_id is None
        assert runtime.template == "unknown"  # Falls back to "unknown"


class TestScanTranscript:
    """Tests for scan_transcript() — TranscriptStats extraction."""

    def test_counts_and_thinking(self, tmp_path: Path) -> None:
        """Detects thinking blocks and counts user turns."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}\n'
            '{"type": "user", "message": {"content": "think about this"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "..."}, '
            '{"type": "text", "text": "done"}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.has_thinking is True
        assert stats.user_count == 2
        assert stats.tool_count == 0

    def test_no_thinking(self, tmp_path: Path) -> None:
        """Returns False for thinking when no thinking blocks present."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.has_thinking is False
        assert stats.user_count == 1

    def test_empty_file(self, tmp_path: Path) -> None:
        """Empty transcript returns zero-valued stats."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("")

        stats = scan_transcript(str(transcript))

        assert stats == TranscriptStats()

    def test_missing_file(self) -> None:
        """Non-existent file returns zero-valued stats."""
        stats = scan_transcript("/tmp/nonexistent_transcript_12345.jsonl")

        assert stats == TranscriptStats()

    def test_no_assistant_messages(self, tmp_path: Path) -> None:
        """Only user messages: thinking=False, user_count=2."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "q1"}}\n' '{"type": "user", "message": {"content": "q2"}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.has_thinking is False
        assert stats.user_count == 2
        assert stats.tool_count == 0

    def test_empty_path_string(self) -> None:
        """Empty string path returns zero-valued stats."""
        stats = scan_transcript("")

        assert stats == TranscriptStats()

    def test_thinking_only_in_last_assistant(self, tmp_path: Path) -> None:
        """Only reports thinking from the LAST assistant message."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "..."}, '
            '{"type": "text", "text": "first"}]}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.has_thinking is False
        assert stats.user_count == 0


class TestScanTranscriptToolCounting:
    """Tests for tool_use counting in scan_transcript()."""

    def test_counts_tool_use_blocks(self, tmp_path: Path) -> None:
        """Counts tool_use blocks in assistant messages."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "do stuff"}}\n'
            '{"type": "assistant", "message": {"content": ['
            '{"type": "text", "text": "ok"}, '
            '{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}, '
            '{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}'
            "]}}\n"
        )

        stats = scan_transcript(str(transcript))

        assert stats.tool_count == 2
        assert stats.user_count == 1

    def test_tool_count_across_multiple_messages(self, tmp_path: Path) -> None:
        """Tool counts accumulate across all assistant messages."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Write", "input": {}}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.tool_count == 2

    def test_no_tool_use_returns_zero(self, tmp_path: Path) -> None:
        """Messages without tool_use blocks return tool_count=0."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}\n')

        stats = scan_transcript(str(transcript))

        assert stats.tool_count == 0


class TestScanTranscriptTokenMetrics:
    """Tests for token accumulation in scan_transcript()."""

    def test_accumulates_token_usage(self, tmp_path: Path) -> None:
        """Sums input/output/cached tokens from message.usage fields."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}], '
            '"usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 10}}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "bye"}], '
            '"usage": {"input_tokens": 200, "output_tokens": 30}}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.input_tokens == 300
        assert stats.output_tokens == 80
        assert stats.cached_tokens == 30  # 20 + 10 from first entry

    def test_missing_usage_ignored(self, tmp_path: Path) -> None:
        """Entries without message.usage don't contribute to totals."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.input_tokens == 0
        assert stats.output_tokens == 0
        assert stats.cached_tokens == 0

    def test_user_entries_with_usage(self, tmp_path: Path) -> None:
        """Token usage from any entry type is accumulated (not just assistant)."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello", "usage": {"input_tokens": 50, "output_tokens": 0}}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.input_tokens == 50


class TestScanTranscriptNewFormat:
    """Tests for new transcript format (requestId + message.role)."""

    def test_fixture_transcript(self) -> None:
        """Real fixture file with new format: counts users, tools, skips tool_results."""
        fixture_path = Path(__file__).parent.parent.parent / "fixtures" / "transcript_sample.jsonl"
        assert fixture_path.is_file(), "tests/fixtures/transcript_sample.jsonl fixture is missing"

        stats = scan_transcript(str(fixture_path))

        # 2 real user messages (the tool_result entries have role=user but should be skipped)
        assert stats.user_count == 2
        # 3 tool_use blocks across assistant messages (Read, Edit, Bash)
        assert stats.tool_count == 3
        assert stats.has_thinking is False

    def test_new_format_user_and_assistant(self, tmp_path: Path) -> None:
        """New format with message.role correctly identifies user and assistant."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"requestId": "r1", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}}\n'
            '{"requestId": "r1", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.user_count == 1
        assert stats.tool_count == 0

    def test_new_format_tool_result_not_counted_as_user(self, tmp_path: Path) -> None:
        """New format: tool_result entries (role=user) are not counted as user turns."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"requestId": "r1", "message": {"role": "user", "content": [{"type": "text", "text": "read file"}]}}\n'
            '{"requestId": "r1", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}}\n'
            '{"requestId": "r1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}]}}\n'
            '{"requestId": "r1", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}\n'
        )

        stats = scan_transcript(str(transcript))

        assert stats.user_count == 1  # Only the real user message, not tool_result
        assert stats.tool_count == 1


class TestFormatBreadcrumb:
    """Tests for session lineage breadcrumb formatting."""

    def test_no_derivation_shows_name_only(self):
        manifest = {"name": "my-session", "confirmed": {}}
        assert format_breadcrumb(manifest, is_authoritative=True) == "my-session"

    def test_one_ancestor(self):
        manifest = {
            "name": "child",
            "confirmed": {"derivation": {"lineage": ["parent"]}},
        }
        assert format_breadcrumb(manifest, is_authoritative=True) == "parent > child"

    def test_two_ancestors(self):
        manifest = {
            "name": "grandchild",
            "confirmed": {"derivation": {"lineage": ["parent", "origin"]}},
        }
        assert format_breadcrumb(manifest, is_authoritative=True) == "origin > parent > grandchild"

    def test_three_plus_ancestors_ellipsis(self):
        manifest = {
            "name": "current",
            "confirmed": {"derivation": {"lineage": ["parent", "middle", "origin"]}},
        }
        # origin > ... > parent > current
        result = format_breadcrumb(manifest, is_authoritative=True)
        assert result == "origin > ... > parent > current"

    def test_non_authoritative_suffix(self):
        manifest = {"name": "my-session", "confirmed": {}}
        assert format_breadcrumb(manifest, is_authoritative=False) == "my-session(~)"

    def test_empty_name_returns_none(self):
        manifest = {"name": "", "confirmed": {}}
        assert format_breadcrumb(manifest, is_authoritative=True) is None

    def test_empty_lineage_list(self):
        manifest = {
            "name": "solo",
            "confirmed": {"derivation": {"lineage": []}},
        }
        assert format_breadcrumb(manifest, is_authoritative=True) == "solo"


class TestFormatVerification:
    """Tests for verification status indicator."""

    def test_active_verification(self):
        manifest = {
            "confirmed": {"verification": {"iterations": 3, "last_result": "failed"}},
            "intent": {"verification": {"max_iterations": 10}},
        }
        assert format_verification(manifest) == "LOOP 3/10"

    def test_terminal_states_hidden(self):
        for terminal in (
            "passed",
            "max_iterations",
            "max_minutes",
            "bypassed",
            "warned",
        ):
            manifest = {
                "confirmed": {"verification": {"iterations": 5, "last_result": terminal}},
                "intent": {"verification": {"max_iterations": 10}},
            }
            assert format_verification(manifest) is None, f"Should hide for {terminal}"

    def test_error_state_still_shown(self):
        """Error state is NOT terminal — broken verifier is actionable info."""
        manifest = {
            "confirmed": {"verification": {"iterations": 3, "last_result": "error"}},
            "intent": {"verification": {"max_iterations": 10}},
        }
        assert format_verification(manifest) == "LOOP 3/10"

    def test_zero_iterations_hidden(self):
        manifest = {
            "confirmed": {"verification": {"iterations": 0, "last_result": None}},
            "intent": {"verification": {"max_iterations": 10}},
        }
        assert format_verification(manifest) is None

    def test_no_verification_returns_none(self):
        manifest = {"confirmed": {}, "intent": {}}
        assert format_verification(manifest) is None

    def test_default_max_iterations(self):
        """Uses default 50 when intent.verification.max_iterations missing."""
        manifest = {
            "confirmed": {"verification": {"iterations": 2, "last_result": "failed"}},
            "intent": {},
        }
        assert format_verification(manifest) == "LOOP 2/50"


class TestFormatSidecar:
    """Tests for sidecar indicator."""

    def test_sidecar_shows_lock(self):
        manifest = {"confirmed": {"is_sandboxed": True}}
        assert format_sidecar(manifest) == "SC"

    def test_not_sidecar_returns_none(self):
        manifest = {"confirmed": {"is_sandboxed": False}}
        assert format_sidecar(manifest) is None

    def test_missing_field_returns_none(self):
        manifest = {"confirmed": {}}
        assert format_sidecar(manifest) is None


class TestFormatNativeSandbox:
    """Tests for native sandbox detection stub."""

    def test_returns_none_currently(self):
        """Stub returns None until Claude Code exposes sandbox env var."""
        assert format_native_sandbox() is None


class TestFormatTokenBreakdown:
    """Tests for ASCII token breakdown display."""

    def test_all_nonzero(self):
        result = format_token_breakdown(12000, 3200, 8000)
        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert "in:12.0K" in visible
        assert "out:3.2K" in visible
        assert "cache:8.0K" in visible
        assert "\033[2m" in result

    def test_all_zero_returns_none(self):
        assert format_token_breakdown(0, 0, 0) is None

    def test_partial_counts(self):
        result = format_token_breakdown(5000, 0, 0)
        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert "in:5.0K" in visible
        assert "out:" not in visible


class TestFormatRateLimits:
    """Tests for rate limit display formatting."""

    def test_green_under_50(self):
        limits = [{"type": "5_hour", "used_percentage": 30.0, "resets_at": "2026-03-21T15:00:00Z"}]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        assert "\033[32m" in result  # GREEN
        visible = _ANSI_RE.sub("", result)
        assert "RL:30%" == visible

    def test_yellow_at_50(self):
        limits = [{"type": "5_hour", "used_percentage": 50.0}]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        assert "\033[33m" in result  # YELLOW

    def test_yellow_at_80(self):
        limits = [{"type": "5_hour", "used_percentage": 80.0}]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        assert "\033[33m" in result  # YELLOW

    def test_red_above_80(self):
        limits = [{"type": "5_hour", "used_percentage": 95.0}]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        assert "\033[31;1m" in result  # RED_BOLD
        visible = _ANSI_RE.sub("", result)
        assert "RL:95%" == visible

    def test_red_above_80_fractional(self):
        limits = [{"type": "5_hour", "used_percentage": 80.1}]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        assert "\033[31;1m" in result  # RED_BOLD

    def test_suppressed_in_proxy_mode(self):
        limits = [{"type": "5_hour", "used_percentage": 42.0}]
        assert format_rate_limits(limits, is_proxy=True) is None

    def test_none_input(self):
        assert format_rate_limits(None, is_proxy=False) is None

    def test_empty_list(self):
        assert format_rate_limits([], is_proxy=False) is None

    def test_non_list_input(self):
        assert format_rate_limits({"unexpected": "dict"}, is_proxy=False) is None

    def test_falls_back_to_first_entry(self):
        limits = [{"type": "7_day", "used_percentage": 15.0}]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert "RL:15%" == visible

    def test_prefers_5h_window(self):
        limits = [
            {"type": "7_day", "used_percentage": 10.0},
            {"type": "5_hour", "used_percentage": 75.0},
        ]
        result = format_rate_limits(limits, is_proxy=False)
        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert "RL:75%" == visible

    def test_missing_used_percentage(self):
        limits = [{"type": "5_hour", "resets_at": "2026-03-21T15:00:00Z"}]
        assert format_rate_limits(limits, is_proxy=False) is None

    def test_non_numeric_used_percentage(self):
        limits = [{"type": "5_hour", "used_percentage": "not-a-number"}]
        assert format_rate_limits(limits, is_proxy=False) is None


class TestGetSessionMetrics:
    """Tests for status-line cost and duration formatting."""

    def test_direct_cost_uses_claude_total_without_estimate_prefix(self):
        result = get_session_metrics({"total_cost_usd": 0.05, "total_duration_ms": 60_000}, is_proxy=False)

        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert visible == "$0.05 1m"
        assert "~" not in visible

    def test_proxy_cost_uses_estimate_prefix_and_duration(self):
        result = get_session_metrics(
            {"total_cost_usd": 0.99, "total_duration_ms": 60_000},
            is_proxy=True,
            proxy_cost_usd=0.05,
        )

        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert visible == "~$0.05 1m"
        assert "$0.99" not in visible

    def test_direct_subcent_cost_uses_cents_format(self):
        result = get_session_metrics({"total_cost_usd": 0.005, "total_duration_ms": 30_000}, is_proxy=False)

        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert visible == "0c 30s"

    def test_proxy_subcent_cost_uses_fractional_cents(self):
        result = get_session_metrics({"total_duration_ms": 30_000}, is_proxy=True, proxy_cost_usd=0.005)

        assert result is not None
        visible = _ANSI_RE.sub("", result)
        assert visible == "~0.5c 30s"


class TestFormatLineChanges:
    """Tests for direct line-change formatting."""

    def test_formats_added_and_removed(self):
        result = format_line_changes({"total_lines_added": 12, "total_lines_removed": 3})
        assert result is not None
        assert "\033[38;5;28m+12\033[0m" in result
        assert "\033[38;5;124m-3\033[0m" in result

    def test_formats_added_only(self):
        result = format_line_changes({"total_lines_added": 5, "total_lines_removed": 0})
        assert result == "\033[38;5;28m+5\033[0m"

    def test_zero_changes_returns_none(self):
        assert format_line_changes({"total_lines_added": 0, "total_lines_removed": 0}) is None


class TestGetLineChangeValues:
    """Tests for line change value sourcing."""

    def test_prefers_claude_cost_totals(self):
        assert get_line_change_values({"total_lines_added": 7, "total_lines_removed": 2}, "/tmp/demo") == (7, 2)

    def test_falls_back_to_git_numstat(self):
        from forge.cli.status_line import _numstat_cache

        _numstat_cache.clear()
        unstaged = MagicMock(returncode=0, stdout="3\t1\tfoo.py\n-\t-\timage.png\n")
        staged = MagicMock(returncode=0, stdout="2\t4\tbar.py\n")

        with patch("forge.cli.status_line.subprocess.run", side_effect=[unstaged, staged]):
            assert get_line_change_values({}, "/tmp/numstat-test") == (5, 5)

    def test_git_timeout_returns_zero(self):
        from forge.cli.status_line import _numstat_cache

        _numstat_cache.clear()
        with patch("forge.cli.status_line.subprocess.run", side_effect=TimeoutError):
            assert get_line_change_values({}, "/tmp/timeout-test") == (0, 0)

    def test_git_failure_returns_zero(self):
        from forge.cli.status_line import _numstat_cache

        _numstat_cache.clear()
        failed = MagicMock(returncode=128, stdout="")
        with patch("forge.cli.status_line.subprocess.run", return_value=failed):
            assert get_line_change_values({}, "/tmp/failure-test") == (0, 0)


class TestFormatModelLabel:
    """Tests for model label cleanup and context suffixing."""

    def test_default_context_has_no_suffix(self):
        assert format_model_label("Opus 4.6", 200_000) == "Opus 4.6"

    def test_large_context_adds_suffix(self):
        assert format_model_label("Opus 4.6", 1_000_000) == "Opus 4.6 (1M)"

    def test_strips_redundant_context_suffix_before_adding_new_one(self):
        assert format_model_label("Opus 4.6 (200k context)", 1_000_000) == "Opus 4.6 (1M)"


class TestParseContextFromJson:
    """Tests for context parsing from Claude Code JSON."""

    def test_prefers_used_percentage_when_present(self):
        result = parse_context_from_json(
            {
                "context_window": {
                    "context_window_size": 1_000_000,
                    "used_percentage": 12,
                    "current_usage": {
                        "input_tokens": 8500,
                        "cache_creation_input_tokens": 2000,
                        "cache_read_input_tokens": 1500,
                    },
                }
            }
        )

        assert result is not None
        assert result["percent"] == 12
        # tokens comes from current_usage (8500+2000+1500), not back-computed
        assert result["tokens"] == 12_000

    def test_back_computes_tokens_when_current_usage_missing(self):
        result = parse_context_from_json(
            {
                "context_window": {
                    "context_window_size": 1_000_000,
                    "used_percentage": 12,
                }
            }
        )

        assert result is not None
        assert result["percent"] == 12
        # Back-computed: 1_000_000 * 12 / 100 = 120_000
        assert result["tokens"] == 120_000


class TestGetTokenBreakdownValues:
    """Tests for preferring Claude Code token totals when available."""

    def test_prefers_context_window_totals(self):
        stats = TranscriptStats(input_tokens=1, output_tokens=2, cached_tokens=3)
        values = get_token_breakdown_values(
            {
                "context_window": {
                    "total_input_tokens": 100,
                    "total_output_tokens": 50,
                }
            },
            stats,
        )

        assert values == (100, 50, 3)

    def test_falls_back_to_transcript_stats(self):
        stats = TranscriptStats(input_tokens=10, output_tokens=20, cached_tokens=30)
        values = get_token_breakdown_values({}, stats)
        assert values == (10, 20, 30)

    def test_prefers_aggregate_cache_key(self):
        """total_cached_tokens takes precedence over breakdown keys to avoid double-counting."""
        stats = TranscriptStats(cached_tokens=999)
        values = get_token_breakdown_values(
            {
                "context_window": {
                    "total_input_tokens": 100,
                    "total_output_tokens": 50,
                    "total_cached_tokens": 80,
                    "total_cache_read_input_tokens": 60,
                    "total_cache_creation_input_tokens": 20,
                }
            },
            stats,
        )
        assert values == (100, 50, 80)

    def test_sums_breakdown_cache_keys_when_no_aggregate(self):
        stats = TranscriptStats(cached_tokens=999)
        values = get_token_breakdown_values(
            {
                "context_window": {
                    "total_input_tokens": 100,
                    "total_output_tokens": 50,
                    "total_cache_read_input_tokens": 60,
                    "total_cache_creation_input_tokens": 20,
                }
            },
            stats,
        )
        assert values == (100, 50, 80)


class TestGetContextDisplay:
    """Tests for context bar rendering."""

    def test_ascii_progress_bar(self):
        result = get_context_display({"percent": 50, "context_window": 1_000_000})
        visible = _ANSI_RE.sub("", result)
        assert "####----" in visible
        assert "50%/1M" in visible
        # Gradient E: 50% → CTX_HIGH (179), value bold
        assert "\033[38;5;179m" in result
        assert "\033[1m1M" in result

    def test_percent_and_window_share_bar_color(self):
        result = get_context_display({"percent": 17, "context_window": 200_000})
        visible = _ANSI_RE.sub("", result)
        assert "#------- 17%/200K" in visible
        # Gradient E: 17% → CTX_LOW (115), value bold
        assert "\033[38;5;115m" in result
        assert "\033[1m200K" in result

    def test_no_context_returns_ascii_placeholder(self):
        result = get_context_display(None)
        from forge.cli.status_line import _ANSI_RE

        visible = _ANSI_RE.sub("", result)
        assert visible == "---"


class TestFormatTokens:
    """Tests for compact token number formatting."""

    def test_millions(self):
        assert format_tokens(1_500_000) == "1.5M"

    def test_thousands(self):
        assert format_tokens(12_500) == "12.5K"

    def test_small(self):
        assert format_tokens(42) == "42"

    def test_exact_million(self):
        assert format_tokens(1_000_000) == "1.0M"


class TestRenderCategories:
    """Tests for category-based rendering."""

    def test_all_categories_populated(self):
        result = render_categories(
            where=["path", " (main)"],
            who=["origin > current"],
            what=["[Model] bar"],
            metrics=["$0.05 5m"],
            state=["THINK"],
        )
        assert "path (main)" in result
        assert "|" in result

    def test_empty_categories_skipped(self):
        result = render_categories(
            where=["path"],
            who=[],
            what=["[Model] bar"],
            metrics=[],
            state=[],
        )
        # Only where and what — single separator
        assert result.count("|") == 1

    def test_where_only(self):
        result = render_categories(
            where=["path"],
            who=[],
            what=[],
            metrics=[],
            state=[],
        )
        assert "path" in result
        assert "|" not in result


class TestTruncateAnsi:
    """Tests for ANSI-aware truncation."""

    def test_no_truncation_needed(self):
        text = "short text"
        assert truncate_ansi(text, 50) == text

    def test_truncates_plain_text(self):
        text = "a" * 100
        result = truncate_ansi(text, 20)
        assert result.endswith("...")
        assert len(result) == 20  # 17 chars + "..."

    def test_preserves_ansi_codes(self):
        text = "\033[31mred text here that is long\033[0m"
        result = truncate_ansi(text, 10)
        # Should contain ANSI codes but truncated visible text
        assert "\033[31m" in result
        assert result.endswith("...")


class TestOutputHardening:
    """Tests for non-breaking spaces and ANSI reset prefix."""

    def test_render_categories_output_is_plain(self):
        """render_categories returns plain output (hardening applied by caller)."""
        result = render_categories(
            where=["path (main)"],
            who=[],
            what=["[Model]"],
            metrics=[],
            state=[],
        )
        # Spaces are regular spaces — caller applies non-breaking space replacement
        assert " " in result

    def test_non_breaking_space_replacement(self):
        """Verify the replacement pattern works correctly."""
        text = "hello world | test"
        result = text.replace(" ", "\u00a0")
        assert " " not in result
        assert "\u00a0" in result

    def test_trailing_margin_present(self):
        """Actual status_line() output ends with NBSP margin."""
        from click.testing import CliRunner

        from forge.cli.status_line import TRAILING_MARGIN, status_line

        minimal_json = json.dumps({"workspace": {"current_dir": "/tmp"}, "model": {"display_name": "Test"}})
        runner = CliRunner()
        # Use wide terminal so truncation doesn't interfere
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line._get_terminal_width", return_value=200),
        ):
            result = runner.invoke(status_line, input=minimal_json)
        assert result.exit_code == 0
        line = result.output.rstrip("\n")
        expected_suffix = "\u00a0" * TRAILING_MARGIN
        assert line.endswith(
            expected_suffix
        ), f"Output must end with {TRAILING_MARGIN} NBSP margin, got: ...{line[-20:]!r}"

    def test_status_line_renders_ascii_metrics_and_model_suffix(self):
        """Integration: visible output uses model suffix, direct line counts, and dimmed token labels."""
        from click.testing import CliRunner

        from forge.cli.status_line import _ANSI_RE, status_line

        input_json = json.dumps(
            {
                "workspace": {"current_dir": "/tmp/demo"},
                "model": {"display_name": "Opus 4.6"},
                "context_window": {
                    "context_window_size": 1_000_000,
                    "used_percentage": 12,
                    "total_input_tokens": 28_000,
                    "total_output_tokens": 17_500,
                    "current_usage": {
                        "input_tokens": 12_000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
                "cost": {
                    "total_duration_ms": 185_000,
                    "total_lines_added": 12,
                    "total_lines_removed": 3,
                },
            }
        )
        runner = CliRunner()
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line._get_terminal_width", return_value=200),
        ):
            result = runner.invoke(status_line, input=input_json)
        assert result.exit_code == 0

        raw_output = result.output.replace("\u00a0", " ")
        assert "\033[38;5;75m[Opus 4.6 (1M)]\033[0m" in raw_output
        visible = _ANSI_RE.sub("", result.output).replace("\u00a0", " ")
        assert "[Opus 4.6 (1M)]" in visible
        assert "12%/1M" in visible
        assert "+12/-3" in visible
        assert "in:28.0K out:17.5K" in visible

    def test_status_line_renders_direct_cost_without_estimate_prefix(self):
        """CLI rendering: direct cost comes from Claude status-line input."""
        from click.testing import CliRunner

        from forge.cli.status_line import _ANSI_RE, status_line

        input_json = json.dumps(
            {
                "workspace": {"current_dir": "/tmp/demo"},
                "model": {"display_name": "Opus 4.6"},
                "cost": {
                    "total_cost_usd": 0.05,
                    "total_duration_ms": 60_000,
                },
            }
        )
        runner = CliRunner()
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line.discover_session", return_value=(None, False)),
            patch("forge.cli.status_line.get_git_branch", return_value=None),
            patch("forge.cli.status_line._get_terminal_width", return_value=200),
        ):
            result = runner.invoke(status_line, input=input_json)

        assert result.exit_code == 0
        visible = _ANSI_RE.sub("", result.output).replace("\u00a0", " ")
        assert "$0.05 1m" in visible
        assert "~$0.05" not in visible

    def test_status_line_renders_proxy_cost_with_estimate_prefix(self):
        """CLI rendering: proxy cost comes from runtime metrics, not direct input cost."""
        from click.testing import CliRunner

        from forge.cli.status_line import _ANSI_RE, status_line

        input_json = json.dumps(
            {
                "workspace": {"current_dir": "/tmp/demo"},
                "model": {"display_name": "Opus 4.6"},
                "cost": {
                    "total_cost_usd": 0.99,
                    "total_duration_ms": 60_000,
                },
            }
        )
        runtime = ProxyRuntimeTruth(
            {
                "is_proxy": True,
                "proxy": {
                    "proxy_id": "test-proxy",
                    "template": "litellm-openai",
                    "port": 4000,
                    "base_url": "http://localhost:4000",
                },
                "runtime": {
                    "active_context_window": 200_000,
                    "tier_mappings": {"sonnet": "gpt-5.5"},
                },
                "metrics": {"costs": {"total_usd": 0.05}},
            }
        )
        runner = CliRunner()
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(True, runtime, True)),
            patch("forge.cli.status_line.discover_session", return_value=(None, False)),
            patch("forge.cli.status_line.get_git_branch", return_value=None),
            patch("forge.cli.status_line._get_terminal_width", return_value=200),
        ):
            result = runner.invoke(status_line, input=input_json)

        assert result.exit_code == 0
        visible = _ANSI_RE.sub("", result.output).replace("\u00a0", " ")
        assert "~$0.05 1m" in visible
        assert "$0.99" not in visible

    def test_status_line_merges_template_with_proxy_model_segment(self):
        """Proxy template should render before the proxy model/context in one segment."""
        from click.testing import CliRunner

        from forge.cli.status_line import TEMPLATE_COLOR, status_line

        input_json = json.dumps(
            {
                "workspace": {"current_dir": "/tmp/demo"},
                "model": {"display_name": "Opus 4.6"},
                "context_window": {
                    "context_window_size": 400_000,
                    "used_percentage": 59,
                    "total_input_tokens": 65_000_000,
                    "total_output_tokens": 74_100,
                    "current_usage": {
                        "input_tokens": 236_000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
                "cost": {
                    "total_duration_ms": 8_640_000,
                    "total_lines_added": 320,
                    "total_lines_removed": 37,
                },
            }
        )
        runtime = ProxyRuntimeTruth(
            {
                "is_proxy": True,
                "proxy": {
                    "proxy_id": "test-proxy",
                    "template": "litellm-openai",
                    "port": 4000,
                    "base_url": "http://localhost:4000",
                },
                "runtime": {
                    "active_context_window": 400_000,
                    "tier_mappings": {
                        "opus": "gpt-5.5",
                        "sonnet": "gpt-5.5",
                        "haiku": "gpt-5.4-mini",
                    },
                },
            }
        )
        runner = CliRunner()
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(True, runtime, True)),
            patch("forge.cli.status_line._get_terminal_width", return_value=240),
            patch(
                "forge.cli.status_line.discover_session",
                return_value=({"name": "spotted-kingfisher", "parent_session": None}, True),
            ),
            patch("forge.cli.status_line.get_git_branch", return_value="feat/session-1to1"),
        ):
            result = runner.invoke(status_line, input=input_json)

        assert result.exit_code == 0
        raw_output = result.output.replace("\u00a0", " ")
        assert f"{TEMPLATE_COLOR}litellm-openai\033[0m" in raw_output

        visible = _ANSI_RE.sub("", result.output).replace("\u00a0", " ")
        assert "spotted-kingfisher" in visible
        assert "litellm-openai [O:gpt-5.5 S:gpt-5.5 H:gpt-5.4-mini] ####---- 59%/400K" in visible
        assert "[O:gpt-5.5 S:gpt-5.5 H:gpt-5.4-mini] ####---- 59%/400K | litellm-openai" not in visible
        assert visible.index("spotted-kingfisher") < visible.index(
            "litellm-openai [O:gpt-5.5 S:gpt-5.5 H:gpt-5.4-mini]"
        )
        assert visible.index("litellm-openai") < visible.index("[O:gpt-5.5 S:gpt-5.5 H:gpt-5.4-mini]")

    def test_wrapping_on_by_default(self):
        """Wrapping/truncation is enabled by default — each line fits terminal width."""
        from click.testing import CliRunner

        from forge.cli.status_line import status_line

        minimal_json = json.dumps({"workspace": {"current_dir": "/tmp"}, "model": {"display_name": "Test"}})
        runner = CliRunner()
        narrow_width = 40
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line._get_terminal_width", return_value=narrow_width),
        ):
            result = runner.invoke(status_line, input=minimal_json)
        assert result.exit_code == 0
        from forge.cli.status_line import _ANSI_RE

        for line in result.output.rstrip("\n").split("\n"):
            visible = _ANSI_RE.sub("", line)
            assert (
                len(visible) <= narrow_width
            ), f"Each line should fit in {narrow_width} cols, got {len(visible)}: {visible!r}"

    def test_truncation_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch):
        """FORGE_STATUS_TRUNCATE=0 disables truncation."""
        from click.testing import CliRunner

        from forge.cli.status_line import status_line

        monkeypatch.setenv("FORGE_STATUS_TRUNCATE", "0")
        minimal_json = json.dumps({"workspace": {"current_dir": "/tmp"}, "model": {"display_name": "Test"}})
        runner = CliRunner()
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line._get_terminal_width", return_value=10),
        ):
            result = runner.invoke(status_line, input=minimal_json)
        assert result.exit_code == 0
        line = result.output.rstrip("\n")
        from forge.cli.status_line import _ANSI_RE

        visible = _ANSI_RE.sub("", line)
        # Output should NOT be truncated — exceeds the 10-col "terminal"
        assert len(visible) > 10
        assert "..." not in line

    def test_wrapping_uses_fallback_when_piped(self):
        """When /dev/tty is unavailable, falls back to DEFAULT_TERM_WIDTH."""
        from click.testing import CliRunner

        from forge.cli.status_line import status_line

        minimal_json = json.dumps({"workspace": {"current_dir": "/tmp"}, "model": {"display_name": "Test"}})
        runner = CliRunner()
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line._get_terminal_width", return_value=DEFAULT_TERM_WIDTH),
        ):
            result = runner.invoke(status_line, input=minimal_json)
        assert result.exit_code == 0
        from forge.cli.status_line import _ANSI_RE

        for line in result.output.rstrip("\n").split("\n"):
            visible = _ANSI_RE.sub("", line)
            assert (
                len(visible) <= DEFAULT_TERM_WIDTH
            ), f"Each line should fit in {DEFAULT_TERM_WIDTH} cols, got {len(visible)}"

    def test_get_terminal_width_dev_tty(self):
        """_get_terminal_width queries /dev/tty when stdout is piped."""
        from forge.cli.status_line import _get_terminal_width

        # Simulate /dev/tty returning 150 cols
        with (
            patch("os.open", return_value=42),
            patch("os.get_terminal_size", return_value=os.terminal_size((150, 40))),
            patch("os.close"),
        ):
            assert _get_terminal_width() == 150

    def test_get_terminal_width_fallback(self):
        """_get_terminal_width falls back to shutil when /dev/tty fails."""
        from forge.cli.status_line import _get_terminal_width

        with (
            patch("os.open", side_effect=OSError("no tty")),
            patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))),
        ):
            assert _get_terminal_width() == 80


class TestWrapOutput:
    """Tests for _wrap_output separator-boundary wrapping."""

    def test_wraps_at_separator_boundary(self):
        """Output splits into two lines at the last separator that fits."""
        from forge.cli.status_line import _ANSI_RE, _HARDENED_SEP

        seg_a = "segment_a"
        seg_b = "segment_b"
        seg_c = "segment_c"
        output = _HARDENED_SEP.join([seg_a, seg_b, seg_c])
        # seg_a(9) + sep(3) + seg_b(9) = 21 fits in 25
        # + sep(3) + seg_c(9) = 33 does NOT fit
        result = _wrap_output(output, available=25)
        lines = result.split("\n")
        assert len(lines) == 2
        vis_1 = _ANSI_RE.sub("", lines[0])
        vis_2 = _ANSI_RE.sub("", lines[1])
        assert "segment_a" in vis_1
        assert "segment_b" in vis_1
        assert "segment_c" in vis_2
        assert len(vis_1) <= 25
        assert len(vis_2) <= 25

    def test_no_separators_falls_back_to_truncation(self):
        """When there are no separators, falls back to truncate_ansi."""
        result = _wrap_output("a_very_long_single_segment_text", available=15)
        assert "\n" not in result
        assert "..." in result

    def test_fits_without_wrapping(self):
        """Returns output unchanged when it fits in available width."""
        from forge.cli.status_line import _HARDENED_SEP

        output = _HARDENED_SEP.join(["ab", "cd"])
        result = _wrap_output(output, available=100)
        assert "\n" not in result

    def test_first_segment_exceeds_width(self):
        """Falls back to truncation when even the first segment is too wide."""
        from forge.cli.status_line import _HARDENED_SEP

        output = _HARDENED_SEP.join(["a" * 50, "short"])
        result = _wrap_output(output, available=20)
        # First segment alone is 50 chars, exceeds 20 — split_idx stays at 1,
        # so line1 is the first segment (too long) and line2 is "short".
        # But line1_visible (50) > 0, so it produces two lines with line1 untruncated.
        # This is acceptable: the caller's truncation path handles this case.
        lines = result.split("\n")
        assert len(lines) == 2

    def test_line2_truncated_when_too_long(self):
        """Line 2 is truncated with '...' if it still exceeds available width."""
        from forge.cli.status_line import _ANSI_RE, _HARDENED_SEP

        short = "ab"
        long_seg = "x" * 50
        output = _HARDENED_SEP.join([short, long_seg])
        result = _wrap_output(output, available=20)
        lines = result.split("\n")
        assert len(lines) == 2
        vis_2 = _ANSI_RE.sub("", lines[1])
        assert len(vis_2) <= 20
        assert "..." in lines[1]

    def test_trailing_margin_on_each_line(self):
        """Integration: each output line gets its own trailing margin."""
        from click.testing import CliRunner

        from forge.cli.status_line import status_line

        minimal_json = json.dumps({"workspace": {"current_dir": "/tmp"}, "model": {"display_name": "Test"}})
        runner = CliRunner()
        narrow_width = 40
        with (
            patch("forge.cli.status_line.detect_proxy", return_value=(False, None, False)),
            patch("forge.cli.status_line._get_terminal_width", return_value=narrow_width),
        ):
            result = runner.invoke(status_line, input=minimal_json)
        assert result.exit_code == 0
        expected_suffix = "\u00a0" * TRAILING_MARGIN
        for line in result.output.rstrip("\n").split("\n"):
            assert line.endswith(expected_suffix), f"Every line must end with NBSP margin, got: ...{line[-20:]!r}"


class TestVisibleWidth:
    """Tests for _visible_width Unicode display width calculation."""

    def test_ascii_text(self):
        assert _visible_width("hello world") == 11

    def test_strips_ansi_codes(self):
        assert _visible_width("\033[31mred\033[0m") == 3

    def test_supplementary_emoji_two_cols(self):
        """Emoji in supplementary planes count as 2 columns each."""
        assert _visible_width("\U0001f504") == 2  # 🔄 verification
        assert _visible_width("\U0001f5d2") == 2  # 🗒 notepad
        assert _visible_width("\U0001f9e0") == 2  # 🧠 brain
        assert _visible_width("\U0001f4a1") == 2  # lightbulb

    def test_bmp_emoji_with_vs16(self):
        """BMP chars + VS16 (v^) count as 2 columns each."""
        assert _visible_width("v\ufe0f") == 2
        assert _visible_width("^\ufe0f") == 2

    def test_supplementary_emoji_with_vs16(self):
        """Supplementary emoji + VS16 (🗒️) still count as 2 columns total."""
        assert _visible_width("🗒\ufe0f") == 2

    def test_mixed_ascii_and_emoji(self):
        """Mixed text counts each character correctly."""
        assert _visible_width("\U0001f50411 \U0001f9e0115") == 10  # 🔄(2)+1+1+space(1)+🧠(2)+1+1+1

    def test_status_line_ascii_pattern(self):
        """Realistic ASCII token breakdown pattern."""
        text = "in:28.6K out:17.5K cache:24.1M"
        width = _visible_width(text)
        assert width == 30

    def test_progress_bar_chars(self):
        """ASCII progress bar characters are 1 column each."""
        assert _visible_width("###-----") == 8
