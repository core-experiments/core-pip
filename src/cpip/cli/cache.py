"""The ``cpip cache`` command and the manager it drives."""

from __future__ import annotations

import builtins
import fnmatch
import os
import sys

from cpip.cli.parsers.cache import create_parser
from cpip.core.appdirs import user_cache_dir
from cpip.core.errors import CommandError


class CacheManager:
    """Inspect and remove files from cpip's cache directories."""

    def __init__(self, cache_dir: str | None = None) -> None:
        self.cache_dir = os.path.normcase(cache_dir or user_cache_dir("cpip"))
        self.http_dir = os.path.join(self.cache_dir, "http-v2")
        self.wheel_dir = os.path.join(self.cache_dir, "wheels")
        self.archive_dir = os.path.join(self.cache_dir, "archive-v1")
        self.artifact_dir = os.path.join(self.cache_dir, "artifacts-v1")
        self.fast_install_tree_dir = os.path.join(
            self.cache_dir,
            "fast-install-trees-v1",
        )
        self.resolution_dir = os.path.join(self.cache_dir, "resolution-v2")
        self.legacy_resolution_dir = os.path.join(
            self.cache_dir,
            "resolution-v1",
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
        root_text = root
        if not os.path.isdir(root_text):
            return []
        return [
            os.path.join(current, name)
            for current, _, files in os.walk(root_text, followlinks=False)
            for name in files
        ]

    def list(self, pattern: str | None, *, absolute: bool) -> builtins.list[str]:
        wheels = self.wheel_files()
        if pattern:
            expression = (
                pattern if any(char in pattern for char in "*?[]") else f"*{pattern}*"
            )
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
                for root in (
                    self.http_dir,
                    self.wheel_dir,
                    self.archive_dir,
                    self.artifact_dir,
                    self.fast_install_tree_dir,
                    self.resolution_dir,
                    self.legacy_resolution_dir,
                    os.path.join(self.cache_dir, "http"),
                )
                for path in self._files_under(root)
            ]
            files.extend(
                path
                for version in (1, 2, 3)
                if os.path.isfile(
                    path := os.path.join(
                        self.cache_dir,
                        f"fast-install-v{version}.marshal",
                    ),
                )
            )
        else:
            files = [
                path
                for path in self.wheel_files()
                if pattern is not None
                and fnmatch.fnmatch(
                    os.path.basename(path),
                    pattern
                    if any(char in pattern for char in "*?[]")
                    else f"*{pattern}*",
                )
            ]

        if not files:
            if purge:
                print("WARNING: No matching packages", file=sys.stderr)
            elif pattern is not None:
                print(
                    f'WARNING: No matching packages for pattern "{pattern}"',
                    file=sys.stderr,
                )
            return 0, 0, 0

        bytes_removed = 0
        for path in files:
            try:
                bytes_removed += os.stat(path).st_size
            except OSError:
                pass
            if verbose:
                print(f"Removed {path}")
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

        selfcheck = os.path.join(self.cache_dir, "selfcheck.json")
        if purge and os.path.isfile(selfcheck):
            os.unlink(selfcheck)
            print("Removed legacy selfcheck.json file")

        directories_removed = 0
        directories = [
            os.path.join(current, name)
            for current, directory_names, _ in os.walk(
                self.cache_dir,
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
        return len(files), bytes_removed, directories_removed

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
