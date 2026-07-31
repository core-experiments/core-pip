"""Cached indexing of local package source directories."""

from __future__ import annotations

import mimetypes
import os
from collections import defaultdict
from pathlib import Path

from cpip.core.packaging import Version, canonicalize_name
from cpip.core.urls import path_to_url
from cpip.index.links import SOURCE_ARCHIVE_SUFFIXES


def local_source_files(path: Path) -> tuple[Path, ...]:
    if not os.path.isdir(os.fspath(path)):
        return ()
    with os.scandir(os.fspath(path)) as entries:
        return tuple(Path(entry.path) for entry in entries if entry.is_file())


class DirectoryIndex:
    """Cached page and artifact URLs discovered in a local source directory."""

    def __init__(self, path: str) -> None:
        self.path_internal = path
        self.page_candidates_internal: list[str] = []
        self.project_name_to_urls_internal: dict[str, list[str]] = defaultdict(list)
        self.scanned = False

    def scan(self) -> None:
        try:
            entries = os.scandir(self.path_internal)
        except OSError:
            self.scanned = True
            return
        with entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                url = path_to_url(entry.path)
                filename = entry.name
                # The common simple-index page suffixes can be recognized
                # without invoking mimetypes for every wheel and archive.
                if filename.lower().endswith((".html", ".htm", ".html.gz", ".htm.gz")) and is_html_file(url):
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
        return name.replace("_", "-"), Version(version)
    except ValueError:
        return None
