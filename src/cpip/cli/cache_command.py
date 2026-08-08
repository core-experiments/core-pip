"""Implementation of the ``cpip cache`` command."""

from __future__ import annotations

import os

from cpip.cli.cache import CacheManager
from cpip.cli.parser import ArgumentParser
from cpip.core.appdirs import user_cache_dir
from cpip.core.errors import CommandError


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip cache")

    parser.add_argument("command", choices=("dir", "info", "list", "remove", "purge"))

    parser.add_argument("pattern", nargs="?")

    parser.add_argument("--format", choices=("human", "abspath"), default="human")

    parser.add_argument("--cache-dir")

    parser.add_argument("--no-cache-dir", action="store_true")

    parser.add_argument("-v", "--verbose", action="count", default=0)

    return parser


def run_cache(args: list[str]) -> int:
    parser = create_parser()

    options = parser.parse_args(args)

    if options.command == "dir":
        if options.pattern or options.cache_dir or options.no_cache_dir:
            raise CommandError("Too many arguments")

        print(os.path.normcase(user_cache_dir("cpip")))

        return 0

    if options.no_cache_dir:
        raise CommandError(
            "cpip cache commands can not function since cache is disabled.",
        )

    manager = CacheManager(options.cache_dir)

    if options.command == "info":
        if options.pattern:
            raise CommandError("Too many arguments")

        http_dir, wheel_dir, wheel_count = manager.info()

        print(f"Package index page cache location (cpip v23.3+): {http_dir}")

        print(f"Locally built wheels location: {wheel_dir}")

        print(f"Number of locally built wheels: {wheel_count}")

        return 0

    if options.command == "list":
        if options.pattern and options.pattern == "":
            parser.error("Too many arguments")

        lines = manager.list(options.pattern, absolute=options.format == "abspath")

        if not lines and options.format == "human":
            print("No locally built wheels cached.")

        else:
            print("\n".join(lines))

        return 0

    if options.command == "remove" and options.pattern is None:
        raise CommandError("Missing package name")

    if options.command == "purge" and options.pattern is not None:
        raise CommandError("Too many arguments")

    files, bytes_removed, directories = manager.remove(
        options.pattern,
        purge=options.command == "purge",
        verbose=bool(options.verbose),
    )

    print(f"Files removed: {files} ({bytes_removed} bytes)")

    print(f"Directories removed: {directories}")

    return 0
