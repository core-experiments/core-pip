"""Package source locations and their link collection behavior."""

from __future__ import annotations

import ntpath
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pip.core.packaging import Requirement, canonicalize_name
from pip.core.urls import path_to_url, url_to_path
from pip.index.directory_index import (
    local_source_files,
)
from pip.index.links import Link
from pip.index.page_parsing import IndexPageParser
from pip.index.source_models import ArtifactKind

SUPPORTED_SCHEMES = frozenset(("http", "https", "file", "ftp"))
VCS_SCHEMES = frozenset(("git", "hg", "svn", "bzr"))
HTML_SUFFIXES = frozenset((".html", ".htm"))


def is_supported_location(value: str) -> bool:
    scheme = urllib.parse.urlsplit(value).scheme
    vcs_scheme = scheme.partition("+")[0]
    return scheme in SUPPORTED_SCHEMES or vcs_scheme in VCS_SCHEMES


def resolve_source_location(location: str) -> tuple[str | None, str | None]:
    """Return the normalized URL and local path represented by a source option."""
    if os.path.exists(location):
        absolute_location = os.path.abspath(location)
        return path_to_url(absolute_location), absolute_location
    if location.startswith("file:"):
        return location, url_to_path(location)
    if is_supported_location(location):
        return location, None
    return None, None


@dataclass(frozen=True)
class FindLinksSource:
    links: tuple[str, ...]
    trusted_hosts: tuple[str, ...] = ()
    session: Any = None

    def collect_links(self, requirement: Requirement) -> list[Link]:
        links: list[Link] = []
        for link in self.links:
            links.extend(self.links_from_find_link(link))
        return links

    def links_from_find_link(self, link: str) -> list[Link]:
        normalized, local = resolve_source_location(link)
        if local is not None:
            return self.links_from_local_path(Path(local))
        if normalized is None:
            return []
        candidate = Link.from_url(normalized, source_url=None)
        if urllib.parse.urlparse(normalized).fragment.startswith("egg="):
            return [candidate]
        if candidate.kind is not ArtifactKind.UNKNOWN:
            return [candidate]
        return IndexPageParser(
            trusted_hosts=self.trusted_hosts, session=self.session
        ).links_from_url(normalized)

    def links_from_local_path(self, path: Path) -> list[Link]:
        if path.is_file():
            if path.suffix.lower() in HTML_SUFFIXES:
                return IndexPageParser(
                    trusted_hosts=self.trusted_hosts, session=self.session
                ).links_from_url(path.as_uri())
            return [Link.from_path(path, source_url=None)]
        if not path.is_dir():
            return []
        return [
            Link.from_path(item, source_url=str(path), is_dir=False)
            for item in local_source_files(path)
        ]


@dataclass(frozen=True)
class SimpleIndexSource:
    index_url: str
    trusted_hosts: tuple[str, ...] = ()
    session: Any = None

    def collect_links(self, requirement: Requirement) -> list[Link]:
        project_url = self.project_page_url(self.index_url, requirement.canonical_name)
        return IndexPageParser(
            trusted_hosts=self.trusted_hosts, session=self.session
        ).links_from_url(project_url)

    @staticmethod
    def project_page_url(index_url: str, canonical_name: str) -> str:
        return urllib.parse.urljoin(
            index_url if index_url.endswith("/") else index_url + "/",
            canonicalize_name(canonical_name) + "/",
        )


def looks_like_path_requirement(value: str) -> bool:
    return (
        value.startswith((".", "/", "~"))
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
        or bool(ntpath.splitdrive(value)[0])
    )
