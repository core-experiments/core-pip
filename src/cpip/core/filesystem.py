from __future__ import annotations

import errno
import os
from pathlib import Path


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise


def display_path(path: str) -> str:
    try:
        relative = Path(path).relative_to(Path.cwd())
    except ValueError:
        return path
    return os.path.join(".", relative)
