"""URL identity helpers shared by resolution orchestration and providers."""

from __future__ import annotations

from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    netloc = (
        "" if parts.scheme == "file" and parts.netloc == "localhost" else parts.netloc
    )
    fragment = tuple(
        item
        for item in parse_qsl(parts.fragment, keep_blank_values=True)
        if item[0].lower() != "egg"
    )
    query = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query), urlencode(fragment))
    )


def url_name(url: str) -> str | None:
    values = parse_qs(urlsplit(url).fragment).get("egg")
    return values[0] if values else None
