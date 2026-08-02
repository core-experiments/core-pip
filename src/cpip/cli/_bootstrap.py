"""Compatibility exports for dependency-light entrypoint parsing."""

from __future__ import annotations

from cpip.cli.entrypoint import (
    COMMAND_NAMES,
    VISIBLE_COMMAND_NAMES,
    extract_global_options,
    extract_python_option,
)

__all__ = (
    "COMMAND_NAMES",
    "VISIBLE_COMMAND_NAMES",
    "extract_global_options",
    "extract_python_option",
)
