"""The ``cpip cache`` command and the manager it drives."""

from __future__ import annotations

import builtins
import fnmatch
import glob
import os
import sys

from cpip.cli.parsers.cache import create_parser
from cpip.core.appdirs import (
    WHEEL_CACHE_BUCKET,
    cache_root,
    http_cache_path,
    versioned_cache_dir,
)
from cpip.core.errors import CommandError


def _match_expression(pattern: str) -> str:
    """A glob pattern as given; plain text matches as a substring."""
    return pattern if any(char in pattern for char in "*?[]") else f"*{pattern}*"


class CacheManager:
    """Inspect and remove files from cpip's cache directories.

    Every persisted cache lives under one ``v<N>`` directory of the cache
    root, so the manager never needs to know the individual stores: a purge
    removes every ``v*`` directory, and list/info/remove work on this
    version's built-wheel bucket.
    """

    def __init__(self, cache_dir: str | None = None) -> None:
        # The same resolution every writer uses (explicit, then
        # CPIP_CACHE_DIR, then the platform default), so the manager looks
        # where the caches actually are.
        self.root = os.path.normcase(cache_root(cache_dir))
        self.cache_dir = versioned_cache_dir(self.root)
        self.http_dir = http_cache_path(self.cache_dir)
        self.wheel_dir = os.path.join(self.cache_dir, WHEEL_CACHE_BUCKET)

    def version_dirs(self) -> builtins.list[str]:
        """Every ``v<N>`` cache directory under the root, this cpip's or another's."""
        # The root is escaped so a directory name with glob metacharacters
        # still matches.
        return sorted(
            path
            for path in glob.glob(os.path.join(glob.escape(self.root), "v*"))
            if os.path.isdir(path)
        )

    def wheel_files(self) -> builtins.list[str]:
        wheel_dir = self.wheel_dir
        if not os.path.isdir(wheel_dir):
            return []
        return sorted(
            os.path.join(current, name)
            for current, _, files in os.walk(wheel_dir, followlinks=False)
            for name in files
            if name.endswith(".whl")
        )

    @staticmethod
    def _files_under(root: str) -> builtins.list[str]:
        if not os.path.isdir(root):
            return []
        return [
            os.path.join(current, name)
            for current, _, files in os.walk(root, followlinks=False)
            for name in files
        ]

    def list(self, pattern: str | None, *, absolute: bool) -> builtins.list[str]:
        wheels = self.wheel_files()
        if pattern:
            expression = _match_expression(pattern)
            wheels = [
                path
                for path in wheels
                if fnmatch.fnmatch(os.path.basename(path), expression)
            ]
        if absolute:
            return wheels
        if not wheels:
            return []
        return [
            f" - {os.path.basename(path)} ({os.path.dirname(path)})" for path in wheels
        ]

    def remove(
        self,
        pattern: str | None,
        *,
        purge: bool,
        verbose: bool,
    ) -> tuple[int, int, int]:
        if purge:
            files = [
                path
                for version_dir in self.version_dirs()
                for path in self._files_under(version_dir)
            ]
        else:
            expression = None if pattern is None else _match_expression(pattern)
            files = [
                path
                for path in self.wheel_files()
                if expression is not None
                and fnmatch.fnmatch(os.path.basename(path), expression)
            ]

        if not files and not purge:
            if pattern is not None:
                print(
                    f'WARNING: No matching packages for pattern "{pattern}"',
                    file=sys.stderr,
                )
            return 0, 0, 0

        files_removed = 0
        bytes_removed = 0
        for path in files:
            try:
                size = os.stat(path).st_size
            except OSError:
                size = 0
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                # Another cpip may hold a store open (Windows refuses to
                # unlink an open SQLite file): report it and keep going.
                print(f"WARNING: Could not remove {path}: {error}", file=sys.stderr)
                continue
            files_removed += 1
            bytes_removed += size
            if verbose:
                print(f"Removed {path}")

        # A purge sweeps empty directories even when no file was left to
        # remove, so a second purge finishes the job instead of warning.
        directories_removed = 0
        directories = [
            os.path.join(current, name)
            for current, directory_names, _ in os.walk(
                self.root,
                topdown=False,
                followlinks=False,
            )
            for name in directory_names
        ]
        for directory in directories:
            try:
                os.rmdir(directory)
            except OSError:
                continue
            directories_removed += 1
        if purge and not files_removed and not directories_removed:
            print("WARNING: No matching packages", file=sys.stderr)
        return files_removed, bytes_removed, directories_removed

    def info(self) -> tuple[str, str, int]:
        return (
            self.http_dir,
            self.wheel_dir,
            len(self.wheel_files()),
        )


def run_cache(args: list[str]) -> int:
    parser = create_parser()

    options = parser.parse_args(args)

    if options.command == "dir":
        if options.pattern or options.cache_dir or options.no_cache_dir:
            raise CommandError("Too many arguments")

        print(os.path.normcase(cache_root()))

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

        print(f"Package index page cache location: {http_dir}")

        print(f"Locally built wheels location: {wheel_dir}")

        print(f"Number of locally built wheels: {wheel_count}")

        return 0

    if options.command == "list":
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
