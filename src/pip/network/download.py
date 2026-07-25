"""Download files with progress indicators."""

from __future__ import annotations

import email.message
import json
import logging
import mimetypes
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import BinaryIO

from pip.index.links import Link
from pip.index.paths import PathComponent
from pip.network.progress import BarType, get_download_progress_renderer
from pip.network.exceptions import (
    ConnectionFailedError,
    ConnectionTimeoutError,
    IncompleteDownloadError,
    NetworkConnectionError,
    ProxyConnectionError,
    SSLVerificationError,
)
from pip.network.http import HttpResponse, NetworkSession
from pip.network.utils import HEADERS, raise_for_status, response_chunks
from pip.core.urls import redact_auth_from_url
from pip.platform.filesystem import format_size

logger = logging.getLogger(__name__)


def splitext(path: str) -> tuple[str, str]:
    ext = ""
    base = os.path.basename(path)
    if "." in base:
        base, ext = base.rsplit(".", 1)
        ext = "." + ext
    return path[: -len(base) - len(ext)] + base, ext


def _get_http_response_size(resp: HttpResponse) -> int | None:
    try:
        size = int(resp.headers["content-length"])
    except (ValueError, KeyError, TypeError):
        return None
    # A negative length would make _FileDownload.is_incomplete() report a
    # truncated download as complete, so treat it as unknown instead.
    if size < 0:
        return None
    return size


def _get_http_response_etag_or_last_modified(resp: HttpResponse) -> str | None:
    """
    Return either the ETag or Last-Modified header (or None if neither exists).
    The return value can be used in an If-Range header.
    """
    return resp.headers.get("etag", resp.headers.get("last-modified"))


def _log_download(
    resp: HttpResponse,
    link: Link,
    progress_bar: BarType,
    total_length: int | None,
    range_start: int | None = 0,
) -> Iterable[bytes]:
    if logger.getEffectiveLevel() > logging.INFO:
        url = link.url_without_fragment
    else:
        url = link.show_url

    logged_url = redact_auth_from_url(url)

    if total_length:
        if range_start:
            logged_url = (
                f"{logged_url} ({format_size(range_start)}/{format_size(total_length)})"
            )
        else:
            logged_url = f"{logged_url} ({format_size(total_length)})"

    from_cache = getattr(resp, "from_cache", False)
    if from_cache:
        logger.info("Using cached %s", logged_url)
    elif range_start:
        logger.info("Resuming download %s", logged_url)
    else:
        logger.info("Downloading %s", logged_url)

    if logger.getEffectiveLevel() > logging.INFO:
        show_progress = False
    elif from_cache:
        show_progress = False
    elif not total_length:
        show_progress = True
    elif total_length > (512 * 1024):
        show_progress = True
    else:
        show_progress = False

    chunks = response_chunks(resp)

    if not show_progress:
        return chunks

    renderer = get_download_progress_renderer(
        bar_type=progress_bar, size=total_length, initial_progress=range_start
    )
    return renderer(chunks)


def sanitize_content_filename(filename: str) -> str:
    """
    Sanitize the "filename" value from a Content-Disposition header.
    """
    return os.path.basename(filename)


def parse_content_disposition(content_disposition: str, default_filename: str) -> str:
    """
    Parse the "filename" value from a Content-Disposition header, and
    return the default filename if the result is empty.
    """
    m = email.message.Message()
    m["content-type"] = content_disposition
    filename = m.get_param("filename")
    if filename:
        # We need to sanitize the filename to prevent directory traversal
        # in case the filename contains ".." path parts.
        filename = sanitize_content_filename(str(filename))
    return filename or default_filename


def _get_http_response_filename(resp: HttpResponse, link: Link) -> PathComponent:
    """Get an ideal filename from the given HTTP response, falling back to
    the link filename if not provided.

    The result is validated as a single path component, so it can be joined onto
    a download directory without escaping it.
    """
    filename: str = link.filename  # fallback
    # Have a look at the Content-Disposition header for a better guess
    content_disposition = resp.headers.get("content-disposition")
    if content_disposition:
        filename = parse_content_disposition(content_disposition, filename)
    ext: str | None = splitext(filename)[1]
    if not ext:
        ext = mimetypes.guess_extension(resp.headers.get("content-type", ""))
        if ext:
            filename += ext
    if not ext and link.url != resp.url:
        ext = os.path.splitext(resp.url)[1]
        if ext:
            filename += ext
    return PathComponent.from_name(filename, required=True)


@dataclass
class _FileDownload:
    """Stores the state of a single link download."""

    link: Link
    output_file: BinaryIO
    size: int | None
    bytes_received: int = 0
    reattempts: int = 0

    def is_incomplete(self) -> bool:
        return bool(self.size is not None and self.bytes_received < self.size)

    def write_chunk(self, data: bytes) -> None:
        self.bytes_received += len(data)
        self.output_file.write(data)

    def reset_file(self) -> None:
        """Delete any saved data and reset progress to zero."""
        self.output_file.seek(0)
        self.output_file.truncate()
        self.bytes_received = 0


class Downloader:
    def __init__(
        self,
        session: NetworkSession,
        progress_bar: BarType,
    ) -> None:
        self._session = session
        self._progress_bar = progress_bar
        self._resume_retries = session.resume_retries
        assert self._resume_retries >= 0, (
            "Number of max resume retries must be bigger or equal to zero"
        )

    def batch(
        self, links: Iterable[Link], location: str
    ) -> Iterable[tuple[Link, tuple[str, str]]]:
        """Convenience method to download multiple links."""
        for link in links:
            filepath, content_type = self(link, location)
            yield link, (filepath, content_type)

    def __call__(self, link: Link, location: str) -> tuple[str, str]:
        """Download a link and save it under location."""
        resp = self._http_get(link)
        download_size = _get_http_response_size(resp)

        filepath = _get_http_response_filename(resp, link).join(location)
        with open(filepath, "wb") as content_file:
            download = _FileDownload(link, content_file, download_size)
            self._process_response(download, resp)
            if download.is_incomplete():
                self._attempt_resumes_or_redownloads(download, resp)

        content_type = resp.headers.get("Content-Type", "")
        return filepath, content_type

    def _process_response(self, download: _FileDownload, resp: HttpResponse) -> None:
        """Download and save chunks from a response."""
        chunks = _log_download(
            resp,
            download.link,
            self._progress_bar,
            download.size,
            range_start=download.bytes_received,
        )
        try:
            for chunk in chunks:
                download.write_chunk(chunk)
        except OSError as e:
            # If the download size is not known, then give up downloading the file.
            if download.size is None:
                raise e

            logger.warning("Connection interrupted while downloading.")

    def _attempt_resumes_or_redownloads(
        self, download: _FileDownload, first_resp: HttpResponse
    ) -> None:
        """Attempt to resume/restart the download if connection was dropped."""

        while download.reattempts < self._resume_retries and download.is_incomplete():
            assert download.size is not None
            download.reattempts += 1
            logger.warning(
                "Attempting to resume incomplete download (%s/%s, attempt %d)",
                format_size(download.bytes_received),
                format_size(download.size),
                download.reattempts,
            )

            try:
                resume_resp = self._http_get_resume(download, should_match=first_resp)
                # Fallback: if the server responded with 200 (i.e., the file has
                # since been modified or range requests are unsupported) or any
                # other unexpected status, restart the download from the beginning.
                must_restart = resume_resp.status_code != HTTPStatus.PARTIAL_CONTENT
                if must_restart:
                    download.reset_file()
                    download.size = _get_http_response_size(resume_resp)
                    first_resp = resume_resp
                else:
                    # If the resume request starts at the wrong location, fail
                    # outright since the server is misbehaving.
                    content_range = resume_resp.headers.get("Content-Range", "")
                    resumed_at = content_range.lower().partition("bytes ")[2]
                    resumed_at = resumed_at.partition("-")[0]
                    if resumed_at and resumed_at != str(download.bytes_received):
                        raise IncompleteDownloadError(download)

                self._process_response(download, resume_resp)
            except (
                ConnectionFailedError,
                ConnectionTimeoutError,
                ProxyConnectionError,
                SSLVerificationError,
                OSError,
            ):
                # The error handling here is tricky, a few notes:
                #
                # - Diagnostic connection errors are raised by the transport's
                #   connection exception handler.
                # - Streaming errors occur after the response is returned and
                #   therefore bypass that connection-level handler.
                continue

        # No more resume attempts. Raise an error if the download is still incomplete.
        if download.is_incomplete():
            os.remove(download.output_file.name)
            raise IncompleteDownloadError(download)

        if download.reattempts > 0:
            self._cache_resumed_download(download, first_resp)

    def _cache_resumed_download(
        self, download: _FileDownload, original_response: HttpResponse
    ) -> None:
        cache = getattr(self._session, "cache", None)
        if cache is None:
            return
        key = download.link.url_without_fragment
        metadata = json.dumps(
            {
                "status": 200,
                "reason": original_response.reason,
                "url": download.link.url_without_fragment,
                "headers": dict(original_response.headers.items()),
            }
        ).encode()
        cache.set(key, metadata)
        download.output_file.flush()
        with open(download.output_file.name, "rb") as body:
            cache.set_body_from_io(key, body)

    def _http_get_resume(
        self, download: _FileDownload, should_match: HttpResponse
    ) -> HttpResponse:
        """Issue a HTTP range request to resume the download."""
        # To better understand the download resumption logic, see the mdn web docs:
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Range_requests
        headers = HEADERS.copy()
        headers["Range"] = f"bytes={download.bytes_received}-"
        # If possible, use a conditional range request to avoid corrupted
        # downloads caused by the remote file changing in-between.
        if identifier := _get_http_response_etag_or_last_modified(should_match):
            headers["If-Range"] = identifier
        return self._http_get(download.link, headers)

    def _http_get(
        self, link: Link, headers: Mapping[str, str] = HEADERS
    ) -> HttpResponse:
        target_url = link.url_without_fragment
        try:
            resp = self._session.get(target_url, headers=headers, stream=True)
            raise_for_status(resp)
        except NetworkConnectionError as e:
            assert e.response is not None
            logger.critical(
                "HTTP error %s while getting %s", e.response.status_code, link
            )
            raise
        return resp
