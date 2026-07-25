from __future__ import annotations

import os
import string
import sys
import urllib.parse

WINDOWS = sys.platform == "win32"


def path_to_url(path: str) -> str:
    path = os.path.normpath(os.path.abspath(path))
    import urllib.request

    return urllib.parse.urljoin("file://", urllib.request.pathname2url(path))


def url_to_path(url: str) -> str:
    import urllib.request

    assert url.startswith("file:"), (
        f"You can only turn file: urls into filenames (not {url!r})"
    )

    _, netloc, path, _, _ = urllib.parse.urlsplit(url)
    if not netloc or netloc == "localhost":
        netloc = ""
    elif WINDOWS:
        netloc = "\\\\" + netloc
    else:
        raise ValueError(
            f"non-local file URIs are not supported on this platform: {url!r}"
        )

    path = urllib.request.url2pathname(netloc + path)
    if (
        WINDOWS
        and not netloc
        and len(path) >= 3
        and path[0] == "/"
        and path[1] in string.ascii_letters
        and path[2:4] in (":", ":/")
    ):
        path = path[1:]
    return path


def split_auth_from_netloc(
    netloc: str,
) -> tuple[str, tuple[str | None, str | None]]:
    if "@" not in netloc:
        return netloc, (None, None)
    auth, netloc = netloc.rsplit("@", 1)
    user, separator, password = auth.partition(":")
    return netloc, (
        urllib.parse.unquote(user),
        urllib.parse.unquote(password) if separator else None,
    )


def split_auth_netloc_from_url(
    url: str,
) -> tuple[str, str, tuple[str | None, str | None]]:
    parsed = urllib.parse.urlsplit(url)
    netloc, credentials = split_auth_from_netloc(parsed.netloc)
    if netloc == parsed.netloc:
        return url, netloc, credentials
    clean = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return clean, netloc, credentials


def remove_auth_from_url(url: str) -> str:
    return split_auth_netloc_from_url(url)[0]


def redact_auth_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if "@" not in parsed.netloc:
        return url
    auth, netloc = parsed.netloc.rsplit("@", 1)
    user, separator, _password = auth.partition(":")
    redacted = f"{urllib.parse.quote(user)}:****@" if separator else "****@"
    return urllib.parse.urlunsplit(
        (parsed.scheme, redacted + netloc, parsed.path, parsed.query, parsed.fragment)
    )
