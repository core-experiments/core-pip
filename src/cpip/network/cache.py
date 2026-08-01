"""HTTP cache implementation."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, BinaryIO

from cpip.core.filesystem import ensure_dir
from cpip.platform.filesystem import (
    adjacent_tmp_file,
    copy_directory_permissions,
    replace,
)


@contextmanager
def suppressed_cache_errors() -> Generator[None, None, None]:
    """If we can't access the cache then we can just skip caching and process
    as if caching wasn't enabled.
    """
    try:
        yield
    except OSError:
        pass


class SafeFileCache:
    """A file based cache which is safe to use even when the target directory may
    not be accessible or writable.

    There is a race condition when two processes try to write and/or read the
    same entry at the same time, since each entry consists of two separate
    files. We therefore have
    additional logic that makes sure that both files to be present before
    returning an entry; this fixes the read side of the race condition.

    For the write side, we assume that the server will only ever return the
    same data for the same URL, which ought to be the case for files cpip is
    downloading.  PyPI does not have a mechanism to swap out a wheel for
    another wheel, for example.  If this assumption is not true, the
    this race will need to be fixed.
    """

    def __init__(self, directory: str) -> None:
        assert directory is not None, "Cache directory must not be None."
        super().__init__()
        self.directory = directory

    def get_cache_path(self, name: str) -> str:
        hashed = hashlib.sha224(name.encode()).hexdigest()
        parts = list(hashed[:5]) + [hashed]
        return os.path.join(self.directory, *parts)

    def get(self, key: str) -> bytes | None:
        # The cache entry is only valid if both metadata and body exist.
        metadata_path = self.get_cache_path(key)
        body_path = metadata_path + ".body"
        metadata: bytes | None = None
        with suppressed_cache_errors():
            with open(metadata_path, "rb") as file:
                contents = file.read()
            with open(body_path, "rb"):
                pass
            metadata = contents
        return metadata

    def write_to_file(self, path: str, writer_func: Callable[[BinaryIO], Any]) -> None:
        """Common file writing logic with proper permissions and atomic replacement."""
        with suppressed_cache_errors():
            ensure_dir(os.path.dirname(path))

            with adjacent_tmp_file(path) as f:
                writer_func(f)
                # Inherit the read/write permissions of the cache directory
                # to enable multi-user cache use-cases.
                copy_directory_permissions(self.directory, f)

            replace(f.name, path)

    def write_internal(self, path: str, data: bytes) -> None:
        self.write_to_file(path, lambda f: f.write(data))

    def write_from_io(self, path: str, source_file: BinaryIO) -> None:
        self.write_to_file(path, lambda f: shutil.copyfileobj(source_file, f))

    def set(
        self,
        key: str,
        value: bytes,
        expires: int | datetime | None = None,
    ) -> None:
        path = self.get_cache_path(key)
        self.write_internal(path, value)

    def delete(self, key: str) -> None:
        path = self.get_cache_path(key)
        with suppressed_cache_errors():
            os.remove(path)
        with suppressed_cache_errors():
            os.remove(path + ".body")

    def get_body(self, key: str) -> BinaryIO | None:
        # The cache entry is only valid if both metadata and body exist.
        metadata_path = self.get_cache_path(key)
        body_path = metadata_path + ".body"
        with suppressed_cache_errors():
            with open(metadata_path, "rb"):
                pass
            return open(body_path, "rb")
        return None

    def set_body(self, key: str, body: bytes) -> None:
        path = self.get_cache_path(key) + ".body"
        self.write_internal(path, body)

    def set_body_from_io(self, key: str, body_file: BinaryIO) -> None:
        """Set the body of the cache entry from a file object."""
        path = self.get_cache_path(key) + ".body"
        self.write_from_io(path, body_file)
