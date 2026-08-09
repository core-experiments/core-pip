"""Argument parser for ``cpip wheel``.

Kept apart from the command module so that ``cpip wheel --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    """Build wheels for requirements without installing them."""

    parser = ArgumentParser(prog="cpip wheel")

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

    parser.add_argument(
        "--build-constraint",
        dest="build_constraint_files",
        action="append",
        default=[],
    )

    parser.add_argument(
        "-e",
        "--editable",
        dest="editables",
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

    parser.add_argument(
        "--use-feature",
        dest="use_features",
        action="append",
        default=[],
    )

    parser.add_argument("--no-index", action="store_true")

    parser.add_argument("--no-build-isolation", action="store_true")

    parser.add_argument("--no-deps", action="store_true")

    parser.add_argument("-v", "--verbose", action="count", default=0)

    parser.add_argument(
        "--config-settings",
        "--config-setting",
        dest="config_settings",
        action="append",
        default=[],
    )

    parser.add_argument("-w", "--wheel-dir", default=".")

    return parser
