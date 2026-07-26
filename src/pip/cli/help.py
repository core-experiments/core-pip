"""Render top-level CLI help and version information."""

from __future__ import annotations

import sys

from pip.cli.commands.registry import COMMAND_SPECS, get_command, parser_for_command
from pip.core.pip_version import get_pip_version


def print_version(location: str | None = None) -> None:
    from pathlib import Path

    if location is None:
        import importlib.metadata

        location = str(importlib.metadata.distribution("pip").locate_file("pip"))
    print(
        f"pip {get_pip_version()} from {Path(location).resolve()} "
        f"(python {sys.version_info.major}.{sys.version_info.minor})"
    )


def print_help() -> None:
    print("Usage:")
    print("  pip <command> [options]")
    print()
    print("Commands:")
    for spec in COMMAND_SPECS:
        if spec.visible:
            print(f"  {spec.name}")


def run_help(args: list[str]) -> int:
    if not args or args == ["--help"]:
        print_help()
        return 0
    command = args[0]
    spec = get_command(command)
    if spec is not None and spec.name != "help":
        parser_for_command(command).print_help()
        return 0
    print(f"ERROR: Unknown command: {command}", file=sys.stderr)
    return 1
