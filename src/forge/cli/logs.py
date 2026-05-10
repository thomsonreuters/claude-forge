"""Show log file locations and status."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import click
from rich.console import Console

from forge.core.logging import get_effective_log_level
from forge.core.paths import display_path, get_forge_home

logger = logging.getLogger(__name__)

console = Console()

# Descriptions for known log subdirectories (display only).
# Unknown subdirectories are auto-discovered and shown without a description.
_LOG_DIR_DESCRIPTIONS: dict[str, str] = {
    "proxy": "Proxy server logs",
    "backend": "Backend process logs (LiteLLM)",
    "hooks": "Hook logs",
    "cli": "CLI command logs",
    "tool_failures": "Tool failure telemetry (proxy, opt-in)",
    "tool_events": "Tool event recordings (proxy, debug-only)",
    "requests": "Raw request/response logs (proxy, debug-only)",
}


def _discover_log_dirs(logs_root: Path) -> list[tuple[str, str, bool]]:
    """Discover all log subdirectories under logs_root.

    Returns known dirs (with descriptions) first — always included even if
    they don't exist yet — then any unknown dirs that exist on disk.

    Returns:
        List of (name, description, exists_on_disk).
    """
    actual_dirs: set[str] = set()
    if logs_root.is_dir():
        actual_dirs = {d.name for d in logs_root.iterdir() if d.is_dir()}

    result: list[tuple[str, str, bool]] = []
    # Known dirs first (stable display order, always shown)
    for name, desc in _LOG_DIR_DESCRIPTIONS.items():
        result.append((name, desc, name in actual_dirs))
        actual_dirs.discard(name)

    # Unknown dirs that exist on disk (auto-discovered, alphabetical)
    for name in sorted(actual_dirs):
        result.append((name, "", True))

    return result


def _count_files(directory: Path) -> tuple[int, int]:
    """Count files and total size in a directory.

    Returns:
        (file_count, total_bytes)
    """
    if not directory.is_dir():
        return 0, 0
    total_size = 0
    count = 0
    for f in directory.iterdir():
        if f.is_file():
            count += 1
            try:
                total_size += f.stat().st_size
            except OSError:
                pass
    return count, total_size


def _format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _file_age_days(path: Path) -> float:
    """Return file age in days based on mtime."""
    return (time.time() - path.stat().st_mtime) / 86400


def _is_older_than(path: Path, days: int) -> bool:
    """Check if a file's mtime is older than the given number of days."""
    try:
        return _file_age_days(path) > days
    except OSError:
        return False


def _extract_pid(filename: str) -> int | None:
    """Extract PID from log filename.

    Handles patterns: 'proxy.12345.log', 'proxy.12345.log.1' (rotated),
    '20260327_proxy.12345.jsonl'.
    """
    stem = filename
    # Strip rotation suffix (.log.1, .log.2, etc.)
    if ".log." in stem:
        stem = stem[: stem.index(".log.") + 4]  # keep up to '.log'
    parts = stem.rsplit(".", 2)
    if len(parts) >= 3:
        try:
            return int(parts[-2])
        except ValueError:
            pass
    return None


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but we can't signal it


def _is_active_log_file(path: Path) -> bool:
    """Check if a log file belongs to a currently running process."""
    pid = _extract_pid(path.name)
    if pid is None:
        return False
    return _is_process_alive(pid)


def _is_log_file(path: Path) -> bool:
    """Check if a file has a known log extension (.log, .log.N, .jsonl)."""
    name = path.name
    if name.endswith(".jsonl"):
        return True
    if name.endswith(".log"):
        return True
    # Rotated logs: .log.1 through .log.5
    if ".log." in name:
        suffix = name[name.index(".log.") + 5 :]
        return suffix.isdigit()
    return False


def _oldest_file_age_days(logs_root: Path) -> float | None:
    """Find the age (in days) of the oldest log file across all subdirectories."""
    oldest: float | None = None
    if not logs_root.is_dir():
        return None

    def _update_oldest(f: Path) -> None:
        nonlocal oldest
        try:
            age = _file_age_days(f)
            if oldest is None or age > oldest:
                oldest = age
        except OSError:
            pass

    for subdir, _, exists in _discover_log_dirs(logs_root):
        if not exists:
            continue
        for f in (logs_root / subdir).iterdir():
            if f.is_file():
                _update_oldest(f)

    return oldest


def _remove_files(logs_root: Path, older_than_days: int | None = None) -> tuple[int, int, int]:
    """Remove log files from all subdirectories.

    Skips files that belong to a running process (PID extracted from filename)
    to avoid deleting logs out from under an active proxy or backend.

    Args:
        logs_root: Root logs directory.
        older_than_days: If set, only remove files older than this many days.
            None means remove all files.

    Returns:
        (removed_count, failed_count, skipped_active_count)
    """
    if not logs_root.is_dir():
        return 0, 0, 0

    removed = 0
    failed = 0
    skipped_active = 0

    def _try_remove(f: Path) -> None:
        nonlocal removed, failed, skipped_active
        if older_than_days is not None and not _is_older_than(f, older_than_days):
            return
        if _is_active_log_file(f):
            skipped_active += 1
            return
        try:
            f.unlink()
            removed += 1
        except OSError:
            failed += 1

    for subdir, _, exists in _discover_log_dirs(logs_root):
        if not exists:
            continue
        dir_path = logs_root / subdir
        for f in dir_path.iterdir():
            if f.is_file() and _is_log_file(f):
                _try_remove(f)

    return removed, failed, skipped_active


def auto_clean_old_logs() -> None:
    """Auto-prune old logs based on log_retention_days config.

    Called opportunistically on CLI startup. Best-effort: swallows all
    exceptions to avoid breaking CLI commands.
    """
    try:
        from forge.runtime_config import get_runtime_config

        rc = get_runtime_config()
        if rc.log_retention_days <= 0:
            return

        logs_root = get_forge_home() / "logs"
        removed, _, _ = _remove_files(logs_root, older_than_days=rc.log_retention_days)
        if removed:
            logger.debug("Auto-cleaned %d log file(s) older than %d days", removed, rc.log_retention_days)
    except Exception as e:
        logger.debug("Log auto-cleanup error (non-fatal): %s", e)


@click.command("logs")
@click.option("--clean", is_flag=True, help="Remove log files")
@click.option(
    "--older-than",
    type=int,
    default=None,
    metavar="DAYS",
    help="Only remove files older than DAYS days (requires --clean)",
)
def logs_cmd(clean: bool, older_than: int | None) -> None:
    """Show log file locations and status.

    \b
    Examples:
        forge logs                        # Show log locations and file counts
        forge logs --clean                # Remove all log files
        forge logs --clean --older-than 7 # Remove logs older than 7 days
    """
    if older_than is not None and not clean:
        console.print("[red]Error:[/red] --older-than requires --clean")
        raise SystemExit(1)

    if older_than is not None and older_than < 1:
        console.print("[red]Error:[/red] --older-than must be >= 1")
        raise SystemExit(1)

    logs_root = get_forge_home() / "logs"

    if clean:
        _clean_logs(logs_root, older_than_days=older_than)
        return

    _show_logs(logs_root)


def _show_logs(logs_root: Path) -> None:
    """Display log directory status."""
    level = get_effective_log_level()
    console.print(f"\n[bold]Log directory:[/bold]  {display_path(logs_root)}")
    console.print(f"[bold]Log level:[/bold]      {level}")

    # Show retention config if set
    try:
        from forge.runtime_config import get_runtime_config

        rc = get_runtime_config()
        if rc.log_retention_days > 0:
            console.print(f"[bold]Retention:[/bold]      {rc.log_retention_days} days (auto-cleanup on startup)")
        else:
            console.print("[bold]Retention:[/bold]      unlimited")
    except Exception:
        pass

    console.print()

    total_files = 0
    total_bytes = 0
    for subdir, description, exists in _discover_log_dirs(logs_root):
        dir_path = logs_root / subdir
        count, size = _count_files(dir_path) if exists else (0, 0)
        total_files += count
        total_bytes += size
        if count > 0:
            console.print(f"  [cyan]{subdir}/[/cyan]  {count} files ({_format_size(size)})")
        else:
            console.print(f"  [cyan]{subdir}/[/cyan]  [dim](empty)[/dim]")
        if description:
            console.print(f"  [dim]{description}[/dim]")
        console.print(f"  [dim]{display_path(dir_path)}[/dim]")
        console.print()

    # Summary with oldest file age
    if total_files > 0:
        oldest = _oldest_file_age_days(logs_root)
        age_str = f", oldest {oldest:.0f}d ago" if oldest is not None and oldest >= 1 else ""
        console.print(f"  [bold]Total:[/bold] {total_files} files ({_format_size(total_bytes)}{age_str})")
        console.print()

    if level == "off":
        console.print("[dim]Tip: Enable debug logging with:[/dim]")
        console.print("[dim]  forge config set log_level=debug     # persistent[/dim]")
        console.print("[dim]  FORGE_DEBUG=1 forge <command>        # one-off[/dim]")
    else:
        console.print("[dim]Tip: Disable debug logging with:[/dim]")
        console.print("[dim]  forge config set log_level=off[/dim]")

    # Cleanup tips when there are files to manage
    if total_files > 0:
        try:
            from forge.runtime_config import get_runtime_config as _get_rc

            retention = _get_rc().log_retention_days
        except Exception:
            retention = 0
        if retention <= 0:
            console.print("\n[dim]Tip: Clean up old logs:[/dim]")
            console.print("[dim]  forge logs --clean                         # remove all[/dim]")
            console.print("[dim]  forge logs --clean --older-than 30         # older than 30 days[/dim]")
            console.print("[dim]  forge config set log_retention_days=30     # auto-cleanup on startup[/dim]")
        else:
            console.print("\n[dim]Tip: forge logs --clean --older-than 7      # manual one-off cleanup[/dim]")

    # Warn about adopted proxies that won't have Forge logs.
    # Show regardless of whether proxy/ has files — old log files from a
    # previously managed proxy don't help diagnose a current adopted one.
    if level != "off":
        try:
            from forge.proxy.proxies import ProxyRegistryStore

            store = ProxyRegistryStore()
            registry = store.read()
            adopted = [e for e in registry.proxies.values() if e.pid is None and e.status == "healthy"]
            if adopted:
                names = ", ".join(e.proxy_id for e in adopted[:3])
                suffix = f" (+{len(adopted) - 3} more)" if len(adopted) > 3 else ""
                console.print(
                    f"[yellow]Note:[/yellow] {len(adopted)} adopted proxy(ies) "
                    f"({names}{suffix}) were not started by Forge and have no log files."
                )
                console.print("[dim]Tip: Delete and recreate proxies for full Forge logging.[/dim]")
        except Exception:
            pass


def _clean_logs(logs_root: Path, older_than_days: int | None = None) -> None:
    """Remove log files from all subdirectories."""
    removed, failed, skipped_active = _remove_files(logs_root, older_than_days=older_than_days)

    if removed == 0 and failed == 0 and skipped_active == 0:
        if older_than_days is not None:
            console.print(f"[dim]No log files older than {older_than_days} days found.[/dim]")
        else:
            console.print("[dim]No log files found.[/dim]")
        return

    if older_than_days is not None:
        console.print(f"Removed {removed} log file{'s' if removed != 1 else ''} older than {older_than_days} days.")
    else:
        console.print(f"Removed {removed} log file{'s' if removed != 1 else ''}.")
    if skipped_active:
        console.print(
            f"[dim]Kept {skipped_active} file{'s' if skipped_active != 1 else ''}"
            f" belonging to running process(es).[/dim]"
        )
    if failed:
        console.print(
            f"[yellow]Skipped {failed} file{'s' if failed != 1 else ''} (locked or permission denied).[/yellow]"
        )
