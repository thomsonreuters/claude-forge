"""E2E test: file-based credentials resolve through CredentialManager.

Verifies the full resolution chain:
  credentials.yaml → FileSecretsProvider → ChainSecretsProvider
  → CredentialManager.get_credentials() → provider-specific result.

The slow-marked tests make real network calls to verify authentication.

Markers:
  @pytest.mark.integration — requires real credential file I/O
  @pytest.mark.slow — requires real API keys + network access
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import yaml

from forge.core.auth import ChainSecretsProvider, EnvSecretsProvider
from forge.core.auth.secrets import FileSecretsProvider
from forge.core.llm.credentials import CredentialManager


@pytest.fixture
def isolated_creds() -> Path:
    """Return creds file path inside the autouse-isolated FORGE_HOME."""
    return Path(os.environ["FORGE_HOME"]) / "credentials.yaml"


def _write_creds(path: Path, profile: str, secrets: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "profiles": {profile: secrets}}
    with open(path, "w") as f:
        yaml.safe_dump(data, f)
    os.chmod(str(path), 0o600)


@pytest.mark.integration
class TestCredentialManagerFileResolution:
    """CredentialManager resolves keys from the credential file."""

    @pytest.mark.asyncio
    async def test_anthropic_key_from_file(
        self,
        isolated_creds: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anthropic credentials resolve from file when env var absent."""
        test_key = "sk-ant-integration-test-key-abc123"
        _write_creds(isolated_creds, "default", {"ANTHROPIC_API_KEY": test_key})
        # Set empty instead of delenv — load_dotenv(override=False) won't
        # override existing keys, but would re-insert deleted ones from .env
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(),
        )
        cm = CredentialManager(secrets=secrets)

        creds = await cm.get_credentials("anthropic")
        assert creds["api_key"] == test_key

    @pytest.mark.asyncio
    async def test_litellm_remote_key_from_file(
        self,
        isolated_creds: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LiteLLM remote credentials resolve from file."""
        test_key = "sk-litellm-integration-test"
        test_url = "http://localhost:4000"
        _write_creds(
            isolated_creds,
            "default",
            {
                "LITELLM_API_KEY": test_key,
                "LITELLM_BASE_URL": test_url,
            },
        )
        # Set empty instead of delenv — load_dotenv(override=False) won't
        # override existing keys, but would re-insert deleted ones from .env
        monkeypatch.setenv("LITELLM_API_KEY", "")
        monkeypatch.setenv("LITELLM_BASE_URL", "")

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(),
        )
        cm = CredentialManager(secrets=secrets)

        creds = await cm.get_credentials("litellm_remote")
        assert creds["api_key"] == test_key

    @pytest.mark.asyncio
    async def test_env_overrides_file_in_credential_manager(
        self,
        isolated_creds: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env var takes precedence over credential file in full chain."""
        file_key = "sk-ant-from-file-should-lose"
        env_key = "sk-ant-from-env-should-win"
        _write_creds(isolated_creds, "default", {"ANTHROPIC_API_KEY": file_key})
        monkeypatch.setenv("ANTHROPIC_API_KEY", env_key)

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(),
        )
        cm = CredentialManager(secrets=secrets)

        creds = await cm.get_credentials("anthropic")
        assert creds["api_key"] == env_key

    @pytest.mark.asyncio
    async def test_named_profile_resolves(
        self,
        isolated_creds: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FORGE_PROFILE selects the correct credential profile."""
        default_key = "sk-ant-default-profile"
        work_key = "sk-ant-work-profile"
        data = {
            "version": 1,
            "profiles": {
                "default": {"ANTHROPIC_API_KEY": default_key},
                "work": {"ANTHROPIC_API_KEY": work_key},
            },
        }
        with open(isolated_creds, "w") as f:
            yaml.safe_dump(data, f)
        # Set empty instead of delenv — load_dotenv(override=False) won't
        # override existing keys, but would re-insert deleted ones from .env
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("FORGE_PROFILE", "work")

        secrets = ChainSecretsProvider(
            EnvSecretsProvider(),
            FileSecretsProvider(),
        )
        cm = CredentialManager(secrets=secrets)

        creds = await cm.get_credentials("anthropic")
        assert creds["api_key"] == work_key


class TestRealAuthWithFileCredentials:
    """Real API authentication using file-based credentials.

    These tests require actual API keys stored in the host's
    ~/.forge/credentials.yaml. They make real network calls.

    Run with: pytest -m slow
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_anthropic_api_key_authenticates(self) -> None:
        """Anthropic API key from credential file (or env) is accepted by the API.

        Makes a minimal HTTP request to Anthropic's messages endpoint with
        max_tokens=1 to verify the key is valid without consuming significant usage.
        """
        # Use default() which builds the real chain (env → file)
        CredentialManager.reset_default()
        cm = CredentialManager.default()
        try:
            creds = await cm.get_credentials("anthropic")
        except Exception as e:
            pytest.fail(
                f"Could not resolve Anthropic credentials (env or file): {e}\n"
                "Set ANTHROPIC_API_KEY in env or run 'forge auth login -c anthropic-api'."
            )

        api_key = creds["api_key"]
        assert api_key, "Resolved API key should be non-empty"

        # Minimal messages API call — 1 token, cheapest model
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        # 200 = authenticated. 401 = bad key. 4xx/5xx for other issues.
        assert response.status_code == 200, f"Anthropic API returned {response.status_code}: {response.text[:200]}"
