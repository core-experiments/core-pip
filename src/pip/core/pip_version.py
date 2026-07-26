"""Resolve the version of the pip distribution using public metadata."""

from __future__ import annotations

import importlib.metadata


pip_version_internal: str | None = None


def set_pip_version(version: str) -> None:
    """Set pip's version for source-runner execution."""
    global pip_version_internal
    pip_version_internal = version


def get_pip_version() -> str:
    """Return pip's version from the application context or distribution metadata."""
    if pip_version_internal is not None:
        return pip_version_internal
    try:
        return importlib.metadata.version("pip")
    except importlib.metadata.PackageNotFoundError:
        # Source checkouts are importable without an installed distribution.
        from pip import __version__

        return __version__
