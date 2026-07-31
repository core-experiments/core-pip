"""Implementation of the ``cpip freeze`` command."""

from __future__ import annotations

import sys
from pathlib import Path

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip freeze")
    parser.add_argument("-r", "--requirement", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--exclude-editable", action="store_true")
    return parser


def run_freeze(args: list[str]) -> int:
    from cpip.cli.freeze import freeze
    from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES
    from cpip.core.metadata import stdlib_pkgs
    from cpip.core.packaging import canonicalize_name

    options = create_parser().parse_args(args)
    excluded = {canonicalize_name(name) for name in options.exclude}
    if "cpip" in excluded:
        excluded.update(canonicalize_name(name) for name in CPIP_DISTRIBUTION_NAMES)
    skip = set(stdlib_pkgs)
    if not options.all:
        skip.update(CPIP_DISTRIBUTION_NAMES)
        if sys.version_info < (3, 12):
            skip.add("setuptools")
    paths = [str(Path(path)) for path in options.path] if options.path else None
    for line in freeze(
        requirement=options.requirement,
        user_only=options.user,
        paths=paths,
        exclude_editable=options.exclude_editable,
        exclude=excluded,
        skip=skip,
    ):
        print(line, end="")
    return 0
