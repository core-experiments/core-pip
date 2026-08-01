from email.message import Message
from io import BytesIO

import pytest
from cpip.network.exceptions import NetworkConnectionError
from cpip.network.http import HttpResponse
from cpip.network.utils import raise_for_status, response_chunks


def response(status: int = 200, body: bytes = b"downloaded") -> HttpResponse:
    headers = Message()
    headers["Content-Length"] = str(len(body))
    return HttpResponse(
        status_code=status,
        reason="Network Error" if status >= 400 else "OK",
        url="https://example.com/file",
        headers=headers,
        raw=BytesIO(body),
    )


@pytest.mark.parametrize("status", [401, 501])
def test_raise_for_status_raises_exception(status: int) -> None:
    with pytest.raises(NetworkConnectionError):
        raise_for_status(response(status))


def test_raise_for_status_accepts_success() -> None:
    raise_for_status(response())


def test_response_chunks() -> None:
    assert b"".join(response_chunks(response(body=b"abcdef"), 2)) == b"abcdef"
