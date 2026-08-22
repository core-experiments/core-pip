"""Root package sentinel for the virtual root.

Lives in its own module so the resolver phase modules can share
the sentinel without importing from ``resolver.py`` at module load
time (which would create a cycle).
"""

from __future__ import annotations

try:
    from typing import override
except ImportError:  # pragma: no cover - Python < 3.12
    from cpip._vendor.typing_extensions import override

__all__ = [
    "ROOT",
]


class _RootPackage:
    """Singleton sentinel for the virtual root package.

    Uses object identity so it can never collide with user package names.
    """

    @override
    def __repr__(self) -> str:
        return "<root>"

    # No __hash__ override: the default identity hash is what object identity
    # means, and it runs in C. A Python-level __hash__ was a frame on every
    # dict lookup keyed by package, which is most of propagation.


ROOT = _RootPackage()
