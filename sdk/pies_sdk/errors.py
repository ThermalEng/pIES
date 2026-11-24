"""Public exception hierarchy for pies-sdk."""

from __future__ import annotations


class PiesSdkError(Exception):
    """Base error for all pies-sdk contract and runtime failures."""


class SafeYamlError(PiesSdkError, ValueError):
    """Raised when YAML input violates the YAML 1.2 safe subset."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        prefix = f"Line {line}: " if line is not None else ""
        super().__init__(f"{prefix}{message}")


class PluginValidationError(PiesSdkError, ValueError):
    """Raised when a plugin manifest or its parameters fail validation."""


class CompatibilityError(PluginValidationError):
    """Raised when plugin schema_version or compatibility does not match."""


class StageIOError(PiesSdkError, ValueError):
    """Raised when /work/in or /work/out stage I/O violates the contract."""


class EntrypointError(PiesSdkError, RuntimeError):
    """Raised when resolving or invoking a plugin entrypoint fails."""
