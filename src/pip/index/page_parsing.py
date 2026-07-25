"""Parsing helpers for Simple API HTML and JSON pages."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from pip.index.artifacts import ArtifactLocator
from pip.index.datetime import parse_iso_datetime
from pip.index.hashes import SUPPORTED_RECORD_HASHES
from pip.index.links import Link
from pip.index.source_models import MetadataFile

LinkFactory = Callable[..., Link]


@dataclass(frozen=True)
class IndexContent:
    body: str
    content_type: str


class IndexPageParser:
    """Read and parse one Simple API page into canonical links."""

    def __init__(
        self,
        link_factory: LinkFactory = Link.from_url,
        trusted_hosts: tuple[str, ...] = (),
        session: Any = None,
    ) -> None:
        self.link_factory = link_factory
        self.trusted_hosts = {host.lower() for host in trusted_hosts}
        self.session = session
        self.artifacts = ArtifactLocator(session)

    def links_from_url(self, url: str) -> list[Link]:
        try:
            content = self.read(url)
        except (OSError, urllib.error.URLError):
            return []
        if content.content_type.endswith("+json") or "json" in content.content_type:
            return self.links_from_json(content.body, url)
        return self.links_from_html(content.body, url)

    def read(self, url: str) -> IndexContent:
        local = self.artifacts.local_path(url)
        if local is not None:
            if local.is_dir():
                json_path = local / "index.json"
                if json_path.exists():
                    return IndexContent(
                        json_path.read_text(encoding="utf-8"),
                        "application/vnd.pypi.simple.v1+json",
                    )
                local = local / "index.html"
            return IndexContent(local.read_text(encoding="utf-8"), "text/html")

        headers = {
            "Accept": (
                "application/vnd.pypi.simple.v1+json, "
                "text/html;q=0.2, application/vnd.pypi.simple.v1+html;q=0.2"
            )
        }
        if self.session is not None:
            response = self.session.get(url, headers=headers)
            response.raise_for_status()
            return IndexContent(
                response.text,
                response.headers.get("Content-Type", "text/html").split(";", 1)[0],
            )
        request = urllib.request.Request(url, headers=headers)
        parsed = urllib.parse.urlsplit(url)
        context = (
            ssl._create_unverified_context()
            if parsed.hostname and parsed.hostname.lower() in self.trusted_hosts
            else None
        )
        with urllib.request.urlopen(request, context=context) as response:
            content_type = response.headers.get_content_type()
            return IndexContent(
                response.read().decode("utf-8", "replace"), content_type
            )

    def links_from_html(self, body: str, url: str) -> list[Link]:
        parser = _LinkParser(url, self.link_factory)
        parser.feed(body)
        return parser.links

    def links_from_json(self, body: str, url: str) -> list[Link]:
        data = json.loads(body)
        links: list[Link] = []
        for file_data in data.get("files", []):
            if not isinstance(file_data, dict):
                continue
            file_url = file_data.get("url")
            filename = file_data.get("filename")
            if not isinstance(file_url, str):
                continue
            absolute = urllib.parse.urljoin(_ensure_trailing_slash(url), file_url)
            hashes = file_data.get("hashes")
            yanked = file_data.get("yanked")
            links.append(
                self.link_factory(
                    absolute,
                    source_url=url,
                    text=str(filename or ""),
                    hashes=hashes if isinstance(hashes, dict) else None,
                    requires_python=file_data.get("requires-python")
                    if isinstance(file_data.get("requires-python"), str)
                    else None,
                    yanked_reason=(
                        None
                        if yanked is False or yanked is None
                        else ""
                        if yanked is True
                        else str(yanked)
                    ),
                    metadata_file=_metadata_file_from_json(file_data),
                    upload_time=(
                        parse_iso_datetime(file_data["upload-time"])
                        if file_data.get("upload-time")
                        else None
                    ),
                )
            )
        return links


class _LinkParser(HTMLParser):
    def __init__(self, page_url: str, link_factory: LinkFactory) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.link_factory = link_factory
        self.links: list[Link] = []
        self._current: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._current = {name.lower(): value for name, value in attrs}
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current is None:
            return
        href = self._current.get("href")
        if href:
            self.links.append(
                self.link_factory(
                    urllib.parse.urljoin(_ensure_trailing_slash(self.page_url), href),
                    source_url=self.page_url,
                    text="".join(self._text).strip(),
                    requires_python=self._current.get("data-requires-python"),
                    yanked_reason=self._current.get("data-yanked"),
                    metadata_file=_metadata_file_from_attrs(self._current),
                )
            )
        self._current = None
        self._text = []


def _metadata_file_from_attrs(attrs: dict[str, str | None]) -> MetadataFile | None:
    if "data-core-metadata" in attrs:
        return _metadata_file_from_value(attrs.get("data-core-metadata"))
    if "data-dist-info-metadata" in attrs:
        return _metadata_file_from_value(attrs.get("data-dist-info-metadata"))
    return None


def _metadata_file_from_json(file_data: dict[str, Any]) -> MetadataFile | None:
    if "core-metadata" in file_data:
        return _metadata_file_from_json_value(file_data["core-metadata"])
    if "dist-info-metadata" in file_data:
        return _metadata_file_from_json_value(file_data["dist-info-metadata"])
    return None


def _metadata_file_from_json_value(value: Any) -> MetadataFile | None:
    if isinstance(value, dict):
        return MetadataFile({str(name): str(hash_) for name, hash_ in value.items()})
    if value is True:
        return MetadataFile(None)
    return None


def _metadata_file_from_value(value: str | None) -> MetadataFile | None:
    if value is None:
        return None
    if value in {"", "true"}:
        return MetadataFile(None)
    name, sep, digest = value.partition("=")
    return MetadataFile(
        {name: digest} if sep and name in SUPPORTED_RECORD_HASHES else None
    )


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"
