"""Argument parser for ``cpip cache``.

Kept apart from the command module so that ``cpip cache --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip cache")

    parser.add_argument("command", choices=("dir", "info", "list", "remove", "purge"))

    parser.add_argument("pattern", nargs="?")

    parser.add_argument("--format", choices=("human", "abspath"), default="human")

    parser.add_argument("--cache-dir")

    parser.add_argument("--no-cache-dir", action="store_true")

    parser.add_argument("-v", "--verbose", action="count", default=0)

    return parser
