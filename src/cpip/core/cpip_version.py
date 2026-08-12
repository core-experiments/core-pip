"""Resolve the version of the running cpip."""

from __future__ import annotations

from cpip.core.utils import current_version

CPIP_DISTRIBUTION_NAME = "cpip"

CPIP_DISTRIBUTION_NAMES = frozenset((CPIP_DISTRIBUTION_NAME, "cpip"))


def get_cpip_version() -> str:
    """Return cpip's version from the application context or ``cpip.__version__``."""

    context_version = current_version()

    if context_version is not None:
        return context_version

    from cpip import __version__

    return __version__
