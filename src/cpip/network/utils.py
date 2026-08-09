"""Shared helpers for cpip's standard-library network transport."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from cpip.network.exceptions import NetworkConnectionError

HEADERS: dict[str, str] = {"Accept-Encoding": "identity"}
DOWNLOAD_CHUNK_SIZE = 256 * 1024


def raise_for_status(response: Any) -> None:
    if isinstance(response, NetworkConnectionError):
        raise response
    response.raise_for_status()


def response_chunks(
    response: Any,
    chunk_size: int = DOWNLOAD_CHUNK_SIZE,
) -> Generator[bytes, None, None]:
    yield from response.iter_content(chunk_size)
