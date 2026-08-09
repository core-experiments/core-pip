"""Argument parser for ``cpip list``.

Kept apart from the command module so that ``cpip list --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip list")

    parser.add_argument("-o", "--outdated", action="store_true")

    parser.add_argument("-u", "--uptodate", action="store_true")

    parser.add_argument("-e", "--editable", action="store_true")

    parser.add_argument("-l", "--local", action="store_true")

    parser.add_argument("--user", action="store_true")

    parser.add_argument("--path", action="append", default=[])

    parser.add_argument("--not-required", action="store_true")

    parser.add_argument("--exclude", action="append", default=[])

    parser.add_argument("--find-links", "-f", action="append", default=[])

    parser.add_argument("--index-url", "-i")

    parser.add_argument("--extra-index-url", action="append", default=[])

    parser.add_argument("--no-index", action="store_true")

    parser.add_argument("--pre", action="store_true")

    parser.add_argument("--all-releases", action="append", default=[])

    parser.add_argument("--only-final", action="append", default=[])

    parser.add_argument(
        "--exclude-editable",
        action="store_false",
        dest="include_editable",
        default=True,
    )

    parser.add_argument(
        "--include-editable",
        action="store_true",
        dest="include_editable",
    )

    parser.add_argument("-v", "--verbose", action="count", default=0)

    parser.add_argument(
        "--format",
        choices=("columns", "json", "freeze"),
        default="columns",
    )

    return parser
