"""Management commands for cpip's on-disk caches."""

from __future__ import annotations

import builtins
import fnmatch
import os
import sys

from cpip.core.appdirs import user_cache_dir


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
            expression = pattern if any(char in pattern for char in "*?[]") else f"*{pattern}*"
            wheels = [
                path for path in wheels if fnmatch.fnmatch(os.path.basename(path), expression)
            ]
        if absolute:
            return wheels
        if not wheels:
            return []
        return [f" - {os.path.basename(path)} ({os.path.dirname(path)})" for path in wheels]

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
                    pattern if any(char in pattern for char in "*?[]") else f"*{pattern}*",
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
