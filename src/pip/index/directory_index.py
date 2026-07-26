"""Cached indexing of local package source directories."""

from __future__ import annotations

import mimetypes
import os
from collections import defaultdict
from pathlib import Path

from pip.core.packaging import Version, canonicalize_name
from pip.core.urls import path_to_url
from pip.core.wheel import parse_wheel_filename
from pip.index.links import SOURCE_ARCHIVE_SUFFIXES


def local_source_files(path: Path) -> tuple[Path, ...]:
    if not path.is_dir():
        return ()
    with os.scandir(path) as entries:
        return tuple(Path(entry.path) for entry in entries if entry.is_file())


class DirectoryIndex:
    """Cached page and artifact URLs discovered in a local source directory."""

    def __init__(self, path: str) -> None:
        self.path_internal = path
        self.page_candidates_internal: list[str] = []
        self.project_name_to_urls_internal: dict[str, list[str]] = defaultdict(list)
        self.scanned = False

    def scan(self) -> None:
        for entry in local_source_files(Path(self.path_internal)):
            url = path_to_url(str(entry))
            if is_html_file(url):
                self.page_candidates_internal.append(url)
                continue
            wheel = parse_wheel_filename(entry.name)
            if wheel is not None:
                project_name = wheel[0]
            else:
                parsed = project_version_from_filename(entry.name)
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
