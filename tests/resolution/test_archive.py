from __future__ import annotations

import random
import struct
import zipfile
from pathlib import Path

import pytest
from cpip.resolution.archive import WheelArchive, WheelhouseUnavailable


def _write_zip(
    path: Path,
    members: dict[str, bytes],
    *,
    compress_type: int = zipfile.ZIP_DEFLATED,
    comment: bytes = b"",
    prefix: bytes = b"",
) -> None:
    if prefix:
        path.write_bytes(prefix)

    mode = "a" if prefix else "w"

    with zipfile.ZipFile(path, mode, compress_type) as archive:
        for name, data in members.items():
            archive.writestr(name, data)

        archive.comment = comment


def _sample_members() -> dict[str, bytes]:
    return {
        "pkg/__init__.py": b"NAME = 'pkg'\n",
        "pkg-1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: other>=1\n"
        ),
        "pkg-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        "pkg-1.0.dist-info/RECORD": b"",
        "empty.txt": b"",
        "unicode-éè.txt": "café naïve".encode(),
        "binary.bin": bytes(random.Random(0).randbytes(4096)),
        "incompressible.bin": bytes(random.Random(1).randbytes(65536)),
        "repeated.txt": b"a" * 100_000,
    }


def _open(path: Path) -> WheelArchive:
    file = path.open("rb")

    try:
        return WheelArchive(file)

    except Exception:
        file.close()

        raise


def _assert_matches_real_zipfile(path: Path) -> None:
    fast = _open(path)

    try:
        with zipfile.ZipFile(path) as real:
            assert set(fast.namelist()) == set(real.namelist())

            for name in real.namelist():
                assert fast.read(name) == real.read(name)

    finally:
        fast.file.close()


class TestWheelArchiveMatchesRealZipfile:
    @pytest.mark.parametrize(
        "compress_type",
        [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
    )
    def test_plain_archive(self, tmp_path: Path, compress_type: int) -> None:
        path = tmp_path / "sample.zip"

        _write_zip(path, _sample_members(), compress_type=compress_type)

        _assert_matches_real_zipfile(path)

    def test_archive_with_zip_comment(self, tmp_path: Path) -> None:
        path = tmp_path / "commented.zip"

        _write_zip(path, _sample_members(), comment=b"a trailing archive comment")

        _assert_matches_real_zipfile(path)

    def test_single_member(self, tmp_path: Path) -> None:
        path = tmp_path / "single.zip"

        _write_zip(path, {"only.txt": b"one file"})

        _assert_matches_real_zipfile(path)

    def test_many_small_members(self, tmp_path: Path) -> None:
        path = tmp_path / "many.zip"

        members = {f"pkg/module_{i}.py": f"VALUE = {i}\n".encode() for i in range(500)}

        _write_zip(path, members)

        _assert_matches_real_zipfile(path)

    def test_large_archive_spans_outside_the_cached_tail(self, tmp_path: Path) -> None:
        """A large early member sits outside the end-of-central-directory
        scan's cached tail region, forcing read_member()/read_central_directory()
        back onto the plain seek+read fallback path for it -- while a small
        late member and the central directory itself still land inside the
        cached tail. Exercises both branches of that split in one archive.
        """
        path = tmp_path / "large.zip"

        members = {
            "pkg/large_first.bin": bytes(random.Random(2).randbytes(200_000)),
            "pkg/small_last.py": b"VALUE = 1\n",
        }

        _write_zip(path, members, compress_type=zipfile.ZIP_STORED)

        tail_threshold = 22 + 65535

        assert path.stat().st_size > tail_threshold, (
            "test setup no longer produces an archive large enough to "
            "exercise the outside-the-tail fallback path"
        )

        archive = _open(path)

        try:
            large_offset = archive.members["pkg/large_first.bin"][4]

            assert large_offset < path.stat().st_size - tail_threshold, (
                "the large member's local header is not actually outside "
                "the cached tail region"
            )

        finally:
            archive.file.close()

        _assert_matches_real_zipfile(path)

    def test_per_entry_extra_field(self, tmp_path: Path) -> None:
        path = tmp_path / "extra.zip"

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("with-extra.txt")

            # A well-formed (but non-zip64) extra field: id 0x5455
            # ("UT", Info-ZIP Unix extra) carrying a 5-byte payload.
            info.extra = b"\x55\x54\x05\x00\x01\x00\x00\x00\x00"

            archive.writestr(info, b"payload with an extra field")

        _assert_matches_real_zipfile(path)

    def test_member_name_longer_than_read_headroom(self, tmp_path: Path) -> None:
        """A member whose name exceeds read_member's combined-read headroom
        must fall back to the exact positioned read -- exercised on a
        file-resident member (an early member of an archive larger than
        the cached tail).
        """
        long_name = "pkg/" + "x" * 600 + ".txt"

        members = {
            long_name: b"long-named member contents",
            "pkg/filler.bin": bytes(random.Random(6).randbytes(200_000)),
        }

        path = tmp_path / "long-name.zip"

        _write_zip(path, members, compress_type=zipfile.ZIP_STORED)

        archive = _open(path)

        try:
            # The long-named member sits before the filler, outside the
            # tail region of this >64KB archive.
            assert archive.members[long_name][4] < path.stat().st_size - (22 + 65535)

            assert archive.read(long_name) == b"long-named member contents"

        finally:
            archive.file.close()

    def test_read_many_matches_read(self, tmp_path: Path) -> None:
        path = tmp_path / "many.zip"

        members = _sample_members()

        _write_zip(path, members)

        archive = _open(path)

        try:
            names = list(members)

            individually = [archive.read(name) for name in names]

            batched = archive.read_many(names)

            assert batched == individually

            shuffled = list(reversed(names))

            assert archive.read_many(shuffled) == [
                individually[names.index(name)] for name in shuffled
            ]

        finally:
            archive.file.close()


class TestWheelArchiveFallback:
    def test_duplicate_member_name_raises(self, tmp_path: Path) -> None:
        """members is keyed by name, so a second central-directory record
        for the same name would otherwise silently overwrite the first --
        including its independent compressed data at a different offset,
        which would then never get read or CRC-checked at all.
        """
        path = tmp_path / "duplicate.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("only.txt", "first record")

            archive.writestr("only.txt", "second record")

        with zipfile.ZipFile(path) as real:
            # Confirm the fixture actually has two distinct central
            # directory records under the same name, matching what a real
            # (if unusual) zip tool could produce -- not an artifact of
            # this test's own construction.
            assert len(real.infolist()) == 2

            assert {info.filename for info in real.infolist()} == {"only.txt"}

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_lying_directory_size_declines(self, tmp_path: Path) -> None:
        """An EOCD declaring a directory_size that extends past the end of
        the file (the field can claim up to 4 GiB) must decline before any
        attempt to materialize that much memory.
        """
        path = tmp_path / "lying-size.zip"

        _write_zip(path, {"only.txt": b"contents"})

        raw = bytearray(path.read_bytes())

        eocd = raw.rindex(b"PK\x05\x06")

        # size_cd is the little-endian L at byte 12 of the EOCD record
        # (<4s4H2LH). 0x7FFFFFF0 is huge but not the 0xFFFFFFFF zip64
        # sentinel, so it reaches the bounds check rather than the
        # sentinel check.
        raw[eocd + 12 : eocd + 16] = (0x7FFFFFF0).to_bytes(4, "little")

        path.write_bytes(bytes(raw))

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_lying_directory_size_on_large_archive_declines(
        self,
        tmp_path: Path,
    ) -> None:
        """Same lie on an archive big enough that its central directory
        lives outside the cached tail, exercising the pre-read bound on
        the file-read branch rather than the tail-slice branch.
        """
        path = tmp_path / "lying-size-large.zip"

        members = {
            "pkg/large.bin": bytes(random.Random(5).randbytes(200_000)),
            "pkg/small.py": b"VALUE = 1\n",
        }

        _write_zip(path, members, compress_type=zipfile.ZIP_STORED)

        raw = bytearray(path.read_bytes())

        eocd = raw.rindex(b"PK\x05\x06")

        raw[eocd + 12 : eocd + 16] = (0x7FFFFFF0).to_bytes(4, "little")

        path.write_bytes(bytes(raw))

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_final_record_oversized_extra_field_declines(
        self,
        tmp_path: Path,
    ) -> None:
        """A final central-directory record whose declared extra field
        extends past the directory boundary must decline rather than be
        accepted with its overrun ignored.
        """
        path = tmp_path / "overrun-extra.zip"

        _write_zip(path, {"only.txt": b"contents"})

        raw = bytearray(path.read_bytes())

        cd = raw.index(b"PK\x01\x02")

        # extra_size is the little-endian H at byte 30 of the record.
        raw[cd + 30 : cd + 32] = (0xFF00).to_bytes(2, "little")

        path.write_bytes(bytes(raw))

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_not_a_zip_file(self, tmp_path: Path) -> None:
        path = tmp_path / "notazip.txt"

        path.write_bytes(b"this is not a zip archive at all")

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.zip"

        path.write_bytes(b"")

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_zip64_forced_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zip64.zip"

        _write_zip(path, {"big.bin": b"x" * 1024}, compress_type=zipfile.ZIP_STORED)

        raw = bytearray(path.read_bytes())

        # Forcing zipfile's own writer to emit a genuine zip64 sentinel
        # needs a multi-GB member; patch the central directory record's
        # compressed/uncompressed-size fields to the 0xFFFFFFFF sentinel
        # directly instead, to exercise the same detection this guards
        # against on a real oversized archive.
        cd_offset = raw.index(zipfile.stringCentralDir)  # ty:ignore[unresolved-attribute]

        compressed_size_offset = cd_offset + 20  # _CD_COMPRESSED_SIZE

        struct.pack_into("<L", raw, compressed_size_offset, 0xFFFFFFFF)

        path.write_bytes(bytes(raw))

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_data_descriptor_still_reads_correctly(self, tmp_path: Path) -> None:
        """A general-purpose bit-3 (data descriptor) entry, forced by writing
        through a non-seekable stream. The central directory's sizes/CRC are
        authoritative regardless, so WheelArchive is not required to decline
        this -- but it must produce the same bytes either way.
        """

        class _NonSeekable:
            def __init__(self, real: object) -> None:
                self._real = real

            def write(self, data: bytes) -> int:
                return self._real.write(data)  # ty:ignore[unresolved-attribute]

            def tell(self) -> int:
                raise OSError("not seekable")

            def seekable(self) -> bool:
                return False

            def flush(self) -> None:
                self._real.flush()  # ty:ignore[unresolved-attribute]

        path = tmp_path / "streamed.zip"

        with path.open("wb") as raw:
            wrapped = _NonSeekable(raw)

            with zipfile.ZipFile(wrapped, "w", zipfile.ZIP_DEFLATED) as archive:  # ty:ignore[no-matching-overload]
                archive.writestr("streamed.txt", b"data via a non-seekable stream")

        with zipfile.ZipFile(path) as real:
            info = real.getinfo("streamed.txt")

            assert info.flag_bits & 0x8, "test setup did not force a data descriptor"

            assert real.read("streamed.txt") == b"data via a non-seekable stream"

        _assert_matches_real_zipfile(path)

    def test_encrypted_flag_declines(self, tmp_path: Path) -> None:
        path = tmp_path / "flagged.zip"

        _write_zip(path, {"only.txt": b"contents"})

        raw = bytearray(path.read_bytes())

        # Flip general-purpose bit 0 (encrypted) on the sole central
        # directory record.
        cd_offset = raw.index(zipfile.stringCentralDir)  # ty:ignore[unresolved-attribute]

        flag_offset = (
            cd_offset + 8
        )  # signature(4) + version-made-by(2) + version-needed(2)

        raw[flag_offset] |= 0x1

        path.write_bytes(bytes(raw))

        with pytest.raises(WheelhouseUnavailable):
            _open(path)

    def test_corrupted_data_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.zip"

        _write_zip(path, {"only.txt": b"a" * 5000}, compress_type=zipfile.ZIP_STORED)

        raw = bytearray(path.read_bytes())

        # The payload is stored (uncompressed) right after the local file
        # header (30 bytes) + "only.txt" (8 bytes) -- flip a byte inside it
        # to break the CRC check.
        data_offset = 30 + len(b"only.txt")

        raw[data_offset] ^= 0xFF

        path.write_bytes(bytes(raw))

        archive = _open(path)

        try:
            with pytest.raises(WheelhouseUnavailable):
                archive.read("only.txt")

        finally:
            archive.file.close()

    def test_read_missing_member_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.zip"

        _write_zip(path, {"present.txt": b"hi"})

        archive = _open(path)

        try:
            with pytest.raises(WheelhouseUnavailable):
                archive.read("absent.txt")

        finally:
            archive.file.close()
