"""Proxy orchestration.

This module implements the **active** proxy start workflow:

- Reuse an existing healthy proxy from the proxy registry.
- Adopt a healthy unregistered proxy at the template's default port if it is not registered.
- Otherwise spawn a new proxy process and wait for it to become healthy.

The proxy registry persistence layer lives in `forge.proxy.proxies`.

NOTE: Proxy start is intentionally implemented as a synchronous, CLI-friendly
workflow (blocking + polling). The proxy server itself remains async.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from forge.config import TierOverride, load_config
from forge.config.loader import (
    compute_template_digest,
    get_proxy_file_path,
    template_exists,
    write_proxy_instance_config,
)
from forge.config.schema import (
    BackendDependency,
    ProxyInstanceConfig,
    TierModels,
    TierOverrides,
)
from forge.core.auth.template_secrets import resolve_env_or_credential
from forge.core.paths import get_forge_home
from forge.core.state import now_iso
from forge.proxy.proxies import ProxyEntry, ProxyRegistry, ProxyRegistryStore

logger = logging.getLogger(__name__)


class ProxyStartError(ValueError):
    """Raised when a proxy cannot be started."""


@dataclass
class TierOverrideOptions:
    """CLI options for per-tier hyperparameter overrides.

    These override the template defaults when starting a proxy.
    None means "use template default".
    """

    haiku_reasoning_effort: str | None = None
    sonnet_reasoning_effort: str | None = None
    opus_reasoning_effort: str | None = None
    haiku_temperature: float | None = None
    sonnet_temperature: float | None = None
    opus_temperature: float | None = None


@dataclass(frozen=True)
class ProxyStartResult:
    proxy: ProxyEntry
    source: str  # "reuse" | "adopt" | "spawn"


@dataclass(frozen=True)
class PruneStaleProxiesResult:
    pruned_proxy_ids: list[str]
    deleted_overlay_dirs: list[str]
    delete_errors: list[tuple[str, str]]


def _get_proxy_overlay_dir(proxy_id: str) -> Path:
    """Get the proxy overlay directory path."""
    return get_forge_home() / "proxies" / proxy_id


def _tier_override_to_dict(override: TierOverride | None) -> dict[str, Any] | None:
    """Convert TierOverride to dict, excluding None values."""
    if override is None:
        return None
    d = asdict(override)
    return {k: v for k, v in d.items() if v is not None} or None


def create_proxy_file(
    *,
    proxy_id: str,
    template: str,
    base_url: str,
    port: int,
    cli_overrides: TierOverrideOptions | None = None,
    upstream_base_url: str | None = None,
) -> Path:
    """Create a full proxy.yaml file from template + CLI overrides.

    The user owns the entire file (no runtime merge with template).
    File is created at ~/.forge/proxies/<proxy_id>/proxy.yaml.

    Args:
        proxy_id: The proxy identifier.
        template: The template name to copy configuration from.
        base_url: The proxy base URL (this proxy's own endpoint).
        port: The proxy port.
        cli_overrides: Optional CLI overrides to apply on top of template defaults.
        upstream_base_url: Explicit upstream LiteLLM URL. If not provided,
            resolved from env vars or backend_dependency.

    Returns:
        Path to the created proxy.yaml file.
    """
    cfg = load_config(template=template)
    provider = cfg.proxy.get_provider()
    provider_name = cfg.proxy.preferred_provider or "litellm"

    template_digest = compute_template_digest(template)

    tiers = TierModels(
        haiku=provider.tiers.haiku,
        sonnet=provider.tiers.sonnet,
        opus=provider.tiers.opus,
    )

    # Build tier_overrides, merging template defaults with CLI overrides
    template_overrides = provider.tier_overrides

    def _build_tier_override(tier_name: str) -> TierOverride | None:
        template_tier = template_overrides.get(tier_name)
        tier_dict = _tier_override_to_dict(template_tier) or {}

        # Apply CLI overrides if provided
        if cli_overrides:
            reasoning = getattr(cli_overrides, f"{tier_name}_reasoning_effort", None)
            if reasoning is not None:
                tier_dict["reasoning_effort"] = reasoning

            temp = getattr(cli_overrides, f"{tier_name}_temperature", None)
            if temp is not None:
                tier_dict["temperature"] = temp

        return TierOverride(**tier_dict) if tier_dict else None

    tier_overrides = TierOverrides(
        haiku=_build_tier_override("haiku"),
        sonnet=_build_tier_override("sonnet"),
        opus=_build_tier_override("opus"),
    )

    # Build provider_settings from template (e.g., openai_api_mode, error_hints)
    provider_settings: dict[str, Any] = {}
    if hasattr(provider, "openai_api_mode") and provider.openai_api_mode != "auto":
        provider_settings["openai_api_mode"] = provider.openai_api_mode
    if hasattr(provider, "error_hints") and provider.error_hints:
        provider_settings["error_hints"] = True

    # Resolve upstream base_url: explicit arg > template > env/credential file > backend port
    resolved_upstream = upstream_base_url or provider.base_url
    is_local_template = cfg.proxy.backend_dependency is not None
    if not resolved_upstream:
        dep = cfg.proxy.backend_dependency
        if is_local_template and dep is not None:
            resolved_upstream = resolve_env_or_credential("LITELLM_LOCAL_BASE_URL") or ""
            if not resolved_upstream and dep.port:
                resolved_upstream = f"http://localhost:{dep.port}"
        else:
            resolved_upstream = resolve_env_or_credential("LITELLM_BASE_URL") or ""
    if not resolved_upstream:
        raise ProxyStartError(
            f"Template '{template}' has no upstream URL configured.\n"
            f"Use: forge proxy create {template} --base-url https://your-litellm-server/\n"
            f"Or store it: forge auth login -c litellm-remote"
        )

    proxy_config = ProxyInstanceConfig(
        proxy_format=1,
        template=template,
        template_digest=template_digest,
        provider=provider_name,
        proxy_endpoint=base_url,
        port=port,
        upstream_base_url=resolved_upstream,
        tiers=tiers,
        family=cfg.proxy.family,
        tier_overrides=tier_overrides,
        model_alternatives=provider.model_alternatives,
        default_tier=cfg.proxy.default_tier or "sonnet",
        provider_settings=provider_settings,
        created_at=now_iso(),
    )

    return write_proxy_instance_config(proxy_id, proxy_config)


def prune_stale_proxies(*, timeout_s: float = 5.0) -> PruneStaleProxiesResult:
    """Prune stale proxy entries and delete their overlay directories.

    Stale definition (normative):
    - Only proxies with pid != None are eligible (Forge-spawned)
    - A proxy is stale if its pid is no longer running

    This function is intentionally best-effort:
    - It always prunes the registry first (under lock)
    - Overlay directory deletion happens afterward (no lock held)
    - Overlay deletion errors are recorded and do not cause failure
    """

    store = ProxyRegistryStore()
    pruned_ids = store.prune_dead_pids(timeout_s=timeout_s)

    deleted_dirs: list[str] = []
    delete_errors: list[tuple[str, str]] = []

    # Proxy overlays live under ~/.forge/proxies/<proxy_id>/ (sibling of index.json)
    for proxy_id in pruned_ids:
        overlay_dir = store.registry_path.parent / proxy_id
        if not overlay_dir.exists():
            continue

        try:
            shutil.rmtree(overlay_dir)
            deleted_dirs.append(str(overlay_dir))
        except OSError as e:
            delete_errors.append((proxy_id, str(e)))

    return PruneStaleProxiesResult(
        pruned_proxy_ids=pruned_ids,
        deleted_overlay_dirs=deleted_dirs,
        delete_errors=delete_errors,
    )


def _has_env_var(var_name: str) -> bool:
    """Check if environment variable is set.

    Note: load_config() already loads .env files via load_dotenv(),
    so checking os.environ is sufficient after config is loaded.
    """
    return var_name in os.environ


def _ensure_template_credentials(template: str) -> None:
    """Fail fast if template secret credentials are missing.

    Only checks secret env vars (API keys), not connection values
    like LITELLM_BASE_URL that can come from CLI --base-url or
    persisted proxy config. Runs on the spawn path only — after
    reuse/adoption checks pass.
    """
    from forge.core.auth.capabilities import (
        credential_for_env_var,
        credentials_for_template,
        format_missing_credential_error,
    )
    from forge.core.auth.template_secrets import TEMPLATE_SECRETS

    required = TEMPLATE_SECRETS.get(template, [])
    if not required:
        return

    # Only check secret vars (API keys). Connection values (base URLs)
    # may come from CLI args, proxy config, or backend_dependency.
    missing: list[str] = []
    for var_name in required:
        cred = credential_for_env_var(var_name)
        if cred:
            ev = next((ev for ev in cred.env_vars if ev.name == var_name), None)
            if ev and ev.connection_value:
                continue
        if not resolve_env_or_credential(var_name):
            missing.append(var_name)

    if not missing:
        return

    try:
        from forge.runtime_config import get_runtime_config

        env_ignored = get_runtime_config().auth_ignore_env
    except Exception as e:
        logger.debug("Could not read auth_ignore_env; formatting credential error without env-ignored note: %s", e)
        env_ignored = False

    creds = credentials_for_template(template)
    if creds:
        msg = format_missing_credential_error(
            creds[0], missing_vars=missing, template=template, env_ignored=env_ignored
        )
        raise ProxyStartError(msg)

    raise ProxyStartError(
        f"Template '{template}' requires credentials: {', '.join(missing)}\n"
        f"Tip: Run 'forge auth login' to store them, or add to .env / shell exports."
    )


def _ensure_dependency_backend(backend_dep: BackendDependency, template: str) -> None:
    """Ensure dependency backend is running before starting proxy.

    Auto-creates backend config if missing, then starts backend.
    Runs during start_proxy(), NOT during create.

    Args:
        backend_dep: Backend dependency declaration from template
        template: Template name (for error messages)

    Raises:
        ProxyStartError: If backend config creation fails, env vars missing, or backend fails to start
    """
    from forge.backend import BackendManager
    from forge.backend.adapters import get_adapter
    from forge.backend.creation import (
        create_backend_config,
        get_backend_config_path,
        is_backend_config_outdated,
    )
    from forge.backend.registry import BackendRegistryStore

    console = Console(width=200)

    backend_id = f"{backend_dep.adapter}-{backend_dep.port}"

    backend_registry = BackendRegistryStore()
    backend_manager = BackendManager(backend_registry)
    backend_manager.register_adapter(backend_dep.adapter, get_adapter(backend_dep.adapter))

    backend_config = get_backend_config_path(backend_dep.adapter)

    if not backend_config.exists():
        # Auto-create backend config (copy from defaults/backends/, first use)
        console.print(f"[dim]Creating backend config for '{backend_dep.adapter}' (first use)...[/dim]")
        try:
            create_backend_config(adapter_type=backend_dep.adapter)
            console.print(f"[green]✓[/green] Backend config created at {backend_config}")
        except Exception as e:
            raise ProxyStartError(f"Failed to create backend config: {e}")
    else:
        # Config exists — check if default has been updated (new models available)
        if is_backend_config_outdated(backend_dep.adapter):
            console.print(
                f"[yellow]⚠︎[/yellow]  Backend config differs from defaults (new models may be available).\n"
                f"[dim]Tip: Delete {backend_config} and restart to get latest defaults.[/dim]"
            )

    missing = [k for k in backend_dep.required_env_vars if not resolve_env_or_credential(k)]
    if missing:
        from forge.core.auth.capabilities import (
            credentials_for_template,
            format_missing_credential_error,
        )

        try:
            from forge.runtime_config import get_runtime_config

            env_ignored = get_runtime_config().auth_ignore_env
        except Exception as e:
            logger.debug("Could not read auth_ignore_env; formatting credential error without env-ignored note: %s", e)
            env_ignored = False

        creds = credentials_for_template(template)
        if creds:
            raise ProxyStartError(
                format_missing_credential_error(
                    creds[0], missing_vars=missing, template=template, env_ignored=env_ignored
                )
            )
        raise ProxyStartError(
            f"Template '{template}' requires credentials: {', '.join(missing)}\n"
            f"Tip: Run 'forge auth login' to store them, or add to .env / shell exports."
        )

    # Inject credential-file values into os.environ for the backend subprocess
    # (LiteLLM adapter copies os.environ when spawning).
    # When auth_ignore_env is active, override even when env var is present
    # so the subprocess uses the credential-file value, not the ignored env var.
    try:
        from forge.runtime_config import get_runtime_config

        ignore_env = get_runtime_config().auth_ignore_env
    except Exception as e:
        logger.debug("Could not read auth_ignore_env; using environment credentials for backend subprocess: %s", e)
        ignore_env = False

    _SENTINEL = object()
    originals: dict[str, str | object] = {}
    for key in backend_dep.required_env_vars:
        if ignore_env or not os.environ.get(key):
            val = resolve_env_or_credential(key)
            if val:
                originals[key] = os.environ.get(key, _SENTINEL)
                os.environ[key] = val

    try:
        result = backend_manager.ensure_backend(backend_id, backend_dep.adapter, backend_dep.port)
        if result.source == "start":
            console.print(f"[green]✓[/green] Backend '{backend_id}' started on port {backend_dep.port}")
        else:
            console.print(f"[dim]Backend '{backend_id}' already running on port {backend_dep.port}[/dim]")
    except Exception as e:
        raise ProxyStartError(
            f"Failed to start dependency backend for '{template}': {e}\n"
            f"Backend: {backend_dep.adapter} on port {backend_dep.port}"
        )
    finally:
        for key, original in originals.items():
            if original is _SENTINEL:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(original)


def start_proxy(
    *,
    template: str,
    host: str = "localhost",
    proxy_id: str | None = None,
    port: int | None = None,
    timeout_s: float = 10.0,
    max_port_attempts: int = 20,
    tier_overrides: TierOverrideOptions | None = None,
    skip_proxy_file: bool = False,
    upstream_base_url: str | None = None,
) -> ProxyStartResult:
    """Start a proxy for the given template.

    Semantics:
    1) Reuse a registered healthy proxy (by proxy_id if given, otherwise any for the template).
    2) Adopt a healthy unregistered proxy running at the target port if not registered.
    3) Spawn a new proxy process and register it.

    When spawning a new proxy, creates a proxy config at
    ~/.forge/proxies/<proxy_id>/proxy.yaml with tier_overrides from the
    template, merged with any CLI overrides (unless skip_proxy_file=True).

    Args:
        template: Proxy template (e.g., "litellm-openai"). Must match an existing template overlay.
        host: Host to bind the proxy to and to connect healthchecks to.
        proxy_id: Optional proxy identity. When given, reuse checks only this ID (not any
            template proxy), and spawn uses this ID instead of generating one.
        port: Optional port. When given, used directly (no port scan). Fails loudly if in use.
        timeout_s: Total time to wait for the proxy to become healthy.
        max_port_attempts: Upper bound for port scanning (only used when port is None).
        tier_overrides: Optional per-tier hyperparameter overrides from CLI.
        skip_proxy_file: If True, skip creating proxy.yaml (for start_cmd where file exists).

    Returns:
        ProxyStartResult containing the proxy entry and the start source.

    Raises:
        ProxyStartError: On invalid template, no ports available, proxy start failure, timeout, etc.
        ProxyRegistryCorruptedError: If the registry exists but cannot be parsed.
    """

    _validate_template_exists(template)

    cfg = load_config(template=template)

    store = ProxyRegistryStore()
    registry = store.read()  # May raise ProxyRegistryCorruptedError

    # 1) Reuse a registered healthy proxy.
    #    Skip template-wide reuse when an explicit upstream URL is requested,
    #    since an existing proxy may point at a different gateway.
    reused = None
    if not upstream_base_url:
        reused = _try_reuse_registered_proxy(
            registry=registry,
            template=template,
            proxy_id=proxy_id,
            timeout_s=min(1.0, timeout_s),
        )
    if reused is not None:
        # Persist best-effort status updates without clobbering concurrent writers.
        def _persist_reuse(r: ProxyRegistry) -> None:
            entry = r.proxies.get(reused.proxy_id)
            if entry is None:
                return
            entry.last_seen_at = reused.last_seen_at
            entry.status = reused.status

        store.update(timeout_s=5.0, mutate=_persist_reuse)
        return ProxyStartResult(proxy=reused, source="reuse")

    target_port = port if port is not None else _get_template_default_port(template)
    target_base_url = _base_url(host, target_port)

    # 2) Adopt a healthy unregistered proxy at the target port if it is not registered.
    #    If another Forge-managed proxy already owns the port, do not alias it
    #    under a new proxy_id. That would let one FORGE_HOME silently point at
    #    another home's proxy process and later fail identity checks.
    #    Runs entirely under lock to prevent TOCTOU races: two concurrent callers
    #    could both health-check the same orphan and create duplicate entries.
    adopted: ProxyEntry | None = None
    health_timeout = min(1.0, timeout_s)

    def _try_adopt_under_lock(r: ProxyRegistry) -> None:
        nonlocal adopted
        already_registered = any(
            entry.template == template and entry.base_url == target_base_url for entry in r.proxies.values()
        )
        if already_registered:
            return

        if not check_proxy_health(
            base_url=target_base_url,
            expected_template=template,
            timeout_s=health_timeout,
            require_unregistered=True,
        ):
            return

        now = now_iso()
        entry = ProxyEntry(
            proxy_id=proxy_id or _new_proxy_id(set(r.proxies.keys())),
            template=template,
            base_url=target_base_url,
            port=target_port,
            pid=None,
            created_at=now,
            last_seen_at=now,
            status="healthy",
        )
        r.proxies[entry.proxy_id] = entry
        adopted = entry

    # Lock timeout must exceed health check timeout (held inside lock)
    store.update(timeout_s=health_timeout + 5.0, mutate=_try_adopt_under_lock)
    if adopted is not None:
        return ProxyStartResult(proxy=adopted, source="adopt")

    # 3) Spawn a new proxy process.
    # Dependency backend + credential preflights run here (not earlier)
    # so reuse/adopt paths aren't blocked by missing credentials or
    # backend state in the current shell.
    if cfg.proxy.backend_dependency:
        _ensure_dependency_backend(cfg.proxy.backend_dependency, template)
    _ensure_template_credentials(template)

    # Port selection: honor explicit port or scan for available
    if port is not None:
        if _is_port_in_use(target_port):
            raise ProxyStartError(
                f"Port {target_port} is already in use and could not be adopted. "
                f"Stop the process using that port or choose a different one."
            )
        spawn_port = target_port
    else:
        start_port = target_port
        if _is_port_in_use(start_port):
            start_port = target_port + 1
        spawn_port = _find_available_port(start_port=start_port, max_attempts=max_port_attempts)

    base_url = _base_url(host, spawn_port)

    # ID selection: honor explicit proxy_id or generate one
    actual_proxy_id = proxy_id or _new_proxy_id(set(registry.proxies.keys()))

    # Create full proxy file (user owns the entire config)
    # Do this before spawning so the proxy can load it on startup
    # Skip when starting an existing proxy (start_cmd) to preserve user edits
    if not skip_proxy_file:
        create_proxy_file(
            proxy_id=actual_proxy_id,
            template=template,
            base_url=base_url,
            port=spawn_port,
            cli_overrides=tier_overrides,
            upstream_base_url=upstream_base_url,
        )

    # Register proxy BEFORE spawning so startup validation passes (B2.1.3)
    # Server validates that proxy_id exists in registry on startup
    now = now_iso()
    starting_proxy = ProxyEntry(
        proxy_id=actual_proxy_id,
        template=template,
        base_url=base_url,
        port=spawn_port,
        pid=None,  # Not known yet
        created_at=now,
        last_seen_at=None,
        status="starting",
    )

    def _register_starting(r: ProxyRegistry) -> None:
        r.proxies[actual_proxy_id] = starting_proxy

    store.update(timeout_s=5.0, mutate=_register_starting)

    proc, stderr_capture = _spawn_proxy_process(
        template=template,
        host=host,
        port=spawn_port,
        proxy_id=actual_proxy_id,
        provider=cfg.proxy.preferred_provider,
    )
    try:
        _wait_until_healthy(
            base_url=base_url,
            expected_template=template,
            proc=proc,
            stderr_capture=stderr_capture,
            timeout_s=timeout_s,
            expected_proxy_id=actual_proxy_id,
        )
    except Exception:
        _terminate_process(proc)
        # Clean up stderr capture on failure
        if stderr_capture.exists():
            try:
                stderr_capture.unlink()
            except Exception:
                pass
        # Clean up the proxy directory AND registry entry on failure
        # Only clean proxy dir if we created it (not skip_proxy_file)
        if not skip_proxy_file:
            proxy_dir = get_proxy_file_path(actual_proxy_id).parent
            if proxy_dir.exists():
                shutil.rmtree(proxy_dir, ignore_errors=True)

        def _remove_failed(r: ProxyRegistry) -> None:
            r.proxies.pop(actual_proxy_id, None)

        store.update(timeout_s=5.0, mutate=_remove_failed)
        raise

    healthy_proxy = ProxyEntry(
        proxy_id=actual_proxy_id,
        template=template,
        base_url=base_url,
        port=spawn_port,
        pid=proc.pid,
        created_at=now,
        last_seen_at=now_iso(),
        status="healthy",
    )

    def _mark_healthy(r: ProxyRegistry) -> None:
        r.proxies[actual_proxy_id] = healthy_proxy

    store.update(timeout_s=5.0, mutate=_mark_healthy)
    return ProxyStartResult(proxy=healthy_proxy, source="spawn")


def _validate_template_exists(template: str) -> None:
    if not template_exists(template):
        raise ProxyStartError(
            f"Unknown template '{template}'. Run 'forge proxy template list' to see available templates."
        )


def _get_template_default_port(template: str) -> int:
    cfg = load_config(template=template)
    default_port = cfg.proxy.default_port
    if not default_port:
        raise ProxyStartError(f"Template '{template}' has no proxy.default_port configured")
    return int(default_port)


def _try_reuse_registered_proxy(
    *,
    registry: ProxyRegistry,
    template: str,
    proxy_id: str | None = None,
    timeout_s: float,
) -> ProxyEntry | None:
    if proxy_id is not None:
        # Identity-specific reuse: look for THIS proxy only
        entry = registry.proxies.get(proxy_id)
        if entry is None or entry.template != template:
            return None
        if check_proxy_health(
            base_url=entry.base_url,
            expected_template=template,
            timeout_s=timeout_s,
            expected_proxy_id=entry.proxy_id,
        ):
            entry.last_seen_at = now_iso()
            entry.status = "healthy"
            return entry
        entry.status = "unhealthy"
        return None

    # Template-wide reuse: find any healthy proxy for the template
    candidates = [entry for entry in registry.proxies.values() if entry.template == template]

    # Keep behavior deterministic.
    candidates.sort(key=lambda e: (e.last_seen_at is not None, e.proxy_id), reverse=True)

    for entry in candidates:
        if check_proxy_health(
            base_url=entry.base_url,
            expected_template=template,
            timeout_s=timeout_s,
            expected_proxy_id=entry.proxy_id,
        ):
            entry.last_seen_at = now_iso()
            entry.status = "healthy"
            return entry

        # Update status so `forge proxy list` reflects reality (best effort).
        entry.status = "unhealthy"

    return None


def check_proxy_health(
    *,
    base_url: str,
    expected_template: str,
    timeout_s: float,
    expected_proxy_id: str | None = None,
    require_unregistered: bool = False,
) -> bool:
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
            resp = client.get(f"{base_url}/")
    except (httpx.RequestError, httpx.TimeoutException):
        return False

    if resp.status_code != 200:
        return False

    try:
        data = resp.json()
    except ValueError:
        return False

    if data.get("is_proxy") is not True:
        return False

    if data.get("template") != expected_template:
        return False

    proxy_block = data.get("proxy")
    actual_proxy_id = proxy_block.get("proxy_id") if isinstance(proxy_block, dict) else None

    # Missing proxy metadata is treated as "unregistered": adopt may proceed,
    # but identity-specific reuse/spawn validation must still fail.
    if expected_proxy_id is not None and actual_proxy_id != expected_proxy_id:
        return False

    if require_unregistered and actual_proxy_id is not None:
        return False

    return True


def smoke_test_proxy(*, base_url: str, timeout_s: float = 30.0) -> tuple[bool, str]:
    """Send a minimal completion request through the proxy to verify the upstream LLM.

    Returns (success, detail) where detail is the model response text on
    success or the error message on failure. Retries once on failure.
    """
    # max_tokens must be large enough for thinking models (e.g., Gemini 2.5 Pro)
    # which consume tokens for internal reasoning before producing visible output
    payload = {
        "model": "sonnet",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Say hi"}],
    }
    url = f"{base_url.rstrip('/')}/v1/messages"
    last_error = ""

    for attempt in range(2):
        if attempt > 0:
            time.sleep(2)
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
                resp = client.post(url, json=payload)

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue

            data = resp.json()
            content = data.get("content", [])
            if content and isinstance(content, list):
                text = content[0].get("text", "")
                if text:
                    return True, text.strip()
                # Valid structure but empty text — thinking models may consume
                # all tokens for reasoning. Report model + usage for diagnosis.
                model = data.get("model", "unknown")
                usage = data.get("usage", {})
                last_error = (
                    f"Empty response from {model} "
                    f"(input={usage.get('input_tokens', '?')}, "
                    f"output={usage.get('output_tokens', '?')} tokens)"
                )
                continue

            last_error = f"Unexpected response shape: {resp.text[:200]}"
        except httpx.TimeoutException:
            last_error = f"Request timed out after {timeout_s}s"
        except (httpx.RequestError, ValueError) as e:
            last_error = str(e)

    return False, last_error


def _find_available_port(*, start_port: int, max_attempts: int) -> int:
    for port in range(start_port, start_port + max_attempts):
        if not _is_port_in_use(port):
            return port

    raise ProxyStartError(f"Could not find an available port in range {start_port}-{start_port + max_attempts - 1}")


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("", port))
        except OSError:
            return True
        return False


def _check_proxy_dependencies(*, provider: str = "") -> None:
    """Check if proxy dependencies are installed.

    Args:
        provider: The preferred_provider from the template. When "openrouter",
                  litellm is not required (direct API, no LiteLLM subprocess).

    Raises:
        ProxyStartError: If required proxy dependencies are missing.
    """
    missing = []
    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")

    if provider != "openrouter":
        try:
            import litellm  # noqa: F401
        except ImportError:
            missing.append("litellm")

    if missing:
        deps_str = ", ".join(missing)
        raise ProxyStartError(
            f"Missing required proxy dependencies: {deps_str}\n\n"
            "These are needed to run the model routing proxy.\n\n"
            "To install them:\n"
            "  uv sync                            # If developing in the repo\n"
            "  ./scripts/setup.sh --local         # If you installed with --local\n\n"
            "Or use --no-start to create the config without starting the server."
        )


def _spawn_proxy_process(
    *, template: str, host: str, port: int, proxy_id: str, provider: str = ""
) -> tuple[subprocess.Popen[bytes], Path]:
    """Spawn a proxy subprocess with the given configuration.

    Returns:
        Tuple of (process, stderr_capture_path) for error reporting.
    """
    _check_proxy_dependencies(provider=provider)

    cmd = [
        sys.executable,
        "-m",
        "forge.proxy.server",
        "--template",
        template,
        "--host",
        host,
        "--port",
        str(port),
        "--proxy-id",
        proxy_id,
    ]

    env = {**os.environ}

    # Create temp file for stderr capture (for error reporting)
    import tempfile

    stderr_fd, stderr_path = tempfile.mkstemp(suffix=".log", prefix=f"forge_proxy_{proxy_id}_")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fd,
        env=env,
    )

    # Close the fd (process has it open)
    os.close(stderr_fd)

    return proc, Path(stderr_path)


def _wait_until_healthy(
    *,
    base_url: str,
    expected_template: str,
    proc: subprocess.Popen[bytes],
    stderr_capture: Path,
    timeout_s: float,
    expected_proxy_id: str | None = None,
) -> None:
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        if proc.poll() is not None:
            error_msg = f"Proxy process exited before becoming healthy (exit_code={proc.returncode})"

            stderr_content = ""
            if stderr_capture.exists():
                try:
                    stderr_content = stderr_capture.read_text().strip()
                    stderr_capture.unlink()
                except Exception:
                    pass

            # Also try to read from logs directory
            from forge.core.logging import find_latest_log

            log_hint = ""
            latest_log = find_latest_log("proxy", "proxy.*.log")
            if latest_log:
                log_hint = f"\n\nCheck log file for details: {latest_log}"
                # Try to read last 20 lines
                try:
                    with open(latest_log) as f:
                        lines = f.readlines()
                        if lines:
                            tail = "".join(lines[-20:]).strip()
                            if tail and not stderr_content:
                                stderr_content = tail
                except Exception:
                    pass

            if stderr_content:
                if len(stderr_content) > 500:
                    stderr_content = "..." + stderr_content[-500:]
                error_msg += f"\n\nError output:\n{stderr_content}"

                # Add helpful hint for common dependency errors
                if "ModuleNotFoundError" in stderr_content and any(
                    pkg in stderr_content for pkg in ["uvicorn", "fastapi", "litellm"]
                ):
                    error_msg += (
                        "\n\nTip: Proxy dependencies not installed. Run:\n"
                        "  uv sync (if developing) or ./scripts/setup.sh --local (to reinstall)"
                    )

            error_msg += log_hint
            raise ProxyStartError(error_msg)

        if check_proxy_health(
            base_url=base_url,
            expected_template=expected_template,
            timeout_s=min(1.0, timeout_s),
            expected_proxy_id=expected_proxy_id,
        ):
            if stderr_capture.exists():
                try:
                    stderr_capture.unlink()
                except Exception:
                    pass
            return

        time.sleep(0.25)

    if stderr_capture.exists():
        try:
            stderr_capture.unlink()
        except Exception:
            pass

    raise ProxyStartError(f"Timed out waiting for proxy to become healthy at {base_url}")


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.terminate()
    except OSError:
        return

    try:
        proc.wait(timeout=2.0)
    except Exception:
        try:
            proc.kill()
        except OSError:
            return


def _base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _new_proxy_id(existing: set[str] | None = None) -> str:
    """Generate a color-fruit proxy ID (e.g., 'teal-lemon')."""
    from forge.core.naming import generate_unique_proxy_name

    return generate_unique_proxy_name(existing or set())
