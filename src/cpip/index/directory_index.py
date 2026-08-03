"""Cached indexing of local package source directories."""

from __future__ import annotations

import mimetypes
import os
import stat
from collections import defaultdict
from typing import NamedTuple

from cpip.core.packaging import Version, canonicalize_name
from cpip.core.urls import path_to_url
from cpip.index.links import SOURCE_ARCHIVE_SUFFIXES


class LocalSourceEntry(NamedTuple):
    path: str
    identity: str
    stat_identity: tuple[int, int, int]


class LocalSourceSnapshot:
    """A single discovery view of a local package source directory.

    The identity is captured while the directory entry is already being
    inspected.  Consumers can carry it through candidate discovery and avoid
    issuing another stat for the same artifact later in resolution.
    """

    __slots__ = ("entries", "is_directory", "path")

    def __init__(
        self,
        path: str,
        entries: tuple[LocalSourceEntry, ...],
        *,
        is_directory: bool = True,
    ) -> None:
        self.path = path
        self.entries = entries
        self.is_directory = is_directory


def local_source_snapshot(
    path: str,
    *,
    suffixes: tuple[str, ...] | None = None,
) -> LocalSourceSnapshot | None:
    """Return one stable local-source snapshot, or ``None`` if not a directory."""
    path_text = os.fspath(path)
    normalized_suffixes = (
        tuple(suffix.lower() for suffix in suffixes) if suffixes is not None else None
    )
    try:
        entries = os.scandir(path_text)
    except NotADirectoryError:
        return LocalSourceSnapshot(path_text, (), is_directory=False)
    except OSError:
        return None
    discovered: list[LocalSourceEntry] = []
    with entries:
        for entry in entries:
            if normalized_suffixes is not None and not entry.name.lower().endswith(
                normalized_suffixes
            ):
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            identity = (
                f"stat:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
            )
            discovered.append(
                LocalSourceEntry(
                    entry.path,
                    identity,
                    (info.st_ino, info.st_size, info.st_mtime_ns),
                ),
            )
    return LocalSourceSnapshot(path_text, tuple(discovered))


def local_source_files(path: str) -> tuple[str, ...]:
    snapshot = local_source_snapshot(path)
    return () if snapshot is None else tuple(entry.path for entry in snapshot.entries)


class DirectoryIndex:
    """Cached page and artifact URLs discovered in a local source directory."""

    def __init__(self, path: str) -> None:
        self.path_internal = path
        self.page_candidates_internal: list[str] = []
        self.project_name_to_urls_internal: dict[str, list[str]] = defaultdict(list)
        self.scanned = False

    def scan(self) -> None:
        snapshot = local_source_snapshot(
            self.path_internal,
            suffixes=(
                ".html",
                ".htm",
                ".html.gz",
                ".htm.gz",
                ".whl",
                *SOURCE_ARCHIVE_SUFFIXES,
            ),
        )
        if snapshot is None:
            self.scanned = True
            return
        for item in snapshot.entries:
            url = path_to_url(item.path)
            filename = os.path.basename(item.path)
            # The common simple-index page suffixes can be recognized
            # without invoking mimetypes for every wheel and archive.
            if filename.lower().endswith(
                (".html", ".htm", ".html.gz", ".htm.gz"),
            ) and is_html_file(url):
                self.page_candidates_internal.append(url)
                continue
            wheel = parse_wheel_filename_fast(filename)
            if wheel is None and filename.endswith(".whl"):
                from cpip.core.wheel import parse_wheel_filename

                wheel = parse_wheel_filename(filename)
            if wheel is not None:
                project_name = wheel[0]
            else:
                parsed = project_version_from_filename(filename)
                if parsed is None:
                    continue
                project_name = canonicalize_name(parsed[0])
            self.project_name_to_urls_internal[project_name].append(url)
        self.scanned = True

    @property
    def page_candidates(self) -> list[str]:
        if not self.scanned:
            self.scan()
        return self.page_candidates_internal

    @property
    def project_name_to_urls(self) -> dict[str, list[str]]:
        if not self.scanned:
            self.scan()
        return self.project_name_to_urls_internal


def is_html_file(file_url: str) -> bool:
    return mimetypes.guess_type(file_url, strict=False)[0] == "text/html"


def parse_wheel_filename_fast(filename: str) -> tuple[str, str] | None:
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    distribution, version = parts[:2]
    python_tags, abi_tags, platform_tags = parts[-3:]
    if (
        not distribution
        or not version
        or not python_tags
        or abi_tags != "none"
        or platform_tags != "any"
        or not any(tag.startswith("py") for tag in python_tags.split("."))
    ):
        return None
    try:
        parsed_version = Version(version)
    except ValueError:
        return None
    return canonicalize_name(distribution), str(parsed_version)


def project_version_from_filename(filename: str) -> tuple[str, Version] | None:
    stem = filename
    for suffix in SOURCE_ARCHIVE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        return None
    name, sep, version = stem.rpartition("-")
    if not sep or not name or not version:
        return None
    try:
        return canonicalize_name(name), Version(version)
    except ValueError:
        return None
