"""Resolve the version of the pip distribution using public metadata."""

from __future__ import annotations

import importlib.metadata


_pip_version: str | None = None


def set_pip_version(version: str) -> None:
    """Set pip's version for source-runner execution."""
    global _pip_version
    _pip_version = version


def get_pip_version() -> str:
    """Return pip's version from the application context or distribution metadata."""
    if _pip_version is not None:
        return _pip_version
    try:
        return importlib.metadata.version("pip")
    except importlib.metadata.PackageNotFoundError:
        # Source checkouts are importable without an installed distribution.
        from pip import __version__

        return __version__
