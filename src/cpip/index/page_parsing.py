"""Parsing helpers for Simple API HTML and JSON pages."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from cpip.index.artifacts import ArtifactLocator
from cpip.index.catalog_cache import load_links, save_links
from cpip.index.datetime import parse_iso_datetime
from cpip.index.hashes import SUPPORTED_RECORD_HASHES
from cpip.index.links import Link
from cpip.index.source_models import MetadataFile

LinkFactory = Callable[..., Link]


class IndexContent:
    __slots__ = ("body", "content_type", "from_cache")

    def __init__(self, body: str, content_type: str, from_cache: bool = False) -> None:
        self.body = body
        self.content_type = content_type
        self.from_cache = from_cache


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
        if content.from_cache:
            cached = load_links(getattr(self.session, "cache", None), url)
            if cached is not None:
                return cached
        if content.content_type.endswith("+json") or "json" in content.content_type:
            links = self.links_from_json(content.body, url)
        else:
            links = self.links_from_html(content.body, url)
        if self.session is not None:
            save_links(getattr(self.session, "cache", None), url, links)
        return links

    def read(self, url: str) -> IndexContent:
        local = self.artifacts.local_path(url)
        if local is not None:
            local_text = os.fspath(local)
            if os.path.isdir(local_text):
                json_path = os.path.join(local_text, "index.json")
                if os.path.exists(json_path):
                    with open(json_path, encoding="utf-8") as file:
                        return IndexContent(
                            file.read(),
                            "application/vnd.pypi.simple.v1+json",
                        )
                local_text = os.path.join(local_text, "index.html")
            with open(local_text, encoding="utf-8") as file:
                return IndexContent(file.read(), "text/html")

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
                response.from_cache,
            )
        request = urllib.request.Request(url, headers=headers)
        parsed = urllib.parse.urlsplit(url)
        context = (
            getattr(ssl, "create_unverified_context")()
            if parsed.hostname and parsed.hostname.lower() in self.trusted_hosts
            else None
        )
        with urllib.request.urlopen(request, context=context) as response:
            content_type = response.headers.get_content_type()
            return IndexContent(
                response.read().decode("utf-8", "replace"), content_type
            )

    def links_from_html(self, body: str, url: str) -> list[Link]:
        parser = LinkParser(url, self.link_factory)
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
            absolute = urllib.parse.urljoin(ensure_trailing_slash(url), file_url)
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
                    metadata_file=metadata_file_from_json(file_data),
                    upload_time=(
                        parse_iso_datetime(file_data["upload-time"])
                        if file_data.get("upload-time")
                        else None
                    ),
                )
            )
        return links


class LinkParser(HTMLParser):
    def __init__(self, page_url: str, link_factory: LinkFactory) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.link_factory = link_factory
        self.links: list[Link] = []
        self.current_internal: dict[str, str | None] | None = None
        self.text_internal: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self.current_internal = {name.lower(): value for name, value in attrs}
        self.text_internal = []

    def handle_data(self, data: str) -> None:
        if self.current_internal is not None:
            self.text_internal.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self.current_internal is None:
            return
        href = self.current_internal.get("href")
        if href:
            self.links.append(
                self.link_factory(
                    urllib.parse.urljoin(ensure_trailing_slash(self.page_url), href),
                    source_url=self.page_url,
                    text="".join(self.text_internal).strip(),
                    requires_python=self.current_internal.get("data-requires-python"),
                    yanked_reason=self.current_internal.get("data-yanked"),
                    metadata_file=metadata_file_from_attrs(self.current_internal),
                )
            )
        self.current_internal = None
        self.text_internal = []


def metadata_file_from_attrs(attrs: dict[str, str | None]) -> MetadataFile | None:
    if "data-core-metadata" in attrs:
        return metadata_file_from_value(attrs.get("data-core-metadata"))
    if "data-dist-info-metadata" in attrs:
        return metadata_file_from_value(attrs.get("data-dist-info-metadata"))
    return None


def metadata_file_from_json(file_data: dict[str, Any]) -> MetadataFile | None:
    if "core-metadata" in file_data:
        return metadata_file_from_json_value(file_data["core-metadata"])
    if "dist-info-metadata" in file_data:
        return metadata_file_from_json_value(file_data["dist-info-metadata"])
    return None


def metadata_file_from_json_value(value: Any) -> MetadataFile | None:
    if isinstance(value, dict):
        return MetadataFile({str(name): str(hash_) for name, hash_ in value.items()})
    if value is True:
        return MetadataFile(None)
    return None


def metadata_file_from_value(value: str | None) -> MetadataFile | None:
    if value is None:
        return None
    if value in {"", "true"}:
        return MetadataFile(None)
    name, sep, digest = value.partition("=")
    return MetadataFile(
        {name: digest} if sep and name in SUPPORTED_RECORD_HASHES else None
    )


def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"
