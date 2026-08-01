"""Hash fragments and validation helpers for package links."""

from __future__ import annotations

import hashlib
import urllib.parse

SUPPORTED_HASHES = ("sha512", "sha384", "sha256", "sha224", "sha1", "md5")
SUPPORTED_RECORD_HASHES = frozenset(hashlib.algorithms_guaranteed)


def hashes_from_url(url: str) -> dict[str, str]:
    result: dict[str, str] = {}
    parsed = urllib.parse.urlparse(url)
    for key, values in urllib.parse.parse_qs(
        parsed.fragment,
        keep_blank_values=True,
    ).items():
        if key in SUPPORTED_RECORD_HASHES and values:
            result[key] = values[0]
    return result


def supported_hashes(hashes: dict[str, str] | None) -> dict[str, str] | None:
    if hashes is None:
        return None
    result = {name: value for name, value in hashes.items() if name in SUPPORTED_HASHES}
    return result or None
