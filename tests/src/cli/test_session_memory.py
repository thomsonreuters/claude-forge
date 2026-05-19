"""Tests for ``forge session memory`` verbs (D3 from runtime abstraction Phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from forge.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seeded_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Create a session and resolve via FORGE_SESSION env var."""
    from forge.session import IndexStore, SessionStore, create_session_state

    forge_root = tmp_path / "project"
    forge_root.mkdir()
    for rel in (
        "docs/checklist.md",
        "docs/coding-standards.md",
        "docs/c.md",
        "docs/a.md",
        "docs/b.md",
        "docs/same.md",
        "docs/x.md",
        "docs/y.md",
        ".forge/memory/suggested.md",
    ):
        target = forge_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Test doc\n", encoding="utf-8")

    state = create_session_state(
        "s1",
        proxy_template="litellm-openai",
        proxy_base_url="http://localhost:8085",
        worktree_path=str(forge_root),
    )
    state.forge_root = str(forge_root)
    SessionStore(str(forge_root), "s1").write(state)

    index = IndexStore()
    index.add_session(
        name="s1",
        worktree_path=str(forge_root),
        project_root=str(tmp_path),
        forge_root=str(forge_root),
        checkout_root=str(forge_root),
        relative_path=".",
        is_incognito=False,
        is_fork=False,
        parent_session=None,
    )

    monkeypatch.setenv("FORGE_SESSION", "s1")
    monkeypatch.chdir(forge_root)
    return forge_root, "s1"


class TestListDocs:
    def test_empty_initially(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "list-docs"])
        assert result.exit_code == 0, result.output
        assert "No designated memory docs" in result.output

    def test_list_json_empty(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "list-docs", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []


class TestAddDoc:
    def test_add_simple(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "add-doc", "docs/checklist.md", "--strategy", "checklist"])
        assert result.exit_code == 0, result.output

        listed = runner.invoke(main, ["session", "memory", "list-docs", "--json"])
        docs = json.loads(listed.output)
        assert docs == [{"path": "docs/checklist.md", "strategy": "checklist", "shadows": None}]

    def test_add_shadow(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(
            main,
            [
                "session",
                "memory",
                "add-doc",
                ".forge/memory/suggested.md",
                "--strategy",
                "suggested",
                "--shadows",
                "docs/coding-standards.md",
            ],
        )
        assert result.exit_code == 0, result.output

        listed = runner.invoke(main, ["session", "memory", "list-docs", "--json"])
        docs = json.loads(listed.output)
        assert docs == [
            {
                "path": ".forge/memory/suggested.md",
                "strategy": "suggested",
                "shadows": "docs/coding-standards.md",
            }
        ]

    def test_rejects_absolute_path(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "add-doc", "/etc/passwd", "--strategy", "generic"])
        assert result.exit_code != 0
        assert "Invalid path" in result.output

    def test_rejects_missing_doc(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "add-doc", "docs/not-created.md", "--strategy", "generic"])
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_rejects_suggested_without_shadows(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "add-doc", "docs/sugg.md", "--strategy", "suggested"])
        assert result.exit_code != 0
        assert "suggested" in result.output and "shadows" in result.output

    def test_rejects_shadows_without_suggested(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(
            main,
            [
                "session",
                "memory",
                "add-doc",
                "docs/x.md",
                "--strategy",
                "generic",
                "--shadows",
                "docs/y.md",
            ],
        )
        assert result.exit_code != 0
        assert "shadows" in result.output and "suggested" in result.output

    def test_rejects_self_shadow(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(
            main,
            [
                "session",
                "memory",
                "add-doc",
                "docs/same.md",
                "--strategy",
                "suggested",
                "--shadows",
                "docs/same.md",
            ],
        )
        assert result.exit_code != 0
        assert "differ" in result.output

    def test_rejects_duplicate_path(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        runner.invoke(main, ["session", "memory", "add-doc", "docs/c.md", "--strategy", "checklist"])
        result = runner.invoke(main, ["session", "memory", "add-doc", "docs/c.md", "--strategy", "changelog"])
        assert result.exit_code != 0
        assert "already configured" in result.output


class TestRemoveDoc:
    def test_remove_existing(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        runner.invoke(main, ["session", "memory", "add-doc", "docs/c.md", "--strategy", "checklist"])
        result = runner.invoke(main, ["session", "memory", "remove-doc", "docs/c.md"])
        assert result.exit_code == 0, result.output

        listed = runner.invoke(main, ["session", "memory", "list-docs", "--json"])
        assert json.loads(listed.output) == []

    def test_remove_missing(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        result = runner.invoke(main, ["session", "memory", "remove-doc", "docs/not-there.md"])
        assert result.exit_code != 0
        assert "No designated doc" in result.output

    def test_remove_keeps_others(self, runner: CliRunner, seeded_session: tuple[Path, str]) -> None:
        runner.invoke(main, ["session", "memory", "add-doc", "docs/a.md", "--strategy", "generic"])
        runner.invoke(main, ["session", "memory", "add-doc", "docs/b.md", "--strategy", "generic"])
        runner.invoke(main, ["session", "memory", "remove-doc", "docs/a.md"])

        listed = runner.invoke(main, ["session", "memory", "list-docs", "--json"])
        paths = [d["path"] for d in json.loads(listed.output)]
        assert paths == ["docs/b.md"]
