"""Resolve the version of the cpip distribution using public metadata."""

from __future__ import annotations

import importlib.metadata

from cpip.core.utils import current_version

CPIP_DISTRIBUTION_NAME = "cpip"

CPIP_DISTRIBUTION_NAMES = frozenset((CPIP_DISTRIBUTION_NAME, "cpip"))


cpip_version_internal: str | None = None


def get_cpip_distribution() -> importlib.metadata.Distribution:
    """Return the installed cpip distribution, including legacy installs."""

    for name in CPIP_DISTRIBUTION_NAMES:
        try:
            return importlib.metadata.distribution(name)

        except importlib.metadata.PackageNotFoundError:
            continue

    raise importlib.metadata.PackageNotFoundError(CPIP_DISTRIBUTION_NAME)


def get_cpip_version() -> str:
    """Return cpip's version from the application context or distribution metadata."""

    context_version = current_version()

    if context_version is not None:
        return context_version

    if cpip_version_internal is not None:
        return cpip_version_internal

    try:
        return get_cpip_distribution().version

    except importlib.metadata.PackageNotFoundError:
        # Source checkouts are importable without an installed distribution.

        from cpip import __version__

        return __version__
