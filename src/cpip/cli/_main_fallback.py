"""Fallback command dispatch kept off the narrow lock fast path."""

from __future__ import annotations

import sys

from cpip.cli import fast_lock
from cpip.cli import lock
from cpip.cli.registry import get_command, get_command_runner
from cpip.cli.entrypoint import (
    HELP_FLAGS,
    VERSION_FLAGS,
    print_help,
    print_version,
    run_help,
)
from cpip.cli.status_codes import VIRTUALENV_NOT_FOUND
from cpip.platform.virtualenv import running_under_virtualenv


def run(
    args: list[str],
    *,
    require_virtualenv: bool = False,
    location: str | None = None,
) -> int:
    if not args:
        print_help()

        return 0

    if args[0] in VERSION_FLAGS:
        print_version(None, location)

        return 0

    if args[0] in HELP_FLAGS:
        print_help()

        return 0

    if args[0] == "help":
        return run_help(args[1:])

    if require_virtualenv:
        if not running_under_virtualenv():
            print("Could not find an activated virtualenv (required).", file=sys.stderr)

            return VIRTUALENV_NOT_FOUND

    if args[0] == "lock":
        status = fast_lock.run(args[1:])

        if status is not None:
            return status

        return lock.run_lock(args[1:])

    command = args[0]

    if get_command(command) is None:
        print(f"ERROR: Unknown command: {command}", file=sys.stderr)

        return 1

    runner = get_command_runner(command)

    if runner is not None:
        return runner(args[1:])

    raise AssertionError(f"unhandled command: {command}")
