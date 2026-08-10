"""Lightweight copy-on-write cloning primitives.



Installation hot paths import this module without pulling in the broader

filesystem utility stack.  Platform-specific fallback modules are loaded only

when the native clone operation is unavailable.

"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
from collections.abc import Callable
from typing import cast

_FICLONE = 0x40049409

_clonefile: object | None = None

_clonefile_loaded = False


def _darwin_clone(source: str, destination: str) -> bool:
    """Clone one file or directory tree with APFS copy-on-write semantics."""

    global _clonefile, _clonefile_loaded

    if sys.platform != "darwin":
        return False

    import ctypes

    if not _clonefile_loaded:
        _clonefile_loaded = True

        try:
            function = ctypes.CDLL(None, use_errno=True).clonefile

        except (AttributeError, OSError):
            function = None

        if function is not None:
            function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)

            function.restype = ctypes.c_int

        _clonefile = function

    function = _clonefile

    if function is None:
        return False

    function = cast("Callable[[bytes, bytes, int], int]", function)

    if (
        function(
            os.fsencode(source),
            os.fsencode(destination),
            0,
        )
        == 0
    ):
        return True

    error = ctypes.get_errno()

    if error in {
        errno.ENOTSUP,
        errno.EXDEV,
        errno.EINVAL,
        getattr(errno, "ENOSYS", -1),
    }:
        return False

    raise OSError(error, os.strerror(error), destination)


def _linux_reflink(source: str, destination: str) -> bool:
    """Clone one regular file with the Linux FICLONE ioctl when available."""

    if not sys.platform.startswith("linux"):
        return False

    try:
        import fcntl

    except ImportError:
        return False

    source_fd = os.open(source, os.O_RDONLY)

    try:
        mode = stat.S_IMODE(os.fstat(source_fd).st_mode)

        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )

        try:
            fcntl.ioctl(destination_fd, _FICLONE, source_fd)

        except OSError as exc:
            os.close(destination_fd)

            destination_fd = -1

            try:
                os.unlink(destination)

            except FileNotFoundError:
                pass

            if exc.errno in {
                errno.ENOTTY,
                errno.ENOSYS,
                errno.EOPNOTSUPP,
                errno.EXDEV,
                errno.EINVAL,
            }:
                return False

            raise

        finally:
            if destination_fd >= 0:
                os.close(destination_fd)

    finally:
        os.close(source_fd)

    shutil.copystat(source, destination, follow_symlinks=False)

    return True


def clone_path(source: str, destination: str) -> None:
    """Copy a cache path using copy-on-write cloning whenever possible.



    Both paths must be absent from concurrent mutation. If ``destination`` is

    an existing directory, directory contents are merged while duplicate files

    are rejected.

    """

    source_text = os.fspath(source)

    destination_text = os.fspath(destination)

    destination_exists = os.path.lexists(destination_text)

    if not destination_exists:
        try:
            if _darwin_clone(source_text, destination_text):
                return

        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise

            destination_exists = True

    if not destination_exists:
        if os.path.isdir(source_text) and not os.path.islink(source_text):
            source_mode = stat.S_IMODE(os.stat(source_text).st_mode)

            try:
                os.mkdir(destination_text, source_mode)

            except FileExistsError:
                destination_exists = True

            if destination_exists:
                return clone_path(source_text, destination_text)

            try:
                with os.scandir(source_text) as entries:
                    for entry in entries:
                        clone_path(
                            os.path.join(source_text, entry.name),
                            os.path.join(destination_text, entry.name),
                        )

                shutil.copystat(
                    source_text,
                    destination_text,
                    follow_symlinks=False,
                )

            except BaseException:
                shutil.rmtree(destination_text, ignore_errors=True)

                raise

            return

        if os.path.islink(source_text):
            os.symlink(os.readlink(source_text), destination_text)

            return

        if not _linux_reflink(source_text, destination_text):
            shutil.copy2(source_text, destination_text, follow_symlinks=False)

        return

    if not (
        os.path.isdir(source_text)
        and not os.path.islink(source_text)
        and os.path.isdir(destination_text)
        and not os.path.islink(destination_text)
    ):
        raise FileExistsError(
            errno.EEXIST,
            "copy-on-write destination already exists",
            destination_text,
        )

    with os.scandir(source_text) as entries:
        for entry in entries:
            clone_path(
                os.path.join(source_text, entry.name),
                os.path.join(destination_text, entry.name),
            )
