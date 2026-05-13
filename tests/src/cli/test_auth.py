"""Tests for CLI auth commands."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from forge.cli.auth import _mask_value
from forge.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def creds_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point FORGE_HOME to tmp_path so credentials go to a temp file."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    return tmp_path / "credentials.yaml"


# --- Helpers ---


class TestHelpers:

    def test_mask_value_long(self) -> None:
        assert _mask_value("sk-ant-1234567890") == "sk-a…7890"

    def test_mask_value_short(self) -> None:
        assert _mask_value("short") == "****"


# --- Login ---


class TestAuthLogin:

    def test_login_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["auth", "login", "--help"])
        assert result.exit_code == 0
        assert "--credential" in result.output
        assert "--profile" in result.output

    def test_login_stores_credential(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Login prompts and stores value in credentials file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api"],
            input="sk-ant-test-12345\n",
        )

        assert result.exit_code == 0
        assert "Credentials saved" in result.output

        with open(creds_file) as f:
            data = yaml.safe_load(f)
        assert data["profiles"]["default"]["ANTHROPIC_API_KEY"] == "sk-ant-test-12345"

    def test_login_provider_alias_works(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--provider/-p still works as an alias for --credential/-c."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = runner.invoke(
            main,
            ["auth", "login", "-p", "anthropic-api"],
            input="sk-ant-alias-test\n",
        )

        assert result.exit_code == 0
        assert "Credentials saved" in result.output

    def test_login_keeps_existing_on_empty_input(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing Enter keeps the existing value."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"ANTHROPIC_API_KEY": "sk-ant-existing"}, path=creds_file)

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api"],
            input="\n",
        )

        assert result.exit_code == 0

        with open(creds_file) as f:
            data = yaml.safe_load(f)
        assert data["profiles"]["default"]["ANTHROPIC_API_KEY"] == "sk-ant-existing"

    def test_login_with_named_profile(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api", "--profile", "work"],
            input="sk-ant-work-key\n",
        )

        assert result.exit_code == 0
        assert "work" in result.output

        with open(creds_file) as f:
            data = yaml.safe_load(f)
        assert data["profiles"]["work"]["ANTHROPIC_API_KEY"] == "sk-ant-work-key"

    def test_login_no_input_no_existing(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty input with no existing value -> nothing saved."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api"],
            input="\n",
        )

        assert result.exit_code == 0
        assert "No credentials to save" in result.output

    def test_login_recovers_from_corrupt_file(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Login self-heals when credentials file is corrupt YAML."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text("{corrupt yaml: [unterminated")

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api"],
            input="sk-ant-recovery-key\n",
        )

        assert result.exit_code == 0
        assert "corrupt" in result.output.lower()
        assert "Credentials saved" in result.output

        with open(creds_file) as f:
            data = yaml.safe_load(f)
        assert data["profiles"]["default"]["ANTHROPIC_API_KEY"] == "sk-ant-recovery-key"

    def test_login_blocks_on_version_mismatch(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Login refuses to overwrite a future-version credential file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(yaml.dump({"version": 99, "profiles": {"default": {"KEY": "precious"}}}))

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api"],
            input="sk-ant-new-key\n",
        )

        assert result.exit_code != 0
        assert "version 99" in result.output

        raw = yaml.safe_load(creds_file.read_text())
        assert raw["version"] == 99


class TestRetiredNames:

    def test_login_retired_anthropic(self, runner: CliRunner) -> None:
        """Old 'anthropic' name produces migration guidance, not silent accept."""
        result = runner.invoke(main, ["auth", "login", "-c", "anthropic"])
        assert result.exit_code != 0
        assert "anthropic-api" in result.output

    def test_login_retired_litellm_local(self, runner: CliRunner) -> None:
        """Old 'litellm-local' name explains it's not a credential."""
        result = runner.invoke(main, ["auth", "login", "-c", "litellm-local"])
        assert result.exit_code != 0
        assert "not a credential" in result.output
        assert "gemini-api" in result.output

    def test_login_unknown_credential(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["auth", "login", "-c", "bogus"])
        assert result.exit_code != 0
        assert "Unknown credential" in result.output


class TestCredentialMenu:

    def test_menu_shown_without_credential_flag(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without -c, shows numbered menu then prompts for selected credentials."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        # Select credential 2 (anthropic-api), then provide the key
        result = runner.invoke(
            main,
            ["auth", "login"],
            input="2\nsk-ant-menu-test\n",
        )

        assert result.exit_code == 0
        assert "anthropic-api" in result.output
        assert "Forge credentials" in result.output

    def test_menu_all_default(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pressing Enter at the menu (default 'all') prompts for all credentials."""
        for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "LITELLM_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

        # All credentials: Enter for menu default, then empty inputs for each
        result = runner.invoke(
            main,
            ["auth", "login"],
            input="\n" + "\n" * 10,  # all + skip everything
        )

        assert result.exit_code == 0
        assert "openrouter" in result.output
        assert "anthropic-api" in result.output

    def test_env_aware_skip(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When env var is set, prompt shows 'already set via environment variable'."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env")

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "openrouter"],
            input="\n\n",  # skip both vars
        )

        assert result.exit_code == 0
        assert "already set via environment variable" in result.output

    def test_env_ignored_prompt(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With auth_ignore_env, login explains env var is ignored and prompts for file value."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-value")
        monkeypatch.setattr(
            "forge.cli.auth._get_auth_ignore_env",
            lambda: True,
        )

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "anthropic-api"],
            input="sk-ant-file-value\n",
        )

        assert result.exit_code == 0
        assert "auth_ignore_env" in result.output
        assert "Credentials saved" in result.output


# --- Status ---


class TestAuthStatus:

    def test_status_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["auth", "status", "--help"])
        assert result.exit_code == 0
        assert "--profile" in result.output

    def test_status_shows_env_source(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "ANTHROPIC_API_KEY" in result.output
        assert "(env)" in result.output

    def test_status_shows_file_source_with_profile(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"ANTHROPIC_API_KEY": "sk-ant-from-file"}, path=creds_file)

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "ANTHROPIC_API_KEY" in result.output
        assert "(file:default)" in result.output

    def test_status_shows_not_configured(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "not configured" in result.output
        # Never shows "MISSING" — neutral language
        assert "MISSING" not in result.output

    def test_status_dual_view(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Status shows both capability summary and credential details sections."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "Configured capabilities:" in result.output
        assert "Credential details:" in result.output
        assert "Not configured (set up if needed):" in result.output

    def test_status_masks_values(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values are masked in status output -- never shown in full."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-key-12345")

        result = runner.invoke(main, ["auth", "status"])

        assert "sk-ant-super-secret-key-12345" not in result.output
        assert "sk-a" in result.output
        assert "2345" in result.output

    def test_status_with_profile(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)

        from forge.core.auth.credentials_file import save_profile

        save_profile("work", {"LITELLM_API_KEY": "sk-litellm-work"}, path=creds_file)

        result = runner.invoke(main, ["auth", "status", "--profile", "work"])

        assert result.exit_code == 0
        assert "(file:work)" in result.output

    def test_status_survives_corrupt_file(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Status degrades gracefully when credentials file is corrupt."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text("{corrupt yaml: [unterminated")

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "corrupt" in result.output.lower()
        assert "not configured" in result.output

    def test_status_blocks_on_version_mismatch(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Status refuses to proceed with a future-version credential file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(yaml.dump({"version": 99, "profiles": {}}))

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code != 0
        assert "version 99" in result.output

    def test_status_env_ignored(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With auth_ignore_env, env vars show as 'env ignored' in status."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-value")
        monkeypatch.setattr(
            "forge.cli.auth._get_auth_ignore_env",
            lambda: True,
        )

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "env ignored" in result.output

    def test_status_default_value_shown(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENROUTER_BASE_URL shows default value when not configured."""
        monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        result = runner.invoke(main, ["auth", "status"])

        assert result.exit_code == 0
        assert "openrouter.ai/api/v1" in result.output
        assert "(default)" in result.output


# --- Logout ---


class TestAuthLogout:

    def test_logout_removes_profile(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"KEY": "val"}, path=creds_file)

        result = runner.invoke(main, ["auth", "logout", "-y"])

        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_logout_nonexistent_profile(self, runner: CliRunner, creds_file: Path) -> None:
        result = runner.invoke(main, ["auth", "logout", "-y"])

        assert result.exit_code == 0
        assert "not found" in result.output

    def test_logout_confirm_abort(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"KEY": "val"}, path=creds_file)

        result = runner.invoke(main, ["auth", "logout"], input="n\n")

        assert result.exit_code == 0
        assert "Aborted" in result.output

        with open(creds_file) as f:
            data = yaml.safe_load(f)
        assert "default" in data["profiles"]

    def test_logout_with_profile(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.core.auth.credentials_file import save_profile

        save_profile("work", {"KEY": "val"}, path=creds_file)
        save_profile("default", {"KEY": "val"}, path=creds_file)

        result = runner.invoke(main, ["auth", "logout", "--profile", "work", "-y"])

        assert result.exit_code == 0
        assert "Removed" in result.output
        assert "work" in result.output

        with open(creds_file) as f:
            data = yaml.safe_load(f)
        assert "default" in data["profiles"]
        assert "work" not in data["profiles"]


# --- Profiles ---


class TestAuthProfiles:

    def test_profiles_empty(self, runner: CliRunner, creds_file: Path) -> None:
        result = runner.invoke(main, ["auth", "profiles"])
        assert result.exit_code == 0
        assert "No profiles found" in result.output

    def test_profiles_lists_saved(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"KEY_A": "a", "KEY_B": "b"}, path=creds_file)
        save_profile("work", {"KEY_C": "c"}, path=creds_file)

        result = runner.invoke(main, ["auth", "profiles"])

        assert result.exit_code == 0
        assert "default (2 keys)" in result.output
        assert "work (1 keys)" in result.output

    def test_profiles_marks_active(self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FORGE_PROFILE", raising=False)

        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"KEY": "val"}, path=creds_file)
        save_profile("work", {"KEY": "val"}, path=creds_file)

        result = runner.invoke(main, ["auth", "profiles"])

        assert "← active" in result.output
        lines = result.output.strip().split("\n")
        active_lines = [line for line in lines if "← active" in line]
        assert len(active_lines) == 1
        assert "default" in active_lines[0]

    def test_profiles_active_from_env(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORGE_PROFILE", "work")

        from forge.core.auth.credentials_file import save_profile

        save_profile("default", {"KEY": "val"}, path=creds_file)
        save_profile("work", {"KEY": "val"}, path=creds_file)

        result = runner.invoke(main, ["auth", "profiles"])

        lines = result.output.strip().split("\n")
        active_lines = [line for line in lines if "← active" in line]
        assert len(active_lines) == 1
        assert "work" in active_lines[0]


# --- Auth group ---


class TestAuthGroup:

    def test_auth_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output
        assert "status" in result.output
        assert "logout" in result.output
        assert "profiles" in result.output


class TestLitellmRemoteBaseUrl:
    """LITELLM_BASE_URL should be prompted during litellm-remote login."""

    def test_login_prompts_for_base_url(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)

        result = runner.invoke(
            main,
            ["auth", "login", "-c", "litellm-remote"],
            input="my-api-key\nhttps://litellm.example.com\n",
        )
        assert result.exit_code == 0
        creds = yaml.safe_load(creds_file.read_text())
        saved = creds["profiles"]["default"]
        assert saved["LITELLM_API_KEY"] == "my-api-key"
        assert saved["LITELLM_BASE_URL"] == "https://litellm.example.com"

    def test_base_url_not_masked_in_status(
        self, runner: CliRunner, creds_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LITELLM_BASE_URL is a connection value, not a secret -- show it plainly."""
        for key in (
            "LITELLM_BASE_URL",
            "LITELLM_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "OPENROUTER_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        creds_file.write_text(
            yaml.dump(
                {
                    "version": 1,
                    "profiles": {
                        "default": {
                            "LITELLM_API_KEY": "sk-secret-key",
                            "LITELLM_BASE_URL": "https://litellm.example.com",
                        }
                    },
                }
            )
        )
        result = runner.invoke(main, ["auth", "status"])
        assert result.exit_code == 0
        assert "https://litellm.example.com" in result.output
        assert "sk-secret-key" not in result.output
