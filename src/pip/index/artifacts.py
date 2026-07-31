"""Local materialization and URL path helpers for distribution artifacts."""

from __future__ import annotations

import urllib.parse
import urllib.request
import hashlib
import logging
from pathlib import Path
from typing import Any

from pip.core.errors import InstallationError
from pip.core.urls import url_to_path

logger = logging.getLogger(__name__)


DOWNLOAD_DIR: Path | None = None


def vcs_scheme(url: str) -> str | None:
    from pip.index.vcs import vcs_scheme as parse_vcs_scheme

    return parse_vcs_scheme(url)


def materialize_vcs(url: str, *, prompting: bool = True) -> Path:
    from pip.index.vcs import materialize_vcs as materialize

    return materialize(url, prompting=prompting)


def download_dir_internal() -> Path:
    import atexit
    import shutil
    import tempfile

    global DOWNLOAD_DIR
    if DOWNLOAD_DIR is None:
        DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="pip-index-downloads-"))
        atexit.register(shutil.rmtree, DOWNLOAD_DIR, ignore_errors=True)
    return DOWNLOAD_DIR


class ArtifactLocator:
    """Locate local artifacts and materialize remote distribution URLs."""

    __slots__ = ("session",)

    def __init__(self, session: Any = None) -> None:
        self.session = session

    def ensure_local(
        self,
        url_or_path: str,
        *,
        is_vcs: bool | None = None,
        local_path: str | Path | None = None,
    ) -> Path:
        if is_vcs is None:
            is_vcs = vcs_scheme(url_or_path) is not None
        if is_vcs:
            prompting = True
            if self.session is not None:
                prompting = getattr(self.session.auth, "prompting", True)
            return materialize_vcs(url_or_path, prompting=prompting)
        local = (
            Path(local_path) if local_path is not None else self.local_path(url_or_path)
        )
        if local is not None:
            return local

        filename = self.filename(url_or_path)
        # Keep downloads from separate processes and distinct URLs isolated.
        # The URL digest preserves the original filename while preventing
        # same-basename artifacts from overwriting one another.
        target = (
            download_dir_internal()
            / hashlib.sha256(url_or_path.encode()).hexdigest()
            / filename
        )
        target.parent.mkdir(parents=True, exist_ok=True)
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
                    "HTTP error %s while getting %s", response.status_code, url
                )
                raise InstallationError(
                    f"{response.status_code} Client Error: {response.reason} for url: {url}"
                )
            with open(target, "wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    file.write(chunk)
        return target

    def local_path(self, link: str) -> Path | None:
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme == "file":
            return Path(url_to_path(link))
        if parsed.scheme:
            return None
        return Path(link)

    def filename(self, link: str) -> str:
        parsed = urllib.parse.urlparse(link)
        path = urllib.parse.unquote(parsed.path)
        filename = Path(path.rstrip("/")).name
        if filename not in {"", ".", ".."}:
            return filename
        fallback = parsed.netloc or path
        return Path(fallback.replace("\\", "/").rstrip("/")).name or fallback
