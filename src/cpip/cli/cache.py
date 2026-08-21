"""The ``cpip cache`` command and the manager it drives."""

from __future__ import annotations

import builtins
import fnmatch
import glob
import os
import sys

from cpip.cli.fast import FAST_LOCK_PLAN_BUCKET
from cpip.cli.fast_install import NAME_FAMILY as FAST_INSTALL_SNAPSHOT_FAMILY
from cpip.cli.fast_install import TREE_CACHE_BUCKET
from cpip.cli.parsers.cache import create_parser
from cpip.core.appdirs import resolve_cache_dir
from cpip.core.errors import CommandError
from cpip.index.artifact_cache import ARTIFACT_CACHE_BUCKET
from cpip.index.cache import WHEEL_CACHE_BUCKET
from cpip.index.candidate_metadata_cache import NAME as CANDIDATE_METADATA_NAME
from cpip.index.metadata_cache import NAME as WHEEL_METADATA_NAME
from cpip.index.release_facts_cache import NAME as RELEASE_FACTS_NAME
from cpip.install.wheel_archive_cache import ARCHIVE_CACHE_BUCKET_FAMILY
from cpip.install.wheel_install_plan_cache import RESOLUTION_CACHE_BUCKET_FAMILY
from cpip.network.cache import HTTP_CACHE_BUCKET


class CacheManager:
    """Inspect and remove files from cpip's cache directories."""

    def __init__(self, cache_dir: str | None = None) -> None:
        # The same resolution every writer uses (explicit, then
        # CPIP_CACHE_DIR, then the platform default), so the manager looks
        # where the caches actually are.
        self.cache_dir = os.path.normcase(resolve_cache_dir(cache_dir))
        self.http_dir = os.path.join(self.cache_dir, HTTP_CACHE_BUCKET)
        self.wheel_dir = os.path.join(self.cache_dir, WHEEL_CACHE_BUCKET)
        # The archive and resolution buckets are interpreter-tagged (one
        # bucket per interpreter/implementation, since they hold
        # marshal-serialized data); glob every tagged variant rather than
        # just the running interpreter's own, so purge clears caches left by
        # other interpreters too.
        self.archive_dirs = self._glob(f"{ARCHIVE_CACHE_BUCKET_FAMILY}-*")
        self.artifact_dir = os.path.join(self.cache_dir, ARTIFACT_CACHE_BUCKET)
        self.fast_install_tree_dir = os.path.join(self.cache_dir, TREE_CACHE_BUCKET)
        self.fast_lock_plan_dir = os.path.join(self.cache_dir, FAST_LOCK_PLAN_BUCKET)
        self.resolution_dirs = self._glob(f"{RESOLUTION_CACHE_BUCKET_FAMILY}-*")

    def _glob(self, pattern: str) -> builtins.list[str]:
        """Expand ``pattern`` directly under the cache root; the root itself
        is escaped so a directory name with glob metacharacters still matches."""
        return glob.glob(os.path.join(glob.escape(self.cache_dir), pattern))

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
                    *self.archive_dirs,
                    self.artifact_dir,
                    self.fast_install_tree_dir,
                    self.fast_lock_plan_dir,
                    *self.resolution_dirs,
                )
                for path in self._files_under(root)
            ]
            # Single-file caches: the per-interpreter fast-install snapshots
            # and the SQLite/marshal stores. The trailing wildcard also takes
            # SQLite's -wal/-shm sidecars and the .<pid>.tmp files an
            # interrupted save_snapshot leaves behind.
            files.extend(
                path
                for pattern in (
                    f"{FAST_INSTALL_SNAPSHOT_FAMILY}-*.marshal*",
                    f"{CANDIDATE_METADATA_NAME}*",
                    f"{WHEEL_METADATA_NAME}*",
                    f"{RELEASE_FACTS_NAME}*",
                )
                for path in self._glob(pattern)
                if os.path.isfile(path)
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

        # A purge sweeps empty bucket directories even when no file was left
        # to remove, so a second purge finishes the job instead of warning.
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

        print(os.path.normcase(resolve_cache_dir()))

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
