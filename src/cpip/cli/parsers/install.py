"""Argument parser for ``cpip install``.

Kept apart from the command module so that ``cpip install --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip install", allow_abbrev=False)
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("--group", dest="groups", action="append", default=[])
    parser.add_argument(
        "--requirements-from-script",
        dest="requirements_from_scripts",
        action="append",
        default=[],
    )
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
    parser.add_argument("--cert")
    parser.add_argument("--client-cert")
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument(
        "--keyring-provider",
        choices=("auto", "disabled", "import", "subprocess"),
        default="auto",
    )
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--use-pep517", action="store_true")
    parser.add_argument("--use-deprecated", action="append", default=[])
    parser.add_argument(
        "--use-feature",
        dest="use_features",
        action="append",
        default=[],
    )
    parser.add_argument("--disable-cpip-version-check", action="store_true")
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--no-compile", action="store_true")
    parser.add_argument("-U", "--upgrade", action="store_true")
    parser.add_argument(
        "--upgrade-strategy",
        choices=("only-if-needed", "eager"),
        default="only-if-needed",
    )
    parser.add_argument("-I", "--ignore-installed", action="store_true")
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--no-user", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--prefix")
    parser.add_argument("-t", "--target")
    parser.add_argument("--cache-dir")
    parser.add_argument("--no-cache-dir", action="store_true")
    parser.add_argument("--no-binary", action="append", default=[])
    parser.add_argument("--only-binary", action="append", default=[])
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--implementation")
    parser.add_argument("--python-version")
    parser.add_argument("--abi", action="append", default=[])
    parser.add_argument("--pre", action="store_true")
    parser.add_argument("--all-releases", action="append", default=[])
    parser.add_argument("--only-final", action="append", default=[])
    parser.add_argument("--ignore-requires-python", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--uploaded-prior-to")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="count", default=0)
    parser.add_argument("--no-warn-script-location", action="store_true")
    parser.add_argument("--no-warn-conflicts", action="store_true")
    parser.add_argument(
        "--config-settings",
        "--config-setting",
        dest="config_settings",
        action="append",
        default=[],
    )
    parser.add_argument("--require-hashes", action="store_true")
    return parser
