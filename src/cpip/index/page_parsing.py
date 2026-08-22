"""Parsing helpers for Simple API HTML and JSON pages."""

from __future__ import annotations

import json
import os
import urllib.parse
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
        except OSError:
            return []
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 404:
                return []
            raise
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
                try:
                    with open(json_path, encoding="utf-8") as file:
                        return IndexContent(
                            file.read(),
                            "application/vnd.pypi.simple.v1+json",
                        )
                except FileNotFoundError:
                    pass
                local_text = os.path.join(local_text, "index.html")
            with open(local_text, encoding="utf-8") as file:
                return IndexContent(file.read(), "text/html")

        headers = {
            "Accept": (
                "application/vnd.pypi.simple.v1+json, "
                "text/html;q=0.2, application/vnd.pypi.simple.v1+html;q=0.2"
            ),
        }
        if self.session is None:
            from cpip._vendor import requests

            self.session = requests.Session()
            self.artifacts.session = self.session
        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        return IndexContent(
            response.text,
            response.headers.get("Content-Type", "text/html").split(";", 1)[0],
            getattr(response, "from_cache", False),
        )

    def links_from_html(self, body: str, url: str) -> list[Link]:
        parser = link_parser_class()(url, self.link_factory)
        parser.feed(body)
        return parser.links

    def links_from_json(self, body: str, url: str) -> list[Link]:
        data = json.loads(body)
        links: list[Link] = []
        append = links.append
        link_factory = self.link_factory
        base_url = ensure_trailing_slash(url)
        for file_data in data.get("files", []):
            if not isinstance(file_data, dict):
                continue
            file_url = file_data.get("url")
            if not isinstance(file_url, str):
                continue
            filename = file_data.get("filename")
            hashes = file_data.get("hashes")
            yanked = file_data.get("yanked")
            requires_python = file_data.get("requires-python")
            upload_time = file_data.get("upload-time")
            append(
                link_factory(
                    join_index_url(base_url, file_url),
                    source_url=url,
                    text=str(filename or ""),
                    hashes=hashes if isinstance(hashes, dict) else None,
                    requires_python=requires_python
                    if isinstance(requires_python, str)
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
                        parse_iso_datetime(upload_time) if upload_time else None
                    ),
                ),
            )
        return links


_LINK_PARSER: type | None = None


def link_parser_class() -> type:
    """The HTML link parser, built on first use.

    html.parser is imported only here: a JSON Simple API response or a
    find-links directory never needs it, and importing it costs more than
    parsing a small page.
    """
    global _LINK_PARSER
    if _LINK_PARSER is not None:
        return _LINK_PARSER

    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self, page_url: str, link_factory: LinkFactory) -> None:
            super().__init__(convert_charrefs=True)
            self.page_url = page_url
            # Every link on the page resolves against this same base -- computed
            # once here instead of once per <a> tag in handle_endtag.
            self.base_url_internal = ensure_trailing_slash(page_url)
            self.link_factory = link_factory
            self.links: list[Link] = []
            self.current_internal: dict[str, str | None] | None = None
            self.text_internal: list[str] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            # HTMLParser already lowercases tag names before calling this.
            if tag != "a":
                return
            # HTMLParser already lowercases attribute names before calling this.
            self.current_internal = dict(attrs)
            self.text_internal = []

        def handle_data(self, data: str) -> None:
            if self.current_internal is not None:
                self.text_internal.append(data)

        def handle_endtag(self, tag: str) -> None:
            # HTMLParser already lowercases tag names before calling this.
            if tag != "a" or self.current_internal is None:
                return
            href = self.current_internal.get("href")
            if href:
                self.links.append(
                    self.link_factory(
                        join_index_url(self.base_url_internal, href),
                        source_url=self.page_url,
                        text="".join(self.text_internal).strip(),
                        requires_python=self.current_internal.get(
                            "data-requires-python"
                        ),
                        yanked_reason=self.current_internal.get("data-yanked"),
                        metadata_file=metadata_file_from_attrs(self.current_internal),
                    ),
                )
            self.current_internal = None
            self.text_internal = []

    _LINK_PARSER = LinkParser
    return LinkParser


def __getattr__(name: str) -> object:
    if name == "LinkParser":
        return link_parser_class()
    raise AttributeError(name)


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
        {name: digest} if sep and name in SUPPORTED_RECORD_HASHES else None,
    )


_ABSOLUTE_HTTP_PREFIXES = ("https://", "http://")


def join_index_url(base_url: str, href: str) -> str:
    """``urllib.parse.urljoin(base_url, href)`` for one link on an index page.

    Nearly every file URL a real index serves is already absolute, and for
    those ``urljoin`` still parses both URLs and rebuilds the result from the
    parts -- two ``urlparse`` calls and an ``urlunparse`` per link, more than
    a third of the cost of parsing a PyPI JSON page. For an absolute
    ``http(s)`` URL that round-trip is the identity, except in the few shapes
    where ``urlsplit`` would normalize or reject: an upper-case scheme,
    whitespace or control characters (stripped), an empty netloc (resolved
    against the base), a delimiter introducing an empty component -- a
    trailing ``?``, ``#`` or ``;``, or ``?#``, ``;?``, ``;#`` (``urlunparse``
    drops the empty part) -- or a bracketed IPv6 host (validated, and raised
    on when malformed). Those, and every relative reference, still take the
    real ``urljoin``; this only returns early when the answer is known to be
    ``href`` itself.
    """
    if (
        href.startswith(_ABSOLUTE_HTTP_PREFIXES)
        and href.isascii()
        and href.isprintable()
        and href[-1] not in ";?#"
        and "?#" not in href
        and ";?" not in href
        and ";#" not in href
        and "[" not in href
        and "]" not in href
    ):
        # A non-empty netloc is what makes ``urljoin`` ignore the base
        # entirely; its first character follows the ``//`` of the prefix.
        start = 8 if href[4] == "s" else 7
        if href[start : start + 1] not in "/?#":
            return href
    elif (
        href
        and href[0] != " "
        and href[:2] != "//"
        and ":" not in href
        and "?" not in href
        and ";" not in href
        and href.isascii()
        and href.isprintable()
    ):
        joined = _join_relative_reference(base_url, href)
        if joined is not None:
            return joined
    return urllib.parse.urljoin(base_url, href)


def _join_relative_reference(base_url: str, href: str) -> str | None:
    """``urljoin`` for a plain relative reference against a clean http(s) base.

    Mirrors of the same index page serve hrefs such as
    ``../../packages/x.whl#sha256=...``; ``urljoin`` resolves those by
    parsing both URLs into six parts, merging the paths segment by segment
    and rebuilding. This is that merge -- the same segment walk the stdlib
    performs -- on the shapes where nothing else in ``urljoin`` can apply:
    the href carries no scheme, netloc, query or params (the caller has
    checked for ``:``, ``//``, ``?`` and ``;``), and the base is an absolute
    ``http(s)`` URL with a non-empty netloc and no query, fragment or params
    of its own, so its path is exactly the text after the netloc. Returns
    ``None`` for anything else so the caller falls through to ``urljoin``.
    """
    if not (
        base_url.startswith(_ABSOLUTE_HTTP_PREFIXES)
        and base_url.isascii()
        and base_url.isprintable()
        and "?" not in base_url
        and "#" not in base_url
        and ";" not in base_url
        and "[" not in base_url
        and "]" not in base_url
    ):
        return None
    start = 8 if base_url[4] == "s" else 7
    if base_url[start : start + 1] in "/":
        return None
    path, _, fragment = href.partition("#")
    if not path:
        # An empty path means "the base's own path" (plus the fragment);
        # leave that branch, and its query inheritance, to urljoin.
        return None
    slash = base_url.find("/", start)
    if slash < 0:
        origin = base_url
        base_path = ""
    else:
        origin = base_url[:slash]
        base_path = base_url[slash:]
    if path[:1] == "/":
        segments = path.split("/")
    else:
        base_parts = base_path.split("/")
        if base_parts[-1] != "":
            del base_parts[-1]
        segments = base_parts + path.split("/")
        segments[1:-1] = filter(None, segments[1:-1])
    resolved: list[str] = []
    for segment in segments:
        if segment == "..":
            if resolved:
                resolved.pop()
        elif segment != ".":
            resolved.append(segment)
    if segments[-1] in (".", ".."):
        resolved.append("")
    joined = "/".join(resolved) or "/"
    if joined[0] != "/":
        joined = "/" + joined
    if fragment:
        return f"{origin}{joined}#{fragment}"
    return origin + joined


def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"
