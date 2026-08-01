from __future__ import annotations

import os
import ntpath
import string
import sys
import urllib.parse

WINDOWS = sys.platform == "win32"


def path_to_url(path: str) -> str:
    path = normalize_windows_path(path)
    path = os.path.normpath(os.path.abspath(path))
    if WINDOWS:
        from urllib import request

        path = request.pathname2url(path)
    else:
        path = urllib.parse.quote(path, safe="/:")
        return "file://" + path
    return urllib.parse.urljoin("file://", path)


def url_to_path(url: str) -> str:
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

    if WINDOWS:
        from urllib import request

        path = request.url2pathname(netloc + path)
    else:
        path = urllib.parse.unquote(netloc + path)
    # ``url2pathname`` has returned both ``/C:/...`` and ``\\C:\\...``
    # for drive-qualified file URLs across Python versions.  Leaving the
    # separator in place makes ``abspath`` turn it into ``C:\\C:\\...``.
    return normalize_windows_path(path, strip_drive_separator=not bool(netloc))


def normalize_windows_path(path: str, *, strip_drive_separator: bool = False) -> str:
    if not WINDOWS:
        return path
    if (
        strip_drive_separator
        and len(path) >= 3
        and path[0] in "/\\"
        and path[1] in string.ascii_letters
        and path[2] == ":"
    ):
        path = path[1:]
    drive, tail = ntpath.splitdrive(path)
    while (
        drive
        and len(tail) >= 3
        and tail[0] in "/\\"
        and tail[1] in string.ascii_letters
        and tail[2] == ":"
    ):
        path = drive + tail[1:]
        drive, tail = ntpath.splitdrive(path)
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
    user, separator, password_internal = auth.partition(":")
    redacted = f"{urllib.parse.quote(user)}:****@" if separator else "****@"
    return urllib.parse.urlunsplit(
        (parsed.scheme, redacted + netloc, parsed.path, parsed.query, parsed.fragment)
    )
