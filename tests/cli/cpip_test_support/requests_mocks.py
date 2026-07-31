"""Small transport fakes for network tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

from cpip.network.http import HttpRequest, HttpResponse

Hook = Callable[["MockResponse"], None]


class FakeStream:
    def __init__(self, contents: bytes) -> None:
        self.io_internal = BytesIO(contents)

    def read(self, size: int = -1, **_: Any) -> bytes:
        return self.io_internal.read(size)

    def stream(self, size: int, **_: Any) -> Iterator[bytes]:
        while chunk := self.io_internal.read(size):
            yield chunk

    def release_conn(self) -> None:
        pass

    def close(self) -> None:
        self.io_internal.close()


class MockResponse(HttpResponse):
    def __init__(self, contents: bytes) -> None:
        headers = HeaderMap()
        super().__init__(
            status_code=200,
            reason="OK",
            url="",
            headers=headers,
            raw=FakeStream(contents),
            request=None,
        )


class HeaderMap(dict[str, str]):
    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())

    def get(self, key: str, default: str | None = None) -> str | None:
        return super().get(key.lower(), default)

    def update(self, other: Any = (), **kwargs: str) -> None:
        items = other.items() if hasattr(other, "items") else other
        for key, value in items:
            self[key] = value
        for key, value in kwargs.items():
            self[key] = value


class MockConnection:
    def send_internal(self, req: HttpRequest, **kwargs: Any) -> MockResponse:
        raise NotImplementedError("send_internal must be overridden for tests")

    def send(self, req: HttpRequest, **kwargs: Any) -> MockResponse:
        return self.send_internal(req, **kwargs)


class MockRequest(HttpRequest):
    def __init__(self, url: str) -> None:
        super().__init__(method="GET", url=url)
        self.hooks: dict[str, list[Hook]] = {}

    def register_hook(self, event_name: str, callback: Hook) -> None:
        self.hooks.setdefault(event_name, []).append(callback)


class BrokenStream(FakeStream):
    """A truncated stream used to exercise resume handling."""

    def stream(self, size: int, **_: Any) -> Iterator[bytes]:
        yield self.io_internal.read(size)
        raise OSError("Connection broken: IncompleteRead")
