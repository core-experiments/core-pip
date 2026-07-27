"""Resolve the version of the pip distribution using public metadata."""

from __future__ import annotations

import importlib.metadata


PIP_DISTRIBUTION_NAME = "core-pip"
PIP_DISTRIBUTION_NAMES = frozenset((PIP_DISTRIBUTION_NAME, "pip"))

pip_version_internal: str | None = None


def get_pip_distribution() -> importlib.metadata.Distribution:
    """Return the installed core-pip distribution, including legacy installs."""
    for name in PIP_DISTRIBUTION_NAMES:
        try:
            return importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError(PIP_DISTRIBUTION_NAME)


def set_pip_version(version: str) -> None:
    """Set pip's version for source-runner execution."""
    global pip_version_internal
    pip_version_internal = version


def get_pip_version() -> str:
    """Return pip's version from the application context or distribution metadata."""
    if pip_version_internal is not None:
        return pip_version_internal
    try:
        return get_pip_distribution().version
    except importlib.metadata.PackageNotFoundError:
        # Source checkouts are importable without an installed distribution.
        from pip import __version__

        return __version__
