"""Cache Management"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from typing import Any

from cpip.core.direct_url import DirectUrl
from cpip.core.python import CURRENT_PYTHON_VERSION_DIGITS
from cpip.core.temp_dir import TempDirectory, tempdir_kinds

logger = logging.getLogger(__name__)

ORIGIN_JSON_NAME = "origin.json"
INTERPRETER_SHORT_NAMES = {
    "cpython": "cp",
    "pypy": "pp",
    "ironpython": "ip",
    "jython": "jy",
}


def interpreter_name() -> str:
    return INTERPRETER_SHORT_NAMES.get(sys.implementation.name, sys.implementation.name)


def hash_dict(d: dict[str, str]) -> str:
    """Return a stable sha224 of a dictionary."""
    s = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha224(s.encode("ascii")).hexdigest()


class WheelCache:
    """Allocate persistent or ephemeral directories for built wheels."""

    def __init__(self, cache_dir: str) -> None:
        assert not cache_dir or os.path.isabs(cache_dir)
        self.cache_dir = cache_dir or None
        self.temp_dir_internal = TempDirectory(
            kind=tempdir_kinds.EPHEM_WHEEL_CACHE,
            globally_managed=True,
        )

    def get_cache_path_parts(self, link: Any) -> list[str]:
        """Get the path components for a link's persistent cache entry."""
        key_parts = {"url": link.url_without_fragment}
        if link.hash_name is not None and link.hash is not None:
            key_parts[link.hash_name] = link.hash
        if link.subdirectory_fragment:
            key_parts["subdirectory"] = link.subdirectory_fragment
        key_parts["interpreter_name"] = interpreter_name()
        key_parts["interpreter_version"] = CURRENT_PYTHON_VERSION_DIGITS
        hashed = hash_dict(key_parts)
        return [hashed[:2], hashed[2:4], hashed[4:6], hashed[6:]]

    def get_path_for_link(self, link: Any) -> str:
        assert self.cache_dir
        return os.path.join(self.cache_dir, "wheels", *self.get_cache_path_parts(link))

    def get_ephem_path_for_link(self, link: Any) -> str:
        return os.path.join(
            self.temp_dir_internal.path,
            "wheels",
            *self.get_cache_path_parts(link),
        )

    @staticmethod
    def record_download_origin(cache_dir: str, download_info: DirectUrl) -> None:
        origin_path = os.path.join(cache_dir, ORIGIN_JSON_NAME)
        try:
            with open(origin_path, encoding="utf-8") as file:
                origin_json = file.read()
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(
                "Could not read origin file %s in cache entry (%s). Will attempt to overwrite it.",
                origin_path,
                e,
            )
        else:
            try:
                origin = DirectUrl.from_json(origin_json)
            except Exception as e:
                logger.warning(
                    "Could not read origin file %s in cache entry (%s). "
                    "Will attempt to overwrite it.",
                    origin_path,
                    e,
                )
            else:
                if origin.url != download_info.url:
                    logger.warning(
                        "Origin URL %s in cache entry %s does not match download URL "
                        "%s. This is likely a pip bug or a cache corruption issue. "
                        "Will overwrite it with the new value.",
                        origin.url,
                        cache_dir,
                        download_info.url,
                    )
        with open(origin_path, "w", encoding="utf-8") as file:
            file.write(download_info.to_json())
