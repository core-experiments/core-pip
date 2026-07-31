"""Compatibility exports for global command-line option extraction."""

from pip.cli._bootstrap import extract_global_options, extract_python_option

__all__ = ("extract_global_options", "extract_python_option")
