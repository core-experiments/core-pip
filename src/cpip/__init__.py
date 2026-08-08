from __future__ import annotations

from cpip.cli.entrypoint import main as main_internal

# The build backend reads this literal to resolve a dynamic project version,
# and regenerates <package>/_version.py into the wheel it builds.  Keeping it
# a literal here also keeps a module import off every startup.
__version__ = "0.0.1"


def main(args: list[str] | None = None) -> int:
    """This is an internal API only meant for use by cpip's own console scripts.



    For additional details, see https://github.com/pypa/cpip/issues/7498.

    """

    return main_internal(args, version=None, location=__file__)
