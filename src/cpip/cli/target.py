"""Where a ``--target`` install writes.

Split out of ``cli.common`` so that ``platform.locations.sysconfig`` -- which
loads the interpreter's ``_sysconfigdata`` module -- is paid for only by the
commands that resolve target paths.
"""

from __future__ import annotations

import os


def target_prefix() -> str | None:
    return os.environ.get("CPIP_TARGET_PREFIX")


def target_paths() -> list[str] | None:
    prefix = target_prefix()
    if prefix is None:
        return None
    from cpip.platform.locations.sysconfig import get_scheme

    scheme = get_scheme("cpip", prefix=prefix)
    return [scheme.purelib, scheme.platlib]
