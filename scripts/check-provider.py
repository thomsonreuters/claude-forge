#!/usr/bin/env -S uv run --group provider-check python
"""Verify provider API access: auth, models, and optional budget info.

Usage:
    scripts/check-provider.py anthropic
    scripts/check-provider.py openai
    scripts/check-provider.py gemini
    scripts/check-provider.py openrouter
    scripts/check-provider.py openrouter --all-models
    scripts/check-provider.py openrouter --model gpt-5
    scripts/check-provider.py all
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

PROVIDERS = ("anthropic", "openai", "gemini", "openrouter")
DEFAULT_MODEL_LIMIT = 40
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
TEST_MAX_TOKENS = 16
TEST_MAX_OUTPUT_TOKENS_RESPONSES = 256  # Responses API counts reasoning toward this budget
OPENROUTER_TEST_MAX_TOKENS = 16


def _header(name: str) -> None:
    print(f"\n{'=' * 60}", flush=True)
    print(f"  {name}", flush=True)
    print(f"{'=' * 60}", flush=True)


def _ok(msg: str) -> None:
    print(f"  [ok] {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr, flush=True)


def _info(msg: str) -> None:
    print(f"  [info] {msg}", flush=True)


def _check_env(var: str) -> str | None:
    val = os.environ.get(var)
    if val:
        masked = val[:8] + "..." + val[-4:]
        _ok(f"{var} set ({masked})")
    else:
        _fail(f"{var} not set")
    return val


# -- Anthropic ----------------------------------------------------------------


def check_anthropic(*, model: str | None = None) -> bool:
    _header("Anthropic")
    key = _check_env("ANTHROPIC_API_KEY")
    if not key:
        return False

    try:
        import anthropic
    except ImportError:
        _fail("anthropic SDK not installed (run: uv sync --group provider-check)")
        return False

    client = anthropic.Anthropic(api_key=key)
    test_model = model or DEFAULT_ANTHROPIC_MODEL

    # Models: list via the models API
    try:
        models_page = client.models.list(limit=100)
        model_ids = sorted(m.id for m in models_page.data)
        if model and model not in model_ids:
            _fail(f"Model {model!r} was not returned by Anthropic models.list.")
            return False
        if model:
            _ok(f"Model {model!r} was returned by Anthropic models.list.")
        else:
            _info(f"Available models ({len(model_ids)}):")
            for mid in model_ids:
                print(f"    {mid}")
    except Exception as e:
        _info(f"Could not list models before smoke test: {e}")

    # Auth check: minimal message
    try:
        resp = client.messages.create(
            model=test_model,
            max_tokens=TEST_MAX_TOKENS,
            messages=[{"role": "user", "content": "Say 'ok'"}],
        )
        _ok(f"Auth verified (model: {test_model}, stop: {resp.stop_reason})")
    except anthropic.AuthenticationError as e:
        _fail(f"Auth failed: {e}")
        return False
    except anthropic.APIError as e:
        _fail(f"API error: {e}")
        return False

    # Usage: token counts from the test call
    if resp.usage:
        _info(f"Test call usage: {resp.usage.input_tokens} in / {resp.usage.output_tokens} out")

    return True


# -- OpenAI --------------------------------------------------------------------


def _print_model_ids(model_ids: list[str], *, all_models: bool, limit: int, label: str) -> None:
    """Print model ids with a predictable truncation policy."""
    shown = model_ids if all_models else model_ids[:limit]
    _info(f"{label} ({len(model_ids)} total; showing {len(shown)}):")
    for model_id in shown:
        print(f"    {model_id}")
    if len(shown) < len(model_ids):
        _info("Use --all-models to print every model id.")


def _smoke_test_openai_model(client, model: str, *, required: bool) -> bool:  # noqa: ANN001  # openai imported lazily
    """Smoke-test `model` against the right OpenAI endpoint.

    Tries /v1/chat/completions first. If the model is not a chat model
    (e.g., GPT-5 family / codex), retries via /v1/responses. Failures are
    reported as _fail when `required=True` (user passed --model) or as
    informational skips otherwise.
    """
    import openai

    def _report_failure(msg: str) -> None:
        if required:
            _fail(msg)
        else:
            _info(f"Completion test skipped: {msg}")

    try:
        chat_resp = client.chat.completions.create(
            model=model,
            max_completion_tokens=TEST_MAX_TOKENS,
            messages=[{"role": "user", "content": "Say 'ok'"}],
        )
        finish = chat_resp.choices[0].finish_reason
        _ok(f"Completion verified via /v1/chat/completions (model: {model}, finish: {finish})")
        if chat_resp.usage:
            _info(
                f"Test call usage: {chat_resp.usage.prompt_tokens} in / "
                f"{chat_resp.usage.completion_tokens} out"
            )
        return True
    except openai.NotFoundError as e:
        if "chat model" not in str(e).lower():
            _report_failure(f"Completion failed: {e}")
            return False
        _info("Model is not a chat model; retrying via /v1/responses (Responses API).")
    except Exception as e:
        _report_failure(f"Completion failed: {e}")
        return False

    try:
        resp = client.responses.create(
            model=model,
            input="Say 'ok'",
            max_output_tokens=TEST_MAX_OUTPUT_TOKENS_RESPONSES,
        )
        _ok(f"Completion verified via /v1/responses (model: {model}, status: {resp.status})")
        if resp.usage:
            _info(
                f"Test call usage: {resp.usage.input_tokens} in / "
                f"{resp.usage.output_tokens} out"
            )
        return True
    except Exception as e:
        _report_failure(f"Completion failed (Responses API): {e}")
        return False


def check_openai(
    *,
    all_models: bool = False,
    model_limit: int = DEFAULT_MODEL_LIMIT,
    model: str | None = None,
) -> bool:
    _header("OpenAI")
    key = _check_env("OPENAI_API_KEY")
    if not key:
        return False

    try:
        import openai
    except ImportError:
        _fail("openai SDK not installed (run: uv sync --group provider-check)")
        return False

    client = openai.OpenAI(api_key=key)
    test_model = model or DEFAULT_OPENAI_MODEL

    # Auth check via models list (cheapest endpoint)
    try:
        models_page = client.models.list()
        model_ids = sorted(m.id for m in models_page.data)
        _ok(f"Auth verified ({len(model_ids)} models available)")

        if model and model not in model_ids:
            _fail(f"Model {model!r} was not returned by OpenAI models.list.")
            return False
        if model:
            _ok(f"Model {model!r} was returned by OpenAI models.list.")
        elif all_models:
            _print_model_ids(model_ids, all_models=True, limit=model_limit, label="OpenAI models")
        else:
            notable = [m for m in model_ids if any(tag in m for tag in ("gpt-4", "gpt-3.5", "o1", "o3", "o4", "codex"))]
            if notable:
                _print_model_ids(
                    notable,
                    all_models=False,
                    limit=model_limit,
                    label="Notable OpenAI models",
                )
    except openai.AuthenticationError as e:
        _fail(f"Auth failed: {e}")
        return False
    except openai.APIError as e:
        _fail(f"API error: {e}")
        return False

    if not _smoke_test_openai_model(client, test_model, required=bool(model)) and model:
        return False

    return True


# -- Gemini --------------------------------------------------------------------


def check_gemini(*, model: str | None = None) -> bool:
    _header("Gemini")
    key = _check_env("GEMINI_API_KEY")
    if not key:
        return False

    try:
        from google import genai
    except ImportError:
        _fail("google-genai SDK not installed (run: uv sync --group provider-check)")
        return False

    client = genai.Client(api_key=key)
    test_model = model or DEFAULT_GEMINI_MODEL

    # Auth check: list models
    try:
        models = list(client.models.list())
        model_names = sorted(m.name for m in models if m.name and "gemini" in m.name.lower())
        _ok(f"Auth verified ({len(model_names)} Gemini models available)")
        if model and model not in model_names and f"models/{model}" not in model_names:
            _fail(f"Model {model!r} was not returned by Gemini models.list.")
            return False
        if model:
            _ok(f"Model {model!r} was returned by Gemini models.list.")
        elif model_names:
            _info(f"Gemini models ({len(model_names)}):")
            for name in model_names:
                print(f"    {name}")
    except Exception as e:
        _fail(f"Auth/model list failed: {e}")
        return False

    # Quick generation test
    try:
        resp = client.models.generate_content(
            model=test_model,
            contents="Say 'ok'",
        )
        _ok(f"Generation verified (model: {test_model})")
        if resp.usage_metadata:
            meta = resp.usage_metadata
            _info(f"Test call usage: {meta.prompt_token_count} in / " f"{meta.candidates_token_count} out")
    except Exception as e:
        if model:
            _fail(f"Generation failed: {e}")
            return False
        _info(f"Generation test skipped: {e}")

    return True


# -- OpenRouter ----------------------------------------------------------------


def check_openrouter(
    *,
    all_models: bool = False,
    model_limit: int = DEFAULT_MODEL_LIMIT,
    model: str | None = None,
) -> bool:
    _header("OpenRouter")
    key = _check_env("OPENROUTER_API_KEY")
    if not key:
        return False

    try:
        import httpx
    except ImportError:
        _fail("httpx not installed (run: uv sync --group provider-check)")
        return False

    headers = {"Authorization": f"Bearer {key}"}

    try:
        resp = httpx.get("https://openrouter.ai/api/v1/key", headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        _ok("Auth verified via /api/v1/key")
        _info(
            "Usage credits: "
            f"{data.get('usage')}; limit: {data.get('limit')}; "
            f"remaining: {data.get('limit_remaining')}; free_tier: {data.get('is_free_tier')}"
        )
    except httpx.HTTPStatusError as e:
        _fail(f"Auth/key check failed: HTTP {e.response.status_code} {e.response.text[:300]}")
        return False
    except Exception as e:
        _fail(f"Auth/key check failed: {e}")
        return False

    model_url = "https://openrouter.ai/api/v1/models/user"
    if model:
        _info(f"Verifying model {model!r} against the effective /models/user list for this API key.")
    else:
        _info("Listing effective OpenRouter model IDs returned by /models/user for this API key.")

    try:
        resp = httpx.get(model_url, headers=headers, timeout=30)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        models_by_id = {m["id"]: m for m in models if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]}
        model_ids = sorted(models_by_id)
        if model and model not in model_ids:
            _fail(f"Model {model!r} is not available under this key's provider, privacy, ZDR, and guardrail policy.")
            return False
        if model:
            _ok(f"Model {model!r} was returned by the effective /models/user list.")
        elif all_models:
            _print_model_ids(model_ids, all_models=True, limit=model_limit, label="OpenRouter models")
        else:
            notable = [
                model_id
                for model_id in model_ids
                if model_id.startswith(("openai/", "anthropic/", "google/", "meta-llama/"))
            ]
            _print_model_ids(
                notable,
                all_models=False,
                limit=model_limit,
                label="Notable OpenRouter models",
            )
        if model:
            architecture = models_by_id[model].get("architecture", {})
            output_modalities = architecture.get("output_modalities", [])
            if "text" not in output_modalities:
                _fail(
                    f"Model {model!r} is available, but does not advertise text output "
                    f"(output_modalities={output_modalities}); skipping chat completion smoke test."
                )
                return False
    except Exception as e:
        _info(f"Could not list OpenRouter models: {e}")
        if model:
            return False

    if model:
        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say 'ok'"}],
                    "max_completion_tokens": OPENROUTER_TEST_MAX_TOKENS,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data.get("choices", [{}])[0]
            _ok(f"Completion verified (model: {model}, finish: {choice.get('finish_reason')})")
            usage = data.get("usage") or {}
            if usage:
                _info(f"Test call usage: {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out")
        except httpx.HTTPStatusError as e:
            _fail(f"Completion failed: HTTP {e.response.status_code} {e.response.text[:300]}")
            return False
        except Exception as e:
            _fail(f"Completion failed: {e}")
            return False

    return True


# -- Main ----------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify provider API access, list models, and check usage.")
    parser.add_argument(
        "provider",
        choices=[*PROVIDERS, "all"],
        help="Provider to check (or 'all')",
    )
    parser.add_argument(
        "--all-models",
        action="store_true",
        help="Print every listed model id instead of a short notable subset.",
    )
    parser.add_argument(
        "--model-limit",
        type=int,
        default=DEFAULT_MODEL_LIMIT,
        help=f"Maximum model ids to print when --all-models is not set (default: {DEFAULT_MODEL_LIMIT}).",
    )
    parser.add_argument(
        "--model",
        help=("Specific model to smoke-test. For OpenRouter this must be returned by the effective /models/user list."),
    )
    args = parser.parse_args()

    if args.provider == "all" and args.model:
        parser.error("--model requires a single provider, not 'all'")

    targets = PROVIDERS if args.provider == "all" else (args.provider,)

    results: dict[str, bool] = {}
    for name in targets:
        if name == "openai":
            results[name] = check_openai(
                all_models=args.all_models,
                model_limit=args.model_limit,
                model=args.model,
            )
        elif name == "openrouter":
            results[name] = check_openrouter(
                all_models=args.all_models,
                model_limit=args.model_limit,
                model=args.model,
            )
        elif name == "anthropic":
            results[name] = check_anthropic(model=args.model)
        elif name == "gemini":
            results[name] = check_gemini(model=args.model)

    # Summary
    print(f"\n{'=' * 60}")
    print("  Summary")
    print(f"{'=' * 60}")
    for name, ok in results.items():
        status = "[ok]" if ok else "[FAIL]"
        print(f"  {status:>8}  {name}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
