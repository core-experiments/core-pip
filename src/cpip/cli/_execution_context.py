"""Compatibility imports for the process-local execution context."""

from cpip.core._execution_context import (
    configure,
    context,
    current_runner,
    current_version,
)

__all__ = ["configure", "context", "current_runner", "current_version"]
