"""Compatibility entrypoint for callers importing ``cpip.cli.main``."""

from __future__ import annotations

import os
import sys


HELP_TEXT = """Usage:
  cpip <command> [options]

Commands:
  install
  wheel
  index
  download
  uninstall
  list
  freeze
  show
  inspect
  hash
  check
  cache
  lock
"""


def _run_bootstrap_command(
    argv: list[str], version: str | None, location: str | None
) -> int | None:
    if argv not in ([], ["-h"], ["--help"], ["help"], ["-V"], ["--version"]):
        return None

    if argv in ([], ["-h"], ["--help"], ["help"]):
        sys.stdout.write(HELP_TEXT)
        return 0

    if version is None or location is None:
        import cpip

        if version is None:
            version = cpip.__version__
        if location is None:
            location = os.path.dirname(cpip.__file__)
    if location is None:
        raise RuntimeError("cpip package location is unavailable")
    sys.stdout.write(
        f"cpip {version} from {os.path.realpath(location)} "
        f"(python {sys.version_info.major}.{sys.version_info.minor})\n"
    )
    return 0


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    argv = sys.argv[1:] if args is None else args
    bootstrap_status = _run_bootstrap_command(argv, version, location)
    if bootstrap_status is not None:
        return bootstrap_status

    from cpip.cli.entrypoint import main as run_entrypoint

    return run_entrypoint(args, version=version, location=location)


def run(
    args: list[str], *, require_virtualenv: bool = False, location: str | None = None
) -> int:
    from cpip.cli._main_fallback import run as run_fallback

    return run_fallback(args, require_virtualenv=require_virtualenv, location=location)
