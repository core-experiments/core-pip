"""Canonical, dependency-light process entrypoint for cpip."""

from __future__ import annotations

import os
import sys

from cpip.cli.registry import get_command
from cpip.cli.status_codes import BROKEN_STDOUT
VISIBLE_COMMAND_NAMES = (

    "install",
    "wheel",
    "index",
    "download",
    "uninstall",
    "list",
    "freeze",
    "show",
    "inspect",
    "hash",
    "check",
    "cache",
    "lock",
)

COMMAND_NAMES = frozenset((*VISIBLE_COMMAND_NAMES, "help"))

VIRTUALENV_OPTIONS = frozenset(("--require-virtualenv", "--require-venv"))


VERBOSITY_FLAGS = frozenset(("-vv", "-vvv"))

VERSION_FLAGS = frozenset(("-V", "--version"))

HELP_FLAGS = frozenset(("-h", "--help"))

CPIP_VERSION = "0.0.1"


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
    if version is None or location is None:
        if version is None:
            version = CPIP_VERSION

        if location is None:
            location = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if location is None:
        raise RuntimeError("cpip package location is unavailable")

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    print(
        f"cpip {version} from {os.path.realpath(location)} (python {python_version})",
    )


def print_command_help(command: str) -> int | None:
    if get_command(command) is None:
        return None

    from cpip.cli.registry import parser_for_command

    try:
        parser_for_command(command).parse_args(["--help"])

    except SystemExit as exc:
        return int(exc.code or 0)

    return 0


def run_help(args: list[str]) -> int:
    """Handle the ``cpip help [command]`` subcommand."""

    if not args or args == ["--help"]:
        print_help()

        return 0

    command = args[0]

    spec = get_command(command)

    if spec is not None and spec.name != "help":
        from cpip.cli.registry import parser_for_command

        parser_for_command(command).print_help()

        return 0

    print(f"ERROR: Unknown command: {command}", file=sys.stderr)

    return 1


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    verbosity = 0

    managed_environment = {
        name: os.environ.get(name) for name in ("CPIP_RESOLVER_DEBUG", "CPIP_TARGET_PREFIX")
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
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        if argv and argv[0] == "lock" and "--quiet" in argv[1:]:
            from cpip.cli import fast_lock

            status = fast_lock.run(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        fast_install_attempted = False

        if (
            argv
            and argv[0] == "install"
            and "--quiet" in argv[1:]
            and "--no-index" not in argv[1:]
            and all(
                option in argv[1:] for option in ("--ignore-installed", "--no-compile", "--target")
            )
        ):
            from cpip.cli import fast_install

            status = fast_install.run_cached_remote(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        if (
            argv
            and argv[0] == "install"
            and "--quiet" in argv[1:]
            and "--no-index" in argv[1:]
            and "--upgrade" in argv[1:]
            and "--no-compile" in argv[1:]
            and "--target" in argv[1:]
            and "--ignore-installed" not in argv[1:]
        ):
            from cpip.cli import fast_install

            fast_install_attempted = True

            status = fast_install.run_local_fallback(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        if (
            argv
            and argv[0] == "install"
            and all(
                option in argv[1:]
                for option in (
                    "--no-index",
                    "--ignore-installed",
                    "--no-compile",
                    "--target",
                )
            )
        ):
            from cpip.cli import fast_install

            fast_install_attempted = True

            status = fast_install.run(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

            status = fast_install.run_local_fallback(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        if argv and argv[0] == "list":
            from cpip.cli import fast_list

            status = fast_list.run(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        if not require_virtualenv and log_file is None and verbosity == 0:
            if not argv or argv[0] in HELP_FLAGS or argv[:1] == ["help"]:
                if argv[:1] == ["help"] and len(argv) > 1:
                    command = argv[1]

                    if command not in COMMAND_NAMES or command == "help":
                        print(f"ERROR: Unknown command: {command}", file=sys.stderr)

                        sys.stdout.flush()

                        sys.stderr.flush()

                        return 1

                else:
                    print_help()

                    sys.stdout.flush()

                    sys.stderr.flush()

                    return 0

            if argv and argv[0] in VERSION_FLAGS:
                print_version(version, location)

                sys.stdout.flush()

                sys.stderr.flush()

                return 0

            if argv and argv[0] not in COMMAND_NAMES:
                print(f"ERROR: Unknown command: {argv[0]}", file=sys.stderr)

                sys.stdout.flush()

                sys.stderr.flush()

                return 1

        quiet_fast_command = bool(
            argv
            and "--quiet" in argv
            and log_file is None
            and (
                argv[0] == "lock"
                or (
                    argv[0] == "install"
                    and "--no-index" in argv
                    and "--no-compile" in argv
                    and "--target" in argv
                    and ("--ignore-installed" in argv or "--upgrade" in argv)
                )
            ),
        )

        spec = get_command(argv[0]) if argv else None

        if (version is not None or location is not None) and (
            not argv or (argv[0] != "lock" and (spec is None or spec.needs_tempdir))
        ):
            from cpip.core._execution_context import configure

            configure(
                version=version,
                runner=(
                    os.path.join(os.path.dirname(location), "__cpip-runner__.py")
                    if location is not None
                    else None
                ),
            )

        needs_logging = spec is None or spec.needs_logging

        if needs_logging and not quiet_fast_command and not os.environ.get("CPIP_QUIET"):
            from cpip.cli.logging_config import configure_logging

            configure_logging(log_file)

        if argv and argv[0] == "install" and not fast_install_attempted:
            from cpip.cli import fast_install

            status = fast_install.run(argv[1:])

            if status is not None:
                sys.stdout.flush()

                sys.stderr.flush()

                return status

        if spec is not None and not spec.needs_tempdir:
            from cpip.cli import _main_fallback

            status = _main_fallback.run(
                argv,
                require_virtualenv=require_virtualenv,
                location=location,
            )

        else:
            from cpip.cli import _main_fallback
            from cpip.core.temp_dir import global_tempdir_manager

            with global_tempdir_manager():
                status = _main_fallback.run(
                    argv,
                    require_virtualenv=require_virtualenv,
                    location=location,
                )

        sys.stdout.flush()

        sys.stderr.flush()

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
            from cpip.cli.logging_config import BrokenStdoutLoggingError

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
