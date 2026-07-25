from __future__ import annotations

__version__ = "26.2.dev0"


def main(args: list[str] | None = None) -> int:
    """This is an internal API only meant for use by pip's own console scripts.

    For additional details, see https://github.com/pypa/pip/issues/7498.
    """
    from pip.cli.main import main as _main

    return _main(args, version=__version__, location=__file__)
