"""Resolve the version of the cpip distribution using public metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import importlib.metadata

CPIP_DISTRIBUTION_NAME = "cpip"
CPIP_DISTRIBUTION_NAMES = frozenset((CPIP_DISTRIBUTION_NAME, "cpip"))

cpip_version_internal: str | None = None


def get_cpip_distribution() -> importlib.metadata.Distribution:
    """Return the installed cpip distribution, including legacy installs."""
    import importlib.metadata

    for name in CPIP_DISTRIBUTION_NAMES:
        try:
            return importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError(CPIP_DISTRIBUTION_NAME)


def set_cpip_version(version: str) -> None:
    """Set cpip's version for source-runner execution."""
    global cpip_version_internal
    cpip_version_internal = version
    from cpip.core._execution_context import configure

    configure(version=version)


def get_cpip_version() -> str:
    """Return cpip's version from the application context or distribution metadata."""
    from cpip.core._execution_context import current_version

    context_version = current_version()
    if context_version is not None:
        return context_version
    if cpip_version_internal is not None:
        return cpip_version_internal
    import importlib.metadata

    try:
        return get_cpip_distribution().version
    except importlib.metadata.PackageNotFoundError:
        # Source checkouts are importable without an installed distribution.
        from cpip import __version__

        return __version__
