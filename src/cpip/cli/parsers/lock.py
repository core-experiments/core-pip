"""Argument parser for ``cpip lock``.

Kept apart from the command module so that ``cpip lock --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    """Resolve requirements and write a PEP 751 ``pylock.toml`` file."""

    parser = ArgumentParser(prog="cpip lock", allow_abbrev=False)

    parser.add_argument("requirements", nargs="*")

    parser.add_argument("-e", "--editable", action="append", default=[])

    parser.add_argument("-r", "--requirement", action="append", default=[])

    parser.add_argument(
        "-c",
        "--constraint",
        dest="constraints",
        metavar="CONSTRAINT",
        action="append",
        default=[],
    )

    parser.add_argument("-f", "--find-links", action="append", default=[])

    parser.add_argument("--no-index", action="store_true")

    parser.add_argument("--no-binary", action="append", default=[])

    parser.add_argument("--no-build-isolation", action="store_true")

    parser.add_argument("--quiet", action="store_true")

    parser.add_argument("--output", default="pylock.toml")

    return parser
