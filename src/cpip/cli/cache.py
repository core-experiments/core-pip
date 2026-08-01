"""Management commands for cpip's on-disk caches."""

from __future__ import annotations

import builtins
import fnmatch
import os
import sys
from pathlib import Path

from cpip.core.appdirs import user_cache_dir


class CacheManager:
    """Inspect and remove files from cpip's cache directories."""

    def __init__(self, cache_dir: str | None = None) -> None:
        self.cache_dir = Path(os.path.normcase(cache_dir or user_cache_dir("cpip")))
        self.http_dir = self.cache_dir / "http-v2"
        self.wheel_dir = self.cache_dir / "wheels"

    def wheel_files(self) -> builtins.list[Path]:
        wheel_dir = os.fspath(self.wheel_dir)
        if not os.path.isdir(wheel_dir):
            return []
        return sorted(
            Path(os.path.join(current, name))
            for current, _, files in os.walk(wheel_dir, followlinks=False)
            for name in files
            if name.endswith(".whl")
        )

    @staticmethod
    def _files_under(root: Path) -> builtins.list[str]:
        root_text = os.fspath(root)
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
            wheels = [path for path in wheels if fnmatch.fnmatch(path.name, expression)]
        if absolute:
            return [os.fspath(path) for path in wheels]
        if not wheels:
            return []
        return [f" - {path.name} ({path.parent})" for path in wheels]

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
                for root in (self.http_dir, self.wheel_dir, self.cache_dir / "http")
                for path in self._files_under(root)
            ]
        else:
            files = [
                os.fspath(path)
                for path in self.wheel_files()
                if pattern is not None
                and fnmatch.fnmatch(
                    path.name,
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

        selfcheck = self.cache_dir / "selfcheck.json"
        if purge and os.path.isfile(os.fspath(selfcheck)):
            os.unlink(os.fspath(selfcheck))
            print("Removed legacy selfcheck.json file")

        directories_removed = 0
        directories = [
            os.path.join(current, name)
            for current, directory_names, _ in os.walk(
                os.fspath(self.cache_dir),
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
            os.fspath(self.http_dir),
            os.fspath(self.wheel_dir),
            len(self.wheel_files()),
        )
