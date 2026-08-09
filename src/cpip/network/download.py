"""Download files with progress indicators."""

from __future__ import annotations

import email.message
import json
import logging
import mimetypes
import os
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from typing import BinaryIO

from cpip._vendor import requests
from cpip.core.urls import redact_auth_from_url
from cpip.index.links import Link
from cpip.index.paths import PathComponent
from cpip.network.exceptions import (
    ConnectionFailedError,
    ConnectionTimeoutError,
    IncompleteDownloadError,
    NetworkConnectionError,
    ProxyConnectionError,
    SSLVerificationError,
)
from cpip.network.http import HttpResponse, NetworkSession
from cpip.network.progress import BarType, get_download_progress_renderer
from cpip.network.utils import HEADERS, raise_for_status, response_chunks
from cpip.platform.filesystem import format_size

logger = logging.getLogger(__name__)


def splitext(path: str) -> tuple[str, str]:
    ext = ""

    base = os.path.basename(path)

    if "." in base:
        base, ext = base.rsplit(".", 1)

        ext = "." + ext

    return path[: -len(base) - len(ext)] + base, ext


def get_http_response_size(resp: HttpResponse) -> int | None:
    try:
        size = int(resp.headers["content-length"])

    except (ValueError, KeyError, TypeError):
        return None

    # A negative length would make _FileDownload.is_incomplete() report a

    # truncated download as complete, so treat it as unknown instead.

    if size < 0:
        return None

    return size


def get_http_response_etag_or_last_modified(resp: HttpResponse) -> str | None:
    """Return either the ETag or Last-Modified header (or None if neither exists).

    The return value can be used in an If-Range header.

    """

    return resp.headers.get("etag", resp.headers.get("last-modified"))


def log_download(
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

    if logger.getEffectiveLevel() > logging.INFO or from_cache:
        show_progress = False

    elif not total_length or total_length > (512 * 1024):
        show_progress = True

    else:
        show_progress = False

    chunks = response_chunks(resp)

    if not show_progress:
        return chunks

    renderer = get_download_progress_renderer(
        bar_type=progress_bar,
        size=total_length,
        initial_progress=range_start,
    )

    return renderer(chunks)


def sanitize_content_filename(filename: str) -> str:
    """Sanitize the "filename" value from a Content-Disposition header."""

    return os.path.basename(filename)


def parse_content_disposition(content_disposition: str, default_filename: str) -> str:
    """Parse the "filename" value from a Content-Disposition header, and

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


def get_http_response_filename(resp: HttpResponse, link: Link) -> PathComponent:
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


class FileDownload:
    """Stores the state of a single link download."""

    __slots__ = ("bytes_received", "link", "output_file", "reattempts", "size")

    def __init__(
        self,
        link: Link,
        output_file: BinaryIO,
        size: int | None,
        bytes_received: int = 0,
        reattempts: int = 0,
    ) -> None:
        self.link = link

        self.output_file = output_file

        self.size = size

        self.bytes_received = bytes_received

        self.reattempts = reattempts

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
        self.session_internal = session

        self.progress_bar_internal = progress_bar

        self.resume_retries_internal = session.resume_retries

        assert self.resume_retries_internal >= 0, (
            "Number of max resume retries must be bigger or equal to zero"
        )

    def batch(
        self,
        links: Iterable[Link],
        location: str,
    ) -> Iterable[tuple[Link, tuple[str, str]]]:
        """Convenience method to download multiple links."""

        for link in links:
            filepath, content_type = self(link, location)

            yield link, (filepath, content_type)

    def __call__(self, link: Link, location: str) -> tuple[str, str]:
        """Download a link and save it under location."""

        resp = self.http_get(link)

        download_size = get_http_response_size(resp)

        filepath = get_http_response_filename(resp, link).join(location)

        with open(filepath, "wb") as content_file:
            download = FileDownload(link, content_file, download_size)

            self.process_response(download, resp)

            if download.is_incomplete():
                self.attempt_resumes_or_redownloads(download, resp)

        content_type = resp.headers.get("Content-Type", "")

        return filepath, content_type

    def process_response(self, download: FileDownload, resp: HttpResponse) -> None:
        """Download and save chunks from a response."""

        chunks = log_download(
            resp,
            download.link,
            self.progress_bar_internal,
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

        except self._request_error_types() as e:
            if download.size is None:
                raise e

            logger.warning("Connection interrupted while downloading.")

    @staticmethod
    def _request_error_types() -> tuple[type[BaseException], ...]:
        return (requests.exceptions.RequestException,)

    def attempt_resumes_or_redownloads(
        self,
        download: FileDownload,
        first_resp: HttpResponse,
    ) -> None:
        """Attempt to resume/restart the download if connection was dropped."""

        while (
            download.reattempts < self.resume_retries_internal
            and download.is_incomplete()
        ):
            assert download.size is not None

            download.reattempts += 1

            logger.warning(
                "Attempting to resume incomplete download (%s/%s, attempt %d)",
                format_size(download.bytes_received),
                format_size(download.size),
                download.reattempts,
            )

            try:
                resume_resp = self.http_get_resume(download, should_match=first_resp)

                # Fallback: if the server responded with 200 (i.e., the file has

                # since been modified or range requests are unsupported) or any

                # other unexpected status, restart the download from the beginning.

                must_restart = resume_resp.status_code != HTTPStatus.PARTIAL_CONTENT

                if must_restart:
                    download.reset_file()

                    download.size = get_http_response_size(resume_resp)

                    first_resp = resume_resp

                else:
                    # If the resume request starts at the wrong location, fail

                    # outright since the server is misbehaving.

                    content_range = resume_resp.headers.get("Content-Range", "")

                    resumed_at = content_range.lower().partition("bytes ")[2]

                    resumed_at = resumed_at.partition("-")[0]

                    if resumed_at and resumed_at != str(download.bytes_received):
                        raise IncompleteDownloadError(download)

                self.process_response(download, resume_resp)

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
            self.cache_resumed_download(download, first_resp)

    def cache_resumed_download(
        self,
        download: FileDownload,
        original_response: HttpResponse,
    ) -> None:
        cache = getattr(self.session_internal, "cache", None)

        if cache is None:
            return

        key = download.link.url_without_fragment

        metadata = json.dumps(
            {
                "status": 200,
                "reason": original_response.reason,
                "url": download.link.url_without_fragment,
                "headers": dict(original_response.headers.items()),
            },
        ).encode()

        cache.set(key, metadata)

        download.output_file.flush()

        with open(download.output_file.name, "rb") as body:
            cache.set_body_from_io(key, body)

    def http_get_resume(
        self,
        download: FileDownload,
        should_match: HttpResponse,
    ) -> HttpResponse:
        """Issue a HTTP range request to resume the download."""

        # To better understand the download resumption logic, see the mdn web docs:

        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Range_requests

        headers = HEADERS.copy()

        headers["Range"] = f"bytes={download.bytes_received}-"

        # If possible, use a conditional range request to avoid corrupted

        # downloads caused by the remote file changing in-between.

        if identifier := get_http_response_etag_or_last_modified(should_match):
            headers["If-Range"] = identifier

        return self.http_get(download.link, headers)

    def http_get(
        self,
        link: Link,
        headers: Mapping[str, str] = HEADERS,
    ) -> HttpResponse:
        target_url = link.url_without_fragment

        try:
            resp = self.session_internal.get(target_url, headers=headers, stream=True)

            raise_for_status(resp)

        except NetworkConnectionError as e:
            assert e.response is not None

            logger.critical(
                "HTTP error %s while getting %s",
                e.response.status_code,
                link,
            )

            raise

        return resp
