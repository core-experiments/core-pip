"""Argument parsers for ``cpip check``, ``hash``, ``show``, and ``inspect``.

Kept apart from the command module so that ``--help`` on any of them builds a
parser without loading the machinery that runs the command.  One module hosts
all four because one command module implements all four.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_check_parser() -> ArgumentParser:
    return ArgumentParser(prog="cpip check")


def create_hash_parser() -> ArgumentParser:
    # Only this parser enumerates digest algorithms, so the other three do not
    # pay for hashlib.
    import hashlib

    parser = ArgumentParser(prog="cpip hash")
    parser.add_argument("files", nargs="+")
    parser.add_argument(
        "-a",
        "--algorithm",
        default="sha256",
        choices=sorted(hashlib.algorithms_available),
    )
    return parser


def create_show_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip show")
    parser.add_argument("-f", "--files", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("packages", nargs="*")
    return parser


def create_inspect_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip inspect")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    return parser
