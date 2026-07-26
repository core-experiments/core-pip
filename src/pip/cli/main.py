from __future__ import annotations

import os
import sys

from pip.cli.logging_config import BrokenStdoutLoggingError, configure_logging
from pip.cli.options import extract_global_options, extract_python_option
from pip.cli.status_codes import BROKEN_STDOUT, VIRTUALENV_NOT_FOUND
from pip.core.errors import (
    PipError,
)
from pip.core.pip_version import set_pip_version

VERBOSITY_FLAGS = frozenset(("-vv", "-vvv"))
VERSION_FLAGS = frozenset(("-V", "--version"))
HELP_FLAGS = frozenset(("-h", "--help"))


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    verbosity = 0
    managed_environment = {
        name: os.environ.get(name)
        for name in ("PIP_RESOLVER_DEBUG", "PIP_TARGET_PREFIX")
    }
    if version is not None:
        set_pip_version(version)
    if location is not None:
        from pip.install.runner import set_pip_runner

        set_pip_runner(os.path.join(os.path.dirname(location), "__pip-runner__.py"))
    try:
        argv = list(sys.argv[1:] if args is None else args)
        argv, verbosity, require_virtualenv, log_file = extract_global_options(argv)
        if verbosity >= 2 or any(token in VERBOSITY_FLAGS for token in argv):
            os.environ["PIP_RESOLVER_DEBUG"] = "1"
        argv, target_prefix = extract_python_option(argv)
        if target_prefix is not None:
            os.environ["PIP_TARGET_PREFIX"] = target_prefix
        configure_logging(log_file)
        status = run(argv, require_virtualenv=require_virtualenv, location=location)
        sys.stdout.flush()
        sys.stderr.flush()
        return status
    except BrokenPipeError as exc:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except OSError:
            pass
        print("ERROR: Pipe to stdout was broken", file=sys.stderr)
        if verbosity > 0:
            import traceback

            try:
                raise BrokenStdoutLoggingError() from exc
            except BrokenStdoutLoggingError:
                traceback.print_exc(file=sys.stderr)
        return BROKEN_STDOUT
    except KeyboardInterrupt:
        print("ERROR: Operation cancelled by user", file=sys.stderr)
        return 1
    except (PipError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for name, previous in managed_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


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
            print("Could not find an activated virtualenv (required).", file=sys.stderr)
            return VIRTUALENV_NOT_FOUND
    from pip.cli.commands.registry import get_command, get_command_runner

    command = args[0]
    if get_command(command) is None:
        print(f"ERROR: Unknown command: {command}", file=sys.stderr)
        return 1
    runner = get_command_runner(command)
    if runner is not None:
        return runner(args[1:])
    raise AssertionError(f"unhandled command: {command}")
