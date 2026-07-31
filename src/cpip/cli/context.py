"""Runtime context shared by CLI commands."""

from __future__ import annotations

import os

from cpip.platform.locations.sysconfig import get_scheme


def target_prefix() -> str | None:
    return os.environ.get("CPIP_TARGET_PREFIX")


def target_paths() -> list[str] | None:
    prefix = target_prefix()
    if prefix is None:
        return None
    scheme = get_scheme("cpip", prefix=prefix)
    return [scheme.purelib, scheme.platlib]
