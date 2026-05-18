"""Exceptions for Forge Installer.

Follows the pattern from session/exceptions.py: specific exception types
with context fields for debugging.
"""

from __future__ import annotations

from typing import Any


class ForgeInstallError(Exception):
    """Base exception for install module."""


class ConflictError(ForgeInstallError):
    """Base for conflict errors."""


class FileConflictError(ConflictError):
    """Raised when a file conflict is detected.

    Attributes:
        path: The conflicting file path.
        reason: Why the conflict occurred.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"file conflict at '{path}': {reason}")


class SettingsConflictError(ConflictError):
    """Raised when a settings conflict is detected.

    Attributes:
        key_path: The conflicting settings key (dot-notation).
        current_value: The existing value in settings.
        forge_value: The value Forge wants to set.
    """

    def __init__(self, key_path: str, current_value: Any, forge_value: Any) -> None:
        self.key_path = key_path
        self.current_value = current_value
        self.forge_value = forge_value
        super().__init__(f"settings conflict at '{key_path}': " f"current={current_value!r}, forge={forge_value!r}")


class TrackingCorruptedError(ForgeInstallError):
    """Raised when tracking file cannot be parsed.

    Attributes:
        path: Path to the problematic tracking file.
        reason: What went wrong during parsing.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"tracking file at '{path}': {reason}")


class NotInstalledError(ForgeInstallError):
    """Raised when trying to update/uninstall with no installation.

    Attributes:
        scope: The scope that has no installation.
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"no Forge installation found for scope '{scope}'")


class SourceNotFoundError(ForgeInstallError):
    """Raised when source extension files are missing.

    Attributes:
        module: The module whose source is missing.
        path: Expected path to the source.
    """

    def __init__(self, module: str, path: str) -> None:
        self.module = module
        self.path = path
        super().__init__(f"source for module '{module}' not found at '{path}'")


class NestedClaudeDirectoryError(ForgeInstallError):
    """Raised when project_root is inside a .claude directory.

    This prevents creating nested .claude/.claude directories which can
    happen if `forge init --project` is run from within a .claude directory.

    Attributes:
        project_root: The problematic project root path.
    """

    def __init__(self, project_root: str) -> None:
        self.project_root = project_root
        super().__init__(
            f"project root '{project_root}' is inside a .claude directory; "
            "this would create nested .claude/.claude directories. "
            "Run from the project root, not from within .claude/"
        )


class NoClaudeDirectoryError(ForgeInstallError):
    """Raised when no .claude directory is found walking up from cwd.

    This indicates Forge is being run outside of a Claude Code project,
    and the user hasn't explicitly specified a scope.

    Attributes:
        start_path: The directory where the search started.
    """

    def __init__(self, start_path: str) -> None:
        self.start_path = start_path
        super().__init__(
            f"no .claude directory found walking up from '{start_path}'. "
            "Run from within a Claude Code project, or use '--scope user' for global install."
        )


class NoForgeInstallationError(ForgeInstallError):
    """Raised when no Forge installation is found walking up from cwd.

    Different from NotInstalledError: this is for auto-detection when no
    scope is specified, whereas NotInstalledError is for a specific scope.

    Attributes:
        start_path: The directory where the search started.
    """

    def __init__(self, start_path: str) -> None:
        self.start_path = start_path
        super().__init__(
            f"no Forge installation found walking up from '{start_path}'. "
            "Run 'forge init' first, or specify a scope explicitly."
        )


class PathBoundaryViolationError(ForgeInstallError):
    """Raised when a path is outside its expected boundary.

    This is a security check to prevent malicious tracking file modifications
    from causing deletion of arbitrary system files.

    Attributes:
        path: The offending path.
        expected_base: The expected parent directory.
        operation: What was being attempted (e.g., "delete").
    """

    def __init__(self, path: str, expected_base: str, operation: str = "access") -> None:
        self.path = path
        self.expected_base = expected_base
        self.operation = operation
        super().__init__(
            f"security violation: refusing to {operation} '{path}' - " f"not within expected boundary '{expected_base}'"
        )
