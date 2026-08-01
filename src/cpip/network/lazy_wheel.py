"""Lazy ZIP over HTTP"""

from __future__ import annotations

__all__ = ["HTTPRangeRequestUnsupported", "dist_from_wheel_url"]

from bisect import bisect_left, bisect_right
from collections.abc import Generator
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from types import TracebackType
from zipfile import BadZipFile, ZipFile

from cpip.build.metadata import MetadataDistribution
from cpip.network.exceptions import InvalidWheel, NetworkConnectionError
from cpip.network.http import HttpResponse, NetworkSession
from cpip.network.utils import HEADERS, raise_for_status, response_chunks

CONTENT_CHUNK_SIZE = 10 * 1024


class HTTPRangeRequestUnsupported(Exception):
    pass


def dist_from_wheel_url(
    name: str,
    url: str,
    session: NetworkSession,
) -> MetadataDistribution:
    """Return a distribution object from the given wheel URL.

    This uses HTTP range requests to only fetch the portion of the wheel
    containing metadata, just enough for the object to be constructed.
    If such requests are not supported, HTTPRangeRequestUnsupported
    is raised.
    """
    with LazyZipOverHTTP(url, session) as zf:
        try:
            with ZipFile(zf) as archive:
                return MetadataDistribution.from_wheel_archive(archive, name, zf.name)
        except BadZipFile as exc:
            raise InvalidWheel(zf.name, name) from exc


class LazyZipOverHTTP:
    """File-like object mapped to a ZIP file over HTTP.

    This uses HTTP range requests to lazily fetch the file's content,
    which is supposed to be fed to ZipFile.  If such requests are not
    supported by the server, raise HTTPRangeRequestUnsupported
    during initialization.
    """

    def __init__(
        self,
        url: str,
        session: NetworkSession,
        chunk_size: int = CONTENT_CHUNK_SIZE,
    ) -> None:
        head = session.head(url, headers=HEADERS)
        raise_for_status(head)
        assert head.status_code == 200
        self.session_internal, self.url_internal, self.chunk_size_internal = (
            session,
            url,
            chunk_size,
        )
        self.length_internal = int(head.headers["Content-Length"])
        self.file_internal = NamedTemporaryFile()
        self.truncate(self.length_internal)
        self.left_internal: list[int] = []
        self.right_internal: list[int] = []
        if "bytes" not in head.headers.get("Accept-Ranges", "none"):
            raise HTTPRangeRequestUnsupported("range request is not supported")
        self.check_zip()

    @property
    def mode(self) -> str:
        """Opening mode, which is always rb."""
        return "rb"

    @property
    def name(self) -> str:
        """Path to the underlying file."""
        return self.file_internal.name

    def seekable(self) -> bool:
        """Return whether random access is supported, which is True."""
        return True

    def close(self) -> None:
        """Close the file."""
        self.file_internal.close()

    @property
    def closed(self) -> bool:
        """Whether the file is closed."""
        return self.file_internal.closed

    def read(self, size: int = -1) -> bytes:
        """Read up to size bytes from the object and return them.

        As a convenience, if size is unspecified or -1,
        all bytes until EOF are returned.  Fewer than
        size bytes may be returned if EOF is reached.
        """
        download_size = max(size, self.chunk_size_internal)
        start, length = self.tell(), self.length_internal
        stop = length if size < 0 else min(start + download_size, length)
        start = max(0, stop - download_size)
        self.download_internal(start, stop - 1)
        return self.file_internal.read(size)

    def readable(self) -> bool:
        """Return whether the file is readable, which is True."""
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        """Change stream position and return the new absolute position.

        Seek to offset relative position indicated by whence:
        * 0: Start of stream (the default).  pos should be >= 0;
        * 1: Current position - pos may be negative;
        * 2: End of stream - pos usually negative.
        """
        return self.file_internal.seek(offset, whence)

    def tell(self) -> int:
        """Return the current position."""
        return self.file_internal.tell()

    def truncate(self, size: int | None = None) -> int:
        """Resize the stream to the given size in bytes.

        If size is unspecified resize to the current position.
        The current stream position isn't changed.

        Return the new file size.
        """
        return self.file_internal.truncate(size)

    def writable(self) -> bool:
        """Return False."""
        return False

    def __enter__(self) -> LazyZipOverHTTP:
        self.file_internal.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self.file_internal.__exit__(exc_type, exc_value, traceback)

    @contextmanager
    def stay(self) -> Generator[None, None, None]:
        """Return a context manager keeping the position.

        At the end of the block, seek back to original position.
        """
        pos = self.tell()
        try:
            yield
        finally:
            self.seek(pos)

    def check_zip(self) -> None:
        """Check and download until the file is a valid ZIP."""
        end = self.length_internal - 1
        for start in reversed(range(0, end, self.chunk_size_internal)):
            try:
                self.download_internal(start, end)
            except NetworkConnectionError as exc:
                if exc.response is not None and exc.response.status_code == 416:
                    raise InvalidWheel(self.name, "unknown") from exc
                raise
            with self.stay():
                try:
                    # For read-only ZIP files, ZipFile only needs
                    # methods read, seek, seekable and tell.
                    ZipFile(self)
                except BadZipFile:
                    pass
                else:
                    break

    def stream_response(
        self,
        start: int,
        end: int,
        base_headers: dict[str, str] = HEADERS,
    ) -> HttpResponse:
        """Return HTTP response to a range request from start to end."""
        headers = base_headers.copy()
        headers["Range"] = f"bytes={start}-{end}"
        # TODO: Get range requests to be correctly cached
        headers["Cache-Control"] = "no-cache"
        return self.session_internal.get(
            self.url_internal,
            headers=headers,
            stream=True,
        )

    def merge(
        self,
        start: int,
        end: int,
        left: int,
        right: int,
    ) -> Generator[tuple[int, int], None, None]:
        """Return a generator of intervals to be fetched.

        Args:
            start (int): Start of needed interval
            end (int): End of needed interval
            left (int): Index of first overlapping downloaded data
            right (int): Index after last overlapping downloaded data

        """
        lslice, rslice = self.left_internal[left:right], self.right_internal[left:right]
        i = start = min([start] + lslice[:1])
        end = max([end] + rslice[-1:])
        for j, k in zip(lslice, rslice):
            if j > i:
                yield i, j - 1
            i = k + 1
        if i <= end:
            yield i, end
        self.left_internal[left:right], self.right_internal[left:right] = [start], [end]

    def download_internal(self, start: int, end: int) -> None:
        """Download bytes from start to end inclusively."""
        with self.stay():
            left = bisect_left(self.right_internal, start)
            right = bisect_right(self.left_internal, end)
            for start, end in self.merge(start, end, left, right):
                response = self.stream_response(start, end)
                response.raise_for_status()
                self.seek(start)
                for chunk in response_chunks(response, self.chunk_size_internal):
                    self.file_internal.write(chunk)
