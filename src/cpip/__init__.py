from __future__ import annotations

from ._version import __version__


def main(args: list[str] | None = None) -> int:
    """This is an internal API only meant for use by cpip's own console scripts.

    For additional details, see https://github.com/pypa/cpip/issues/7498.
    """
    from cpip.cli.entrypoint import main as main_internal

    return main_internal(args, version=__version__, location=__file__)
