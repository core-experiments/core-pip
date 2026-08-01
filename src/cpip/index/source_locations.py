"""Package source locations and their link collection behavior."""

from __future__ import annotations

import ntpath
import os
import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import Any

from cpip.core.packaging import Requirement, canonicalize_name
from cpip.core.urls import path_to_url, url_to_path
from cpip.index.directory_index import (
    LocalSourceSnapshot,
    local_source_snapshot,
)
from cpip.index.links import Link
from cpip.index.source_models import ArtifactKind

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


class FindLinksSource:
    __slots__ = ("local_snapshots", "links", "session", "trusted_hosts")

    def __init__(
        self,
        links: tuple[str, ...],
        trusted_hosts: tuple[str, ...] = (),
        session: Any = None,
    ) -> None:
        self.links = links
        self.trusted_hosts = trusted_hosts
        self.session = session
        self.local_snapshots: dict[str, LocalSourceSnapshot] = {}

    def collect_links(self, requirement: Requirement) -> list[Link]:
        links: list[Link] = []
        for link in self.links:
            links.extend(self.links_from_find_link(link))
        return links

    def refresh_local_sources(self, path: str | None = None) -> None:
        """Explicitly invalidate local discovery state."""
        if path is None:
            self.local_snapshots.clear()
        else:
            self.local_snapshots.pop(os.fspath(Path(path)), None)

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
        from cpip.index.page_parsing import IndexPageParser

        return IndexPageParser(
            trusted_hosts=self.trusted_hosts,
            session=self.session,
        ).links_from_url(normalized)

    def links_from_local_path(self, path: Path) -> list[Link]:
        path_text = os.fspath(path)
        if os.path.isfile(path_text):
            if os.path.splitext(path_text)[1].lower() in HTML_SUFFIXES:
                from cpip.index.page_parsing import IndexPageParser

                return IndexPageParser(
                    trusted_hosts=self.trusted_hosts,
                    session=self.session,
                ).links_from_url(path.as_uri())
            return [Link.from_path(path, source_url=None)]
        snapshot = self.local_snapshots.get(path_text)
        if snapshot is None:
            snapshot = local_source_snapshot(path)
            if snapshot is None:
                return []
            self.local_snapshots[path_text] = snapshot
        return [
            Link.from_path(
                item.path,
                source_url=str(path),
                is_dir=False,
                local_identity=item.identity,
            )
            for item in snapshot.entries
        ]


class SimpleIndexSource:
    __slots__ = ("index_url", "session", "trusted_hosts")

    def __init__(
        self,
        index_url: str,
        trusted_hosts: tuple[str, ...] = (),
        session: Any = None,
    ) -> None:
        self.index_url = index_url
        self.trusted_hosts = trusted_hosts
        self.session = session

    def collect_links(self, requirement: Requirement) -> list[Link]:
        from cpip.index.page_parsing import IndexPageParser

        project_url = self.project_page_url(self.index_url, requirement.canonical_name)
        return IndexPageParser(
            trusted_hosts=self.trusted_hosts,
            session=self.session,
        ).links_from_url(project_url)

    @staticmethod
    def project_page_url(index_url: str, canonical_name: str) -> str:
        return urllib.parse.urljoin(
            index_url if index_url.endswith("/") else index_url + "/",
            canonicalize_name(canonical_name) + "/",
        )


@lru_cache(maxsize=4096)
def looks_like_path_requirement(value: str) -> bool:
    return (
        value.startswith((".", "/", "~"))
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
        or bool(ntpath.splitdrive(value)[0])
    )
