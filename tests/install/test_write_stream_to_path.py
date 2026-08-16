from __future__ import annotations

import io
import random
from pathlib import Path

import pytest
from cpip.install import unpacking


class TestWriteStreamToPathPartialWrites:
    """os.write() may return fewer bytes than it was given (POSIX allows a
    short write for a regular file, not just pipes/sockets). The extraction
    loop must retry until every byte of each chunk actually lands, or a
    short write silently truncates the extracted file.
    """

    def test_short_os_write_calls_are_all_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_write = unpacking.os.write

        def short_write(fd: int, data) -> int:  # noqa: ANN001
            return original_write(fd, bytes(data)[:3])

        monkeypatch.setattr(unpacking.os, "write", short_write)

        path = tmp_path / "out.bin"

        payload = bytes(random.Random(3).randbytes(10_000))

        unpacking._write_stream_to_path(io.BytesIO(payload), str(path))

        assert path.read_bytes() == payload

    def test_short_writes_with_a_size_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_write = unpacking.os.write

        def short_write(fd: int, data) -> int:  # noqa: ANN001
            return original_write(fd, bytes(data)[:5])

        monkeypatch.setattr(unpacking.os, "write", short_write)

        path = tmp_path / "out.bin"

        payload = bytes(random.Random(4).randbytes(500))

        unpacking._write_stream_to_path(
            io.BytesIO(payload), str(path), size_hint=len(payload)
        )

        assert path.read_bytes() == payload

    def test_zero_length_write_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def zero_write(fd: int, data) -> int:  # noqa: ANN001
            return 0

        monkeypatch.setattr(unpacking.os, "write", zero_write)

        path = tmp_path / "out.bin"

        with pytest.raises(OSError, match="could not write extracted file data"):
            unpacking._write_stream_to_path(io.BytesIO(b"some data"), str(path))
