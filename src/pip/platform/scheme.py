"""Filesystem destinations used by package installation."""

from __future__ import annotations

from dataclasses import dataclass

SCHEME_KEYS = ["platlib", "purelib", "headers", "scripts", "data"]


@dataclass(frozen=True, slots=True)
class Scheme:
    """Paths used for the files produced by a wheel installation."""

    platlib: str
    purelib: str
    headers: str
    scripts: str
    data: str
