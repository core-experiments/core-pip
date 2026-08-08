"""Compatibility entrypoint for callers importing ``cpip.cli.main``."""

from __future__ import annotations

from cpip.cli.entrypoint import main as run_entrypoint


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    """Delegate to :func:`cpip.cli.entrypoint.main`.

    This compatibility wrapper preserves the historical ``cpip.cli.main.main``
    entry point used by tests and external callers while routing all dispatch
    through the canonical :mod:`cpip.cli.entrypoint` implementation.
    """

    return run_entrypoint(args, version=version, location=location)
