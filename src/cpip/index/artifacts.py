"""Local materialization and URL path helpers for distribution artifacts."""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import shutil
import urllib.parse
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

    __slots__ = (
        "artifact_cache",
        "download_cache",
        "download_hashes",
        "local_path_cache",
        "session",
    )

    def __init__(
        self,
        session: Any = None,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.session = session
        self.download_cache: dict[str, str] = {}
        self.download_hashes: dict[str, dict[str, str]] = {}
        self.local_path_cache: dict[str, str | None] = {}
        if cache_dir is None:
            self.artifact_cache = None
        else:
            from cpip.index.artifact_cache import ArtifactCache

            self.artifact_cache = ArtifactCache(cache_dir)

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
        hashes: dict[str, str] | None = None,
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
        parsed = urllib.parse.urlparse(url_or_path)
        url = parsed._replace(fragment="").geturl()
        artifact_cache = self.artifact_cache
        if artifact_cache is not None:
            cached_artifact = artifact_cache.get(url, hashes)
            if cached_artifact is not None:
                from cpip.index.artifact_cache import materialize_cached_artifact

                materialize_cached_artifact(cached_artifact.path, target)
                self.download_hashes[url_or_path] = {
                    "sha256": cached_artifact.digest,
                }
                self.download_cache[url_or_path] = target
                return target
        cache = getattr(self.session, "cache", None)
        cache_key = f"artifact:{url}"
        if cache is not None:
            cached_path = getattr(cache, "get_body_path", lambda _key: None)(
                cache_key,
            )
            if cached_path is not None:
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.link(cached_path, target)
                except FileExistsError:
                    pass
                except OSError:
                    cached_path = None
                if cached_path is not None:
                    if artifact_cache is not None:
                        try:
                            cached_artifact = artifact_cache.store_path(
                                url,
                                filename,
                                cached_path,
                                hashes,
                            )
                        except OSError:
                            pass
                        else:
                            self.download_hashes[url_or_path] = {
                                "sha256": cached_artifact.digest,
                            }
                    self.download_cache[url_or_path] = target
                    return target
            cached_body = cache.get_body(cache_key)
            if cached_body is not None:
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as file:
                        shutil.copyfileobj(cached_body, file)
                finally:
                    cached_body.close()
                if artifact_cache is not None:
                    try:
                        cached_artifact = artifact_cache.store_path(
                            url,
                            filename,
                            target,
                            hashes,
                        )
                    except OSError:
                        pass
                    else:
                        self.download_hashes[url_or_path] = {
                            "sha256": cached_artifact.digest,
                        }
                self.download_cache[url_or_path] = target
                return target
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if self.session is None:
            from cpip._vendor import requests

            self.session = requests.Session()

        def request() -> Any:
            current = self.session.get(url, stream=True)
            if current.status_code < 400:
                return current
            logger.critical(
                "HTTP error %s while getting %s",
                current.status_code,
                url,
            )
            try:
                raise InstallationError(
                    f"{current.status_code} Client Error: {current.reason} "
                    f"for url: {url}",
                )
            finally:
                current.close()

        response = request()
        try:
            chunks = response.iter_content(chunk_size=1024 * 1024)
            if artifact_cache is not None:
                try:
                    cached_artifact = artifact_cache.store_chunks(
                        url,
                        filename,
                        chunks,
                        hashes,
                    )
                except OSError:
                    # A failed cache write can consume part of the streaming
                    # response. Retry without persistence rather than turning
                    # an optional cache failure into an install failure.
                    response.close()
                    response = request()
                    with open(target, "wb") as file:
                        file.writelines(
                            response.iter_content(chunk_size=1024 * 1024),
                        )
                else:
                    from cpip.index.artifact_cache import materialize_cached_artifact

                    materialize_cached_artifact(cached_artifact.path, target)
                    self.download_hashes[url_or_path] = {
                        "sha256": cached_artifact.digest,
                    }
            else:
                with open(target, "wb") as file:
                    file.writelines(chunks)
        finally:
            response.close()
        if cache is not None and artifact_cache is None:
            with open(target, "rb") as file:
                cache.set_body_from_io(cache_key, file)
            cache.set(cache_key, b"{}")
        self.download_cache[url_or_path] = target
        return target

    def hashes_for(self, url_or_path: str) -> dict[str, str] | None:
        hashes = self.download_hashes.get(url_or_path)
        return None if hashes is None else dict(hashes)

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
