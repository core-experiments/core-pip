"""Local materialization and URL path helpers for distribution artifacts."""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import urllib.parse
import urllib.request
from typing import Any

from cpip.core.errors import InstallationError
from cpip.core.urls import url_to_path

logger = logging.getLogger(__name__)


DOWNLOAD_DIR: str | None = None


def vcs_scheme(url: str) -> str | None:
    from cpip.index.vcs import vcs_scheme as parse_vcs_scheme

    return parse_vcs_scheme(url)


def materialize_vcs(url: str, *, prompting: bool = True) -> str:
    from cpip.index.vcs import materialize_vcs as materialize

    return materialize(url, prompting=prompting)


def download_dir_internal() -> str:
    import atexit
    import shutil
    import tempfile

    global DOWNLOAD_DIR
    if DOWNLOAD_DIR is None:
        DOWNLOAD_DIR = tempfile.mkdtemp(prefix="cpip-index-downloads-")
        atexit.register(shutil.rmtree, DOWNLOAD_DIR, ignore_errors=True)
    return DOWNLOAD_DIR


class ArtifactLocator:
    """Locate local artifacts and materialize remote distribution URLs."""

    __slots__ = ("download_cache", "local_path_cache", "session")

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.download_cache: dict[str, str] = {}
        self.local_path_cache: dict[str, str | None] = {}

    def ensure_local(
        self,
        url_or_path: str,
        *,
        is_vcs: bool | None = None,
        local_path: str | None = None,
    ) -> str:
        return self.ensure_local_text(
            url_or_path,
            is_vcs=is_vcs,
            local_path=local_path,
        )

    def ensure_local_text(
        self,
        url_or_path: str,
        *,
        is_vcs: bool | None = None,
        local_path: str | None = None,
    ) -> str:
        if is_vcs is None:
            is_vcs = vcs_scheme(url_or_path) is not None
        if is_vcs:
            prompting = True
            if self.session is not None:
                prompting = getattr(self.session.auth, "prompting", True)
            return os.fspath(materialize_vcs(url_or_path, prompting=prompting))
        local = (
            os.fspath(local_path)
            if local_path is not None
            else self.local_path(url_or_path)
        )
        if local is not None:
            return local

        filename = self.filename(url_or_path)
        # Keep downloads from separate processes and distinct URLs isolated.
        # The URL digest preserves the original filename while preventing
        # same-basename artifacts from overwriting one another.
        target = os.path.join(
            download_dir_internal(),
            hashlib.sha256(url_or_path.encode()).hexdigest(),
            filename,
        )
        cached = self.download_cache.get(url_or_path)
        if cached is not None:
            return cached
        os.makedirs(os.path.dirname(target), exist_ok=True)
        parsed = urllib.parse.urlparse(url_or_path)
        url = parsed._replace(fragment="").geturl()
        if self.session is None:
            import shutil

            response = urllib.request.urlopen(url)
            try:
                with open(target, "wb") as file:
                    shutil.copyfileobj(response, file)
            finally:
                response.close()
        else:
            response = self.session.get(url, stream=True)
            if response.status_code >= 400:
                logger.critical(
                    "HTTP error %s while getting %s",
                    response.status_code,
                    url,
                )
                raise InstallationError(
                    f"{response.status_code} Client Error: {response.reason} for url: {url}",
                )
            with open(target, "wb") as file:
                file.writelines(response.iter_content(chunk_size=1024 * 1024))
        self.download_cache[url_or_path] = target
        return target

    def local_path(self, link: str) -> str | None:
        cached = self.local_path_cache.get(link)
        if cached is not None or link in self.local_path_cache:
            return cached
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme == "file":
            path = url_to_path(link)
        elif parsed.scheme:
            path = None
        else:
            path = link
        self.local_path_cache[link] = path
        return path

    def filename(self, link: str) -> str:
        parsed = urllib.parse.urlparse(link)
        path = urllib.parse.unquote(parsed.path)
        filename = posixpath.basename(path.rstrip("/"))
        if filename not in {"", ".", ".."}:
            return filename
        fallback = parsed.netloc or path
        return posixpath.basename(fallback.replace("\\", "/").rstrip("/")) or fallback
