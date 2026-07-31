"""Canonical, dependency-light process entrypoint for pip."""

from __future__ import annotations

import os
import sys

from pip.cli._bootstrap import extract_global_options, extract_python_option
from pip.cli.commands.names import VISIBLE_COMMAND_NAMES

VERBOSITY_FLAGS = frozenset(("-vv", "-vvv"))
VERSION_FLAGS = frozenset(("-V", "--version"))
HELP_FLAGS = frozenset(("-h", "--help"))


def _print_help() -> None:
    print("Usage:")
    print("  pip <command> [options]")
    print()
    print("Commands:")
    for command in VISIBLE_COMMAND_NAMES:
        print(f"  {command}")


def _print_version(version: str | None, location: str | None) -> None:
    if version is None:
        import importlib.metadata

        distribution = None
        for name in ("core-pip", "pip"):
            try:
                distribution = importlib.metadata.distribution(name)
                break
            except importlib.metadata.PackageNotFoundError:
                continue
        if distribution is not None:
            version = distribution.version
            if location is None:
                location = str(distribution.locate_file("pip"))
        else:
            from pip import __version__

            version = __version__
    if location is None:
        import pip

        location = pip.__file__
    from pathlib import Path

    print(
        f"pip {version} from {Path(location).resolve()} "
        f"(python {sys.version_info.major}.{sys.version_info.minor})"
    )


def _print_command_help(command: str) -> int | None:
    from pip.cli.commands.registry import get_command, parser_for_command

    if get_command(command) is None:
        return None

    try:
        parser_for_command(command).parse_args(["--help"])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


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
    try:
        argv = list(sys.argv[1:] if args is None else args)
        argv, verbosity, require_virtualenv, log_file = extract_global_options(argv)
        if verbosity >= 2 or any(token in VERBOSITY_FLAGS for token in argv):
            os.environ["PIP_RESOLVER_DEBUG"] = "1"
        argv, target_prefix = extract_python_option(argv)
        if target_prefix is not None:
            os.environ["PIP_TARGET_PREFIX"] = target_prefix

        if (
            not require_virtualenv
            and log_file is None
            and verbosity == 0
            and len(argv) > 1
            and argv[0] not in HELP_FLAGS
            and any(token in HELP_FLAGS for token in argv[1:])
        ):
            status = _print_command_help(argv[0])
            if status is not None:
                sys.stdout.flush()
                sys.stderr.flush()
                return status

        if argv and argv[0] == "lock" and "--quiet" in argv[1:]:
            from pip.cli.commands.fast_lock import run as run_fast_lock

            status = run_fast_lock(argv[1:])
            if status is not None:
                sys.stdout.flush()
                sys.stderr.flush()
                return status

        if argv and argv[0] == "install" and all(
            option in argv[1:]
            for option in (
                "--no-index",
                "--ignore-installed",
                "--no-compile",
                "--target",
            )
        ):
            from pip.cli.commands.fast_install import run as run_fast_install

            status = run_fast_install(argv[1:])
            if status is not None:
                sys.stdout.flush()
                sys.stderr.flush()
                return status

        if not require_virtualenv and log_file is None and verbosity == 0:
            if not argv or argv[0] in HELP_FLAGS or argv[:1] == ["help"]:
                if argv[:1] == ["help"] and len(argv) > 1:
                    pass
                else:
                    _print_help()
                    sys.stdout.flush()
                    sys.stderr.flush()
                    return 0
            if argv and argv[0] in VERSION_FLAGS:
                _print_version(version, location)
                sys.stdout.flush()
                sys.stderr.flush()
                return 0

        quiet_fast_command = bool(
            argv
            and "--quiet" in argv
            and log_file is None
            and (
                argv[0] == "lock"
                or (
                    argv[0] == "install"
                    and "--no-index" in argv
                    and "--ignore-installed" in argv
                    and "--no-compile" in argv
                    and "--target" in argv
                )
            )
        )
        from pip.cli._fallback_main import run as run_fallback_main

        status = run_fallback_main(
            argv,
            require_virtualenv=require_virtualenv,
            log_file=log_file,
            version=version,
            location=location,
            quiet_fast_command=quiet_fast_command,
        )
        sys.stdout.flush()
        sys.stderr.flush()
        return status
    except OSError as exc:
        import errno

        from pip.cli.status_codes import BROKEN_STDOUT

        if not isinstance(exc, BrokenPipeError) and exc.errno not in {
            errno.EINVAL,
            errno.EBADF,
        }:
            raise
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except OSError:
            pass
        print("ERROR: Pipe to stdout was broken", file=sys.stderr)
        if verbosity > 0:
            import traceback

            from pip.cli.logging_config import BrokenStdoutLoggingError

            try:
                raise BrokenStdoutLoggingError() from exc
            except BrokenStdoutLoggingError:
                traceback.print_exc(file=sys.stderr)
        return BROKEN_STDOUT
    except KeyboardInterrupt:
        print("ERROR: Operation cancelled by user", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        from pip.core.errors import PipError

        if not isinstance(exc, PipError):
            raise
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for name, previous in managed_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
