"""Implementation of the ``pip freeze`` command."""

from __future__ import annotations

import sys
from pathlib import Path

from pip.cli.freeze import freeze
from pip.cli.parser import ArgumentParser
from pip.core.metadata import stdlib_pkgs
from pip.core.packaging import canonicalize_name
from pip.core.pip_version import PIP_DISTRIBUTION_NAMES


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="pip freeze")
    parser.add_argument("-r", "--requirement", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--exclude-editable", action="store_true")
    return parser


def run_freeze(args: list[str]) -> int:
    options = create_parser().parse_args(args)
    excluded = {canonicalize_name(name) for name in options.exclude}
    if "pip" in excluded:
        excluded.update(canonicalize_name(name) for name in PIP_DISTRIBUTION_NAMES)
    skip = set(stdlib_pkgs)
    if not options.all:
        skip.update(PIP_DISTRIBUTION_NAMES)
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
