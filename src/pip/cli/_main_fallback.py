"""Fallback command dispatch kept off the narrow lock fast path."""

from __future__ import annotations

import sys

VERSION_FLAGS = frozenset(("-V", "--version"))
HELP_FLAGS = frozenset(("-h", "--help"))


def run(
    args: list[str], *, require_virtualenv: bool = False, location: str | None = None
) -> int:
    if not args:
        from pip.cli.help import print_help

        print_help()
        return 0
    if args[0] in VERSION_FLAGS:
        from pip.cli.help import print_version

        print_version(location)
        return 0
    if args[0] in HELP_FLAGS:
        from pip.cli.help import print_help

        print_help()
        return 0
    if args[0] == "help":
        from pip.cli.help import run_help

        return run_help(args[1:])
    if require_virtualenv:
        from pip.platform.virtualenv import running_under_virtualenv

        if not running_under_virtualenv():
            from pip.cli.status_codes import VIRTUALENV_NOT_FOUND

            print("Could not find an activated virtualenv (required).", file=sys.stderr)
            return VIRTUALENV_NOT_FOUND
    if args[0] == "lock":
        from pip.cli.commands.fast_lock import run as run_fast_lock

        status = run_fast_lock(args[1:])
        if status is not None:
            return status
        from pip.cli.commands.lock import run_lock

        return run_lock(args[1:])
    from pip.cli.commands.registry import get_command, get_command_runner

    command = args[0]
    if get_command(command) is None:
        print(f"ERROR: Unknown command: {command}", file=sys.stderr)
        return 1
    runner = get_command_runner(command)
    if runner is not None:
        return runner(args[1:])
    raise AssertionError(f"unhandled command: {command}")
