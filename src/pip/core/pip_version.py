"""Resolve the version of the pip distribution using public metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import importlib.metadata

PIP_DISTRIBUTION_NAME = "core-pip"
PIP_DISTRIBUTION_NAMES = frozenset((PIP_DISTRIBUTION_NAME, "pip"))

pip_version_internal: str | None = None


def get_pip_distribution() -> importlib.metadata.Distribution:
    """Return the installed core-pip distribution, including legacy installs."""
    import importlib.metadata

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
    from pip.cli._execution_context import configure

    configure(version=version)


def get_pip_version() -> str:
    """Return pip's version from the application context or distribution metadata."""
    from pip.cli._execution_context import current_version

    context_version = current_version()
    if context_version is not None:
        return context_version
    if pip_version_internal is not None:
        return pip_version_internal
    import importlib.metadata

    try:
        return get_pip_distribution().version
    except importlib.metadata.PackageNotFoundError:
        # Source checkouts are importable without an installed distribution.
        from pip import __version__

        return __version__
