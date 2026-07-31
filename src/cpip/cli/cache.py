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
        if not self.wheel_dir.is_dir():
            return []
        return sorted(path for path in self.wheel_dir.rglob("*.whl") if path.is_file())

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
        self, pattern: str | None, *, purge: bool, verbose: bool
    ) -> tuple[int, int, int]:
        if purge:
            files = [
                path
                for root in (self.http_dir, self.wheel_dir, self.cache_dir / "http")
                if root.is_dir()
                for path in root.rglob("*")
                if path.is_file()
            ]
        else:
            files = [
                path
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
                bytes_removed += path.stat().st_size
            except OSError:
                pass
            if verbose:
                print(f"Removed {path}")
            path.unlink(missing_ok=True)

        selfcheck = self.cache_dir / "selfcheck.json"
        if purge and selfcheck.is_file():
            selfcheck.unlink()
            print("Removed legacy selfcheck.json file")

        directories_removed = 0
        for directory in sorted(
            (path for path in self.cache_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
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
