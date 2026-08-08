"""Canonical, dependency-light process entrypoint for cpip."""

from __future__ import annotations

import os
import sys

from cpip.cli.common import BROKEN_STDOUT, VIRTUALENV_NOT_FOUND
from cpip.cli.registry import COMMAND_SPECS, CommandSpec, get_command

# Keep command discovery in one place.  The registry also owns command loading,
# parser factories, and lifecycle requirements; deriving these sets here keeps
# global-option handling and help output in sync with dispatch.
VISIBLE_COMMAND_NAMES = tuple(spec.name for spec in COMMAND_SPECS if spec.visible)
COMMAND_NAMES = frozenset(spec.name for spec in COMMAND_SPECS)

VIRTUALENV_OPTIONS = frozenset(("--require-virtualenv", "--require-venv"))


VERBOSITY_FLAGS = frozenset(("-vv", "-vvv"))

VERSION_FLAGS = frozenset(("-V", "--version"))

HELP_FLAGS = frozenset(("-h", "--help"))


def extract_python_option(args: list[str]) -> tuple[list[str], str | None]:
    filtered: list[str] = []

    target_prefix: str | None = None

    index = 0

    while index < len(args):
        token = args[index]

        if token in COMMAND_NAMES:
            filtered.extend(args[index:])

            break

        if token == "--python":
            if index + 1 >= len(args):
                raise ValueError("--python requires a path")

            target_prefix = args[index + 1]

            index += 2

            continue

        if token.startswith("--python="):
            target_prefix = token.partition("=")[2]

            index += 1

            continue

        filtered.append(token)

        index += 1

    return filtered, target_prefix


def extract_global_options(
    args: list[str],
) -> tuple[list[str], int, bool, str | None]:
    filtered: list[str] = []

    log_file: str | None = None

    index = 0

    while index < len(args):
        token = args[index]

        if token == "--log":
            if index + 1 < len(args):
                log_file = args[index + 1]

            index += 2

            continue

        if token.startswith("--log="):
            log_file = token.partition("=")[2]

            index += 1

            continue

        filtered.append(token)

        index += 1

    result: list[str] = []

    verbosity = 0

    require_virtualenv = False

    index = 0

    while index < len(filtered):
        token = filtered[index]

        if token in VIRTUALENV_OPTIONS:
            require_virtualenv = True

            index += 1

            continue

        if token == "--verbose":
            verbosity += 1

            index += 1

            continue

        if token.startswith("-") and set(token[1:]) == {"v"}:
            verbosity += len(token) - 1

            index += 1

            continue

        result.extend(filtered[index:])

        break

    return result, verbosity, require_virtualenv, log_file


def print_help() -> None:
    print("Usage:")

    print("  cpip <command> [options]")

    print()

    print("Commands:")

    for command in VISIBLE_COMMAND_NAMES:
        print(f"  {command}")


def print_version(version: str | None, location: str | None) -> None:
    if version is None:
        # Already imported: cpip.__init__ is what loaded this module.
        from cpip import __version__

        version = __version__

    if location is None:
        location = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    print(
        f"cpip {version} from {os.path.realpath(location)} (python {python_version})",
    )


def print_command_help(command: str) -> int | None:
    """Print a command's own help, or return ``None`` for a non-command.

    ``help`` itself is excluded: it has no parser of its own, and reporting it
    as unknown is what ``cpip help help`` has always done.
    """

    spec = get_command(command)

    if spec is None or spec.name == "help":
        return None

    spec.create_parser().print_help()

    return 0


def run_help(args: list[str]) -> int:
    """Handle the ``cpip help [command]`` subcommand."""

    if not args or args == ["--help"]:
        print_help()

        return 0

    status = print_command_help(args[0])

    if status is None:
        print(f"ERROR: Unknown command: {args[0]}", file=sys.stderr)

        return 1

    return status


def handle_global_commands(
    argv: list[str],
    *,
    require_virtualenv: bool,
    version: str | None,
    location: str | None,
) -> int | None:
    """Handle help, version, the virtualenv gate, and unknown command names.

    Returns the process status, or ``None`` when ``argv`` names a real command
    and dispatch should continue.  The order matches what the fallback
    dispatcher used to do: help and version answer before the virtualenv gate,
    so ``cpip --require-virtualenv --help`` still works outside a virtualenv.
    """

    if not argv or argv[0] in HELP_FLAGS:
        print_help()

        return 0

    if argv[0] == "help":
        return run_help(argv[1:])

    if argv[0] in VERSION_FLAGS:
        print_version(version, location)

        return 0

    if require_virtualenv:
        from cpip.platform.virtualenv import running_under_virtualenv

        if not running_under_virtualenv():
            print(
                "Could not find an activated virtualenv (required).",
                file=sys.stderr,
            )

            return VIRTUALENV_NOT_FOUND

    if argv[0] not in COMMAND_NAMES:
        print(f"ERROR: Unknown command: {argv[0]}", file=sys.stderr)

        return 1

    return None


def run_command(argv: list[str], spec: CommandSpec) -> int:
    """Run a resolved command, giving the lock fast path its last chance."""

    from cpip.cli import fast

    status = fast.run_lock_after_startup(argv)

    if status is not None:
        return status

    runner = spec.load_runner()

    if runner is None:
        raise AssertionError(f"unhandled command: {spec.name}")

    return runner(argv[1:])


def flush_streams() -> None:
    sys.stdout.flush()

    sys.stderr.flush()


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    verbosity = 0

    managed_environment = {
        name: os.environ.get(name)
        for name in ("CPIP_RESOLVER_DEBUG", "CPIP_TARGET_PREFIX")
    }
    try:
        argv = list(sys.argv[1:] if args is None else args)
        argv, verbosity, require_virtualenv, log_file = extract_global_options(argv)
        if verbosity >= 2 or any(token in VERBOSITY_FLAGS for token in argv):
            os.environ["CPIP_RESOLVER_DEBUG"] = "1"
        argv, target_prefix = extract_python_option(argv)
        if target_prefix is not None:
            os.environ["CPIP_TARGET_PREFIX"] = target_prefix

        if (
            not require_virtualenv
            and log_file is None
            and verbosity == 0
            and len(argv) > 1
            and argv[0] not in HELP_FLAGS
            and any(token in HELP_FLAGS for token in argv[1:])
        ):
            status = print_command_help(argv[0])

            if status is not None:
                flush_streams()

                return status

        status = handle_global_commands(
            argv,
            require_virtualenv=require_virtualenv,
            version=version,
            location=location,
        )

        if status is not None:
            flush_streams()

            return status

        from cpip.cli import fast

        status, fast_install_attempted = fast.run_before_startup(argv)

        if status is not None:
            flush_streams()

            return status

        quiet_fast_command = fast.suppresses_logging(argv, log_file=log_file)

        # handle_global_commands rejected empty argv and unknown names, so the
        # command always resolves from here on.
        spec = get_command(argv[0])

        if spec is None:
            raise AssertionError(f"unhandled command: {argv[0]}")

        if version is not None and (
            spec.needs_execution_context and spec.needs_tempdir
        ):
            from cpip.core.utils import configure

            configure(version=version)

        if (
            spec.needs_logging
            and not quiet_fast_command
            and not os.environ.get("CPIP_QUIET")
        ):
            from cpip.cli.common import configure_logging

            configure_logging(log_file)

        if not fast_install_attempted:
            status = fast.run_install_after_startup(argv)

            if status is not None:
                flush_streams()

                return status

        if spec.needs_tempdir:
            from cpip.core.temp_dir import global_tempdir_manager

            with global_tempdir_manager():
                status = run_command(argv, spec)

        else:
            status = run_command(argv, spec)

        flush_streams()

        return status

    except OSError as exc:
        import errno

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

            from cpip.cli.common import BrokenStdoutLoggingError

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
        from cpip.core.errors import CpipError

        if not isinstance(exc, CpipError):
            raise

        print(f"ERROR: {exc}", file=sys.stderr)

        return 1

    finally:
        for name, previous in managed_environment.items():
            if previous is None:
                os.environ.pop(name, None)

            else:
                os.environ[name] = previous
