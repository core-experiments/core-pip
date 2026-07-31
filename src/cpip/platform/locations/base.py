from __future__ import annotations

import functools
import os
import sys
import sysconfig

from cpip.core.appdirs import user_cache_dir
from cpip.core.errors import InstallationError


# Application Directories
USER_CACHE_DIR = user_cache_dir("cpip")


def get_major_minor_version() -> str:
    """
    Return the major-minor version of the current Python as a string, e.g.
    "3.7" or "3.10".
    """
    return "{}.{}".format(*sys.version_info)


def change_root(new_root: str, pathname: str) -> str:
    """Return 'pathname' with 'new_root' prepended.

    If 'pathname' is relative, this is equivalent to os.path.join(new_root, pathname).
    Otherwise, it requires making 'pathname' relative and then joining the
    two, which is tricky on DOS/Windows and Mac OS.

    This is borrowed from Python's standard library's distutils module.
    """
    if os.name == "posix":
        if not os.path.isabs(pathname):
            return os.path.join(new_root, pathname)
        else:
            return os.path.join(new_root, pathname[1:])

    elif os.name == "nt":
        drive, path = os.path.splitdrive(pathname)
        if path[0] == "\\":
            path = path[1:]
        return os.path.join(new_root, path)

    else:
        raise InstallationError(
            f"Unknown platform: {os.name}\n"
            "Can not change root path prefix on unknown platform."
        )


@functools.cache
def is_osx_framework() -> bool:
    return bool(sysconfig.get_config_var("PYTHONFRAMEWORK"))
