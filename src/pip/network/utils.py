"""Shared helpers for pip's standard-library network transport."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from pip.network.exceptions import NetworkConnectionError

HEADERS: dict[str, str] = {"Accept-Encoding": "identity"}
DOWNLOAD_CHUNK_SIZE = 256 * 1024


def raise_for_status(response: Any) -> None:
    if isinstance(response, NetworkConnectionError):
        raise response
    response.raise_for_status()


def response_chunks(
    response: Any, chunk_size: int = DOWNLOAD_CHUNK_SIZE
) -> Generator[bytes, None, None]:
    yield from response.iter_content(chunk_size)


def raise_connection_error(error: Exception, *, url: str, timeout: Any) -> None:
    """Retained as a transport-independent error boundary."""
    del timeout
    raise NetworkConnectionError(str(error), request=url) from error
