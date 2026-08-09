"""Argument parser for ``cpip freeze``.

Kept apart from the command module so that ``cpip freeze --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

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
