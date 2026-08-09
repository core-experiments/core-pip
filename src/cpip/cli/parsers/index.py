"""Argument parser for ``cpip index``.

Kept apart from the command module so that ``cpip index --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    """Query package indexes without resolving or installing a package."""

    parser = ArgumentParser(prog="cpip index")

    parser.add_argument("command", choices=("versions",))

    parser.add_argument("package")

    parser.add_argument("--json", action="store_true")

    parser.add_argument("-i", "--index-url")

    parser.add_argument("--extra-index-url", action="append", default=[])

    parser.add_argument(
        "--trusted-host",
        dest="trusted_hosts",
        action="append",
        default=[],
    )

    parser.add_argument("--no-index", action="store_true")

    parser.add_argument("--pre", action="store_true")

    return parser
