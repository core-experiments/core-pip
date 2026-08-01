"""Filesystem destinations used by package installation."""

from __future__ import annotations

SCHEME_KEYS = ["platlib", "purelib", "headers", "scripts", "data"]


class Scheme:
    """Paths used for the files produced by a wheel installation."""

    __slots__ = ("data", "headers", "platlib", "purelib", "scripts")

    def __init__(
        self,
        platlib: str,
        purelib: str,
        headers: str,
        scripts: str,
        data: str,
    ) -> None:
        self.platlib = platlib
        self.purelib = purelib
        self.headers = headers
        self.scripts = scripts
        self.data = data
