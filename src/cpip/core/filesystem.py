from __future__ import annotations

import errno
import os


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise


def display_path(path: str) -> str:
    if not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, os.getcwd())
    except ValueError:
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path
    return os.path.join(".", relative)
