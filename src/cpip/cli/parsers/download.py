"""Argument parser for ``cpip download``.

Kept apart from the command module so that ``cpip download --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip download")

    parser.add_argument("requirements", nargs="*")

    parser.add_argument("--group", dest="groups", action="append", default=[])

    parser.add_argument(
        "-r",
        "--requirement",
        dest="requirement_files",
        action="append",
        default=[],
    )

    parser.add_argument(
        "-c",
        "--constraint",
        dest="constraint_files",
        action="append",
        default=[],
    )

    parser.add_argument("-f", "--find-links", action="append", default=[])

    parser.add_argument("-i", "--index-url")

    parser.add_argument("--extra-index-url", action="append", default=[])

    parser.add_argument(
        "--trusted-host",
        dest="trusted_hosts",
        action="append",
        default=[],
    )

    parser.add_argument("--proxy")

    parser.add_argument("--cert")

    parser.add_argument("--client-cert")

    parser.add_argument("--no-index", action="store_true")

    parser.add_argument("--no-build-isolation", action="store_true")

    parser.add_argument("--no-cache-dir", action="store_true")

    parser.add_argument("--no-color", action="store_true")

    parser.add_argument("-d", "--dest", required=True)

    return parser
