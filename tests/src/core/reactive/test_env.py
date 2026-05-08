"""Tests for forge.core.reactive.env."""

from __future__ import annotations

from unittest.mock import patch

from forge.core.reactive.env import (
    FORGE_DEPTH_VAR,
    FORGE_MAX_DEPTH,
    build_claude_env,
    can_use_bare,
    get_forge_depth,
    should_spawn_subprocesses,
)


class TestBuildClaudeEnv:
    def test_returns_copy_of_environ(self):
        """Returned dict should not mutate os.environ."""
        env = build_claude_env()
        env["__TEST_KEY__"] = "should_not_leak"
        import os

        assert "__TEST_KEY__" not in os.environ

    def test_sets_anthropic_base_url(self):
        env = build_claude_env(base_url="http://localhost:8085")
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8085"

    def test_no_base_url_preserves_existing(self):
        """When base_url is None, ANTHROPIC_BASE_URL is not injected."""
        with patch.dict("os.environ", {}, clear=True):
            env = build_claude_env()
        assert "ANTHROPIC_BASE_URL" not in env

    def test_extra_vars_override(self):
        env = build_claude_env(extra_vars={"HOME": "/custom", "FOO": "bar"})
        assert env["HOME"] == "/custom"
        assert env["FOO"] == "bar"

    def test_base_url_takes_precedence_over_extra_vars(self):
        """Explicit base_url wins over extra_vars for ANTHROPIC_BASE_URL."""
        env = build_claude_env(
            base_url="http://from-base-url",
            extra_vars={"ANTHROPIC_BASE_URL": "http://from-extra"},
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://from-base-url"

    def test_increments_forge_depth_from_zero(self):
        """Child env gets FORGE_DEPTH=1 when parent has no depth set."""
        with patch.dict("os.environ", {}, clear=True):
            env = build_claude_env()
        assert env[FORGE_DEPTH_VAR] == "1"

    def test_increments_forge_depth_from_existing(self):
        with patch.dict("os.environ", {FORGE_DEPTH_VAR: "1"}, clear=True):
            env = build_claude_env()
        assert env[FORGE_DEPTH_VAR] == "2"

    def test_increments_forge_depth_from_zero_string(self):
        with patch.dict("os.environ", {FORGE_DEPTH_VAR: "0"}, clear=True):
            env = build_claude_env()
        assert env[FORGE_DEPTH_VAR] == "1"

    def test_increments_forge_depth_invalid_treated_as_zero(self):
        """Invalid FORGE_DEPTH → treated as 0, child gets 1."""
        with patch.dict("os.environ", {FORGE_DEPTH_VAR: "abc"}, clear=True):
            env = build_claude_env()
        assert env[FORGE_DEPTH_VAR] == "1"

    def test_extra_vars_forge_depth_is_incremented(self):
        """extra_vars participate in depth calculation but cannot bypass the child increment."""
        env = build_claude_env(extra_vars={FORGE_DEPTH_VAR: "99"})
        assert env[FORGE_DEPTH_VAR] == "100"

    def test_direct_unsets_inherited_proxy_url(self):
        """direct=True removes inherited ANTHROPIC_BASE_URL from parent env."""
        with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "http://proxy:8085"}):
            env = build_claude_env(direct=True)
        assert "ANTHROPIC_BASE_URL" not in env

    def test_direct_without_inherited_url_is_safe(self):
        """direct=True is a no-op when parent has no ANTHROPIC_BASE_URL."""
        with patch.dict("os.environ", {}, clear=True):
            env = build_claude_env(direct=True)
        assert "ANTHROPIC_BASE_URL" not in env

    def test_base_url_takes_precedence_over_direct(self):
        """Explicit base_url wins over direct flag."""
        with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "http://old:8085"}):
            env = build_claude_env(base_url="http://new:8086", direct=True)
        assert env["ANTHROPIC_BASE_URL"] == "http://new:8086"


class TestGetForgeDepth:
    def test_unset_returns_zero(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_forge_depth() == 0

    def test_reads_numeric_value(self):
        assert get_forge_depth({FORGE_DEPTH_VAR: "2"}) == 2

    def test_reads_zero(self):
        assert get_forge_depth({FORGE_DEPTH_VAR: "0"}) == 0

    def test_invalid_string_returns_zero(self):
        assert get_forge_depth({FORGE_DEPTH_VAR: "abc"}) == 0

    def test_negative_clamped_to_zero(self):
        assert get_forge_depth({FORGE_DEPTH_VAR: "-1"}) == 0

    def test_empty_string_returns_zero(self):
        assert get_forge_depth({FORGE_DEPTH_VAR: ""}) == 0

    def test_reads_from_os_environ_by_default(self):
        with patch.dict("os.environ", {FORGE_DEPTH_VAR: "3"}):
            assert get_forge_depth() == 3

    def test_explicit_env_overrides_os_environ(self):
        with patch.dict("os.environ", {FORGE_DEPTH_VAR: "5"}):
            assert get_forge_depth({FORGE_DEPTH_VAR: "2"}) == 2


class TestShouldSpawnSubprocesses:
    def test_true_at_depth_zero(self):
        assert should_spawn_subprocesses({}) is True

    def test_true_at_depth_one(self):
        assert should_spawn_subprocesses({FORGE_DEPTH_VAR: "1"}) is True

    def test_false_at_max_depth(self):
        assert should_spawn_subprocesses({FORGE_DEPTH_VAR: str(FORGE_MAX_DEPTH)}) is False

    def test_false_above_max_depth(self):
        assert should_spawn_subprocesses({FORGE_DEPTH_VAR: str(FORGE_MAX_DEPTH + 1)}) is False

    def test_reads_from_os_environ_by_default(self):
        with patch.dict("os.environ", {FORGE_DEPTH_VAR: str(FORGE_MAX_DEPTH)}):
            assert should_spawn_subprocesses() is False

    def test_invalid_value_allows_spawn(self):
        """Invalid depth → 0 → allow spawn (fail-open)."""
        assert should_spawn_subprocesses({FORGE_DEPTH_VAR: "garbage"}) is True


class TestCanUseBare:
    def test_true_when_api_key_present(self):
        assert can_use_bare({"ANTHROPIC_API_KEY": "sk-test"}) is True

    def test_false_when_api_key_absent(self):
        assert can_use_bare({}) is False

    def test_false_when_api_key_empty(self):
        assert can_use_bare({"ANTHROPIC_API_KEY": ""}) is False

    def test_reads_from_os_environ_by_default(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            assert can_use_bare() is True

    def test_reads_from_os_environ_absent(self):
        with patch.dict("os.environ", {}, clear=True):
            assert can_use_bare() is False
