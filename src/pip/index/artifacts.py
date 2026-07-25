"""Local materialization and URL path helpers for distribution artifacts."""

from __future__ import annotations

import shutil
import tempfile
import urllib.parse
import urllib.request
import atexit
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pip.index.vcs import materialize_vcs, vcs_scheme
from pip.core.errors import InstallationError

logger = logging.getLogger(__name__)


_DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="pip-index-downloads-"))
atexit.register(shutil.rmtree, _DOWNLOAD_DIR, ignore_errors=True)


@dataclass(frozen=True)
class ArtifactLocator:
    """Locate local artifacts and materialize remote distribution URLs."""

    session: Any = None

    def ensure_local(self, url_or_path: str) -> Path:
        if vcs_scheme(url_or_path) is not None:
            prompting = True
            if self.session is not None:
                prompting = getattr(self.session.auth, "prompting", True)
            return materialize_vcs(url_or_path, prompting=prompting)
        local = self.local_path(url_or_path)
        if local is not None:
            return local

        filename = self.filename(url_or_path)
        # Keep downloads from separate processes and distinct URLs isolated.
        # The URL digest preserves the original filename while preventing
        # same-basename artifacts from overwriting one another.
        target = (
            _DOWNLOAD_DIR / hashlib.sha256(url_or_path.encode()).hexdigest() / filename
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        parsed = urllib.parse.urlparse(url_or_path)
        url = parsed._replace(fragment="").geturl()
        if self.session is None:
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
            return Path(urllib.request.url2pathname(parsed.path))
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
