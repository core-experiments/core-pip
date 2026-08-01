"""Render top-level CLI help and version information."""

from __future__ import annotations

import sys

from cpip.cli.commands.registry import COMMAND_SPECS, get_command, parser_for_command
from cpip.core.cpip_version import get_cpip_distribution, get_cpip_version
from cpip.core.python import CURRENT_PYTHON_VERSION


def print_version(location: str | None = None) -> None:
    from pathlib import Path

    if location is None:
        location = str(get_cpip_distribution().locate_file("cpip"))
    print(
        f"cpip {get_cpip_version()} from {Path(location).resolve()} "
        f"(python {CURRENT_PYTHON_VERSION})"
    )


def print_help() -> None:
    print("Usage:")
    print("  cpip <command> [options]")
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
