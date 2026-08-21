from __future__ import annotations

import io
import os
import random
import stat
import tarfile
from pathlib import Path
from typing import BinaryIO

import pytest
from cpip.install import tar_reader


def _write_tar(
    path: Path,
    members: dict[str, bytes],
    *,
    compress: bool = True,
    format: int = tarfile.GNU_FORMAT,  # noqa: A002
    executable: frozenset[str] = frozenset(),
    mtime: int = 1_700_000_000,
) -> None:
    mode = "w:gz" if compress else "w"

    with tarfile.open(path, mode, format=format) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)

            info.size = len(data)

            info.mtime = mtime

            info.mode = 0o755 if name in executable else 0o644

            archive.addfile(info, io.BytesIO(data))


def _write_raw_member(
    fp: BinaryIO,
    name: str,
    data: bytes,
    *,
    typeflag: bytes = tarfile.REGTYPE,
    mtime: int = 1_700_000_000,
) -> None:
    """Write one member's header (with a correct checksum) plus its padded
    data, bypassing tarfile's own writer -- for headers real tarfile would
    never produce (e.g. a directory that also claims a data payload), which
    still need to be byte-valid enough to reach the code under test.
    """
    info = tarfile.TarInfo(name=name)

    info.size = len(data)

    info.mtime = mtime

    info.mode = 0o644

    header = bytearray(info.tobuf(tarfile.GNU_FORMAT))

    header[156:157] = typeflag

    unsigned_chksum, _signed = tarfile.calc_chksums(bytes(header))  # ty:ignore[unresolved-attribute]

    header[148:156] = ("%06o\x00 " % unsigned_chksum).encode()

    fp.write(bytes(header))

    fp.write(data)

    padding = (-len(data)) % tar_reader.BLOCKSIZE

    if padding:
        fp.write(b"\x00" * padding)


def _write_eof_marker(fp: BinaryIO) -> None:
    fp.write(b"\x00" * tar_reader.BLOCKSIZE * 2)


def _sample_members() -> dict[str, bytes]:
    return {
        "pkg-1.0/PKG-INFO": b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        "pkg-1.0/pyproject.toml": b'[project]\nname = "pkg"\nversion = "1.0"\n',
        "pkg-1.0/pkg/__init__.py": b"NAME = 'pkg'\n",
        "pkg-1.0/pkg/nested/deep.py": b"VALUE = 1\n",
        "pkg-1.0/empty.txt": b"",
        "pkg-1.0/unicode-éè.txt": "café naïve".encode(),
        "pkg-1.0/binary.bin": bytes(random.Random(0).randbytes(4096)),
    }


def _mode(path: str) -> str:
    return "r:gz" if path.endswith(".gz") else "r"


def _extract_via_tarfile(archive_path: Path, dest: Path) -> None:
    with tarfile.open(archive_path) as archive:
        data_filter = getattr(tarfile, "data_filter", None)

        if data_filter is not None:
            archive.extractall(dest, filter=data_filter)

        else:
            archive.extractall(dest)


def _tree(root: Path) -> dict[str, bytes | None]:
    result: dict[str, bytes | None] = {}

    for dirpath, _dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)

        if rel_dir != ".":
            result.setdefault(rel_dir.replace(os.sep, "/"), None)

        for filename in filenames:
            full = os.path.join(dirpath, filename)

            rel = os.path.relpath(full, root).replace(os.sep, "/")

            with open(full, "rb") as fp:
                result[rel] = fp.read()

    return result


def _assert_matches_real_tarfile(
    tmp_path: Path,
    archive_path: Path,
    *,
    check_executable: bool = True,
) -> None:
    fast_dest = tmp_path / "fast"

    fast_dest.mkdir()

    names = tar_reader.fast_untar(
        str(archive_path), str(fast_dest), _mode(str(archive_path))
    )

    assert names is not None, "expected the fast path to accept this archive"

    real_dest = tmp_path / "real"

    real_dest.mkdir()

    _extract_via_tarfile(archive_path, real_dest)

    assert _tree(fast_dest) == _tree(real_dest)

    if check_executable:
        for rel_path in _tree(real_dest):
            real_mode = os.stat(real_dest / rel_path).st_mode

            if not stat.S_ISREG(real_mode) or not (real_mode & 0o111):
                continue

            fast_mode = os.stat(fast_dest / rel_path).st_mode

            assert fast_mode & 0o111, f"{rel_path} should be executable"


class TestFastUntarMatchesRealTarfile:
    @pytest.mark.parametrize("compress", [True, False])
    def test_plain_archive(self, tmp_path: Path, compress: bool) -> None:
        archive_path = tmp_path / ("sample.tar.gz" if compress else "sample.tar")

        _write_tar(archive_path, _sample_members(), compress=compress)

        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_ustar_format(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "ustar.tar.gz"

        _write_tar(archive_path, _sample_members(), format=tarfile.USTAR_FORMAT)

        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_executable_files(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "exec.tar.gz"

        members = {"pkg-1.0/run.sh": b"#!/bin/sh\necho hi\n"}

        _write_tar(archive_path, members, executable=frozenset({"pkg-1.0/run.sh"}))

        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_ustar_prefix_long_name(self, tmp_path: Path) -> None:
        """A name that needs the ustar prefix field (>100 chars total, but
        still fits prefix(155) + '/' + name(100)) -- not a GNU longname
        extension, just the standard split-field mechanism.
        """
        archive_path = tmp_path / "prefix.tar.gz"

        long_dir = "a" * 90

        members = {f"{long_dir}/{'b' * 50}/file.txt": b"content"}

        _write_tar(archive_path, members, format=tarfile.USTAR_FORMAT)

        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_many_small_members(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "many.tar.gz"

        members = {f"pkg/module_{i}.py": f"VALUE = {i}\n".encode() for i in range(500)}

        _write_tar(archive_path, members)

        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_large_member_spans_multiple_reads(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "large.tar"

        members = {
            "pkg/large.bin": bytes(random.Random(1).randbytes(300_000)),
            "pkg/small.txt": b"after the large member",
        }

        _write_tar(archive_path, members, compress=False)

        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_explicit_directory_entries(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "dirs.tar.gz"

        with tarfile.open(archive_path, "w:gz") as archive:
            for name in ("pkg", "pkg/sub", "pkg/sub/deep"):
                info = tarfile.TarInfo(name=name)

                info.type = tarfile.DIRTYPE

                info.mode = 0o755

                archive.addfile(info)

            info = tarfile.TarInfo(name="pkg/sub/deep/file.txt")

            data = b"leaf"

            info.size = len(data)

            archive.addfile(info, io.BytesIO(data))

        _assert_matches_real_tarfile(tmp_path, archive_path)


class TestFastUntarDeclines:
    def test_unsupported_compression(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "sample.tar.bz2"

        with tarfile.open(archive_path, "w:bz2") as archive:
            info = tarfile.TarInfo(name="only.txt")

            data = b"hi"

            info.size = len(data)

            archive.addfile(info, io.BytesIO(data))

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r:bz2") is None

        assert list(dest.iterdir()) == []

    def test_gnu_longname_extension_declines_and_cleans_up(
        self,
        tmp_path: Path,
    ) -> None:
        archive_path = tmp_path / "longname.tar.gz"

        # Long enough that GNU format must use a longname (@LongLink)
        # extension header rather than the plain prefix mechanism.
        very_long_name = "pkg/" + "x" * 40 + "/" + "y" * 40 + "/" + "z" * 40 + "/f.txt"

        members = {
            "pkg/first.txt": b"extracted before the long-name entry",
            very_long_name: b"needs a GNU longname extension",
        }

        _write_tar(archive_path, members, format=tarfile.GNU_FORMAT)

        dest = tmp_path / "dest"

        dest.mkdir()

        ok = tar_reader.fast_untar(str(archive_path), str(dest), "r:gz")

        assert ok is None

        assert list(dest.iterdir()) == [], (
            "the fast path must undo files it already wrote before bailing"
        )

    def test_pax_format_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "pax.tar.gz"

        _write_tar(archive_path, _sample_members(), format=tarfile.PAX_FORMAT)

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r:gz") is None

        assert list(dest.iterdir()) == []

    def test_symlink_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "symlink.tar.gz"

        with tarfile.open(archive_path, "w:gz") as archive:
            regular = tarfile.TarInfo(name="pkg/first.txt")

            data = b"first"

            regular.size = len(data)

            archive.addfile(regular, io.BytesIO(data))

            link = tarfile.TarInfo(name="pkg/link.txt")

            link.type = tarfile.SYMTYPE

            link.linkname = "first.txt"

            archive.addfile(link)

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r:gz") is None

        assert list(dest.iterdir()) == []

    def test_corrupted_checksum_declines_and_cleans_up(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "corrupt.tar.gz"

        members = {
            "pkg/first.txt": b"extracted before corruption is discovered",
            "pkg/second.txt": b"this header gets corrupted",
        }

        _write_tar(archive_path, members, compress=False)

        raw = bytearray(archive_path.read_bytes())

        # The second header starts one block (the first header) plus the
        # first member's data, padded up to the next 512-byte boundary --
        # flip a byte in the second header's name field to break its
        # checksum.
        first_size = len(b"extracted before corruption is discovered")

        padded_first_size = -(-first_size // 512) * 512

        second_header_offset = 512 + padded_first_size

        raw[second_header_offset] ^= 0xFF

        archive_path.write_bytes(bytes(raw))

        dest = tmp_path / "dest"

        dest.mkdir()

        ok = tar_reader.fast_untar(str(archive_path), str(dest), "r")

        assert ok is None

        assert list(dest.iterdir()) == []

    def test_truncated_archive_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "truncated.tar.gz"

        members = {"pkg/first.txt": b"data"}

        _write_tar(archive_path, members, compress=False)

        raw = archive_path.read_bytes()

        # Cut off well before any end-of-archive marker.
        archive_path.write_bytes(raw[:600])

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r") is None

        assert list(dest.iterdir()) == []

    def test_directory_member_with_data_declines_and_cleans_up(
        self,
        tmp_path: Path,
    ) -> None:
        """A directory header claiming a nonzero size is malformed -- real
        tar writers never produce one -- but the extraction loop doesn't
        consume a payload for _DIRTYPE, so trusting it would desync the
        stream and parse the "directory's data" as the next header.
        """
        archive_path = tmp_path / "dirdata.tar"

        with archive_path.open("wb") as fp:
            _write_raw_member(fp, "pkg/first.txt", b"extracted first")

            _write_raw_member(
                fp,
                "pkg/weird",
                b"not really directory data",
                typeflag=tarfile.DIRTYPE,
            )

            _write_eof_marker(fp)

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r") is None

        assert list(dest.iterdir()) == []

    def test_backslash_in_name_declines(self, tmp_path: Path) -> None:
        """split_leading_dir() (unpacking.py) treats '\\' as a directory
        separator, but a POSIX filesystem treats it as a literal filename
        character. Writing such a member verbatim (as the fast path
        otherwise would) can leave an archive whose members share a
        backslash-separated leading directory un-flattened, since the
        directory unpacking.py looks for after extraction was never
        actually created -- so the fast path declines instead, leaving
        this case to the tarfile path's name-based (pre-write) stripping.
        """
        archive_path = tmp_path / "backslash.tar.gz"

        _write_tar(archive_path, {"pkg\\file.txt": b"data"})

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r:gz") is None

        assert list(dest.iterdir()) == []

    def test_sparse_member_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "sparse.tar.gz"

        with tarfile.open(archive_path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
            info = tarfile.TarInfo(name="pkg/sparse.bin")

            info.type = tarfile.GNUTYPE_SPARSE

            info.size = 0

            archive.addfile(info)

        dest = tmp_path / "dest"

        dest.mkdir()

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r:gz") is None

        assert list(dest.iterdir()) == []

    def test_nonempty_destination_declines_without_touching_it(
        self,
        tmp_path: Path,
    ) -> None:
        archive_path = tmp_path / "sample.tar.gz"

        _write_tar(archive_path, {"pkg/first.txt": b"data"})

        dest = tmp_path / "dest"

        dest.mkdir()

        preexisting = dest / "already-here.txt"

        preexisting.write_bytes(b"do not touch")

        assert tar_reader.fast_untar(str(archive_path), str(dest), "r:gz") is None

        assert preexisting.read_bytes() == b"do not touch"


class TestFastUntarEmptyArchive:
    def test_empty_archive_returns_empty_list_not_none(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "empty.tar.gz"

        with tarfile.open(archive_path, "w:gz"):
            pass  # no members added

        dest = tmp_path / "dest"

        dest.mkdir()

        names = tar_reader.fast_untar(str(archive_path), str(dest), "r:gz")

        assert names == []

        assert list(dest.iterdir()) == []

    def test_untar_file_empty_archive_does_not_raise(self, tmp_path: Path) -> None:
        """End-to-end regression test for the untar_file()-level bug:
        has_leading_dir([]) returns True, so naively indexing
        extracted_names[0] after a successful-but-empty fast_untar() raised
        IndexError instead of the (correct) no-op.
        """
        from cpip.install.unpacking import untar_file

        archive_path = tmp_path / "empty.tar.gz"

        with tarfile.open(archive_path, "w:gz"):
            pass

        dest = tmp_path / "dest"

        untar_file(str(archive_path), str(dest))

        assert list(Path(dest).iterdir()) == []


class TestExtractExactPartialWrites:
    def test_short_os_write_calls_are_all_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A conforming-but-unhelpful os.write() that only ever accepts 3
        bytes per call must not silently truncate the extracted file.
        """
        original_write = os.write

        def short_write(fd: int, data) -> int:  # noqa: ANN001
            return original_write(fd, bytes(data)[:3])

        monkeypatch.setattr(tar_reader.os, "write", short_write)

        path = tmp_path / "out.bin"

        payload = bytes(random.Random(2).randbytes(10_000))

        tar_reader._extract_exact(io.BytesIO(payload), str(path), len(payload))

        assert path.read_bytes() == payload

    def test_zero_length_write_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def zero_write(fd: int, data) -> int:  # noqa: ANN001
            return 0

        monkeypatch.setattr(tar_reader.os, "write", zero_write)

        path = tmp_path / "out.bin"

        with pytest.raises(OSError, match="could not write extracted member data"):
            tar_reader._extract_exact(io.BytesIO(b"some data"), str(path), 9)


class TestChecksumHelpers:
    def test_checksum_matches_tarfile_calc_chksums(self) -> None:
        buf = bytearray(512)

        buf[0:100] = b"name.txt".ljust(100, b"\x00")

        buf[100:108] = b"0000644\x00"

        buf[124:136] = b"00000000004\x00"

        buf[136:148] = b"00000000000\x00"

        buf[156:157] = b"0"

        # A byte with the high bit set makes the unsigned and signed sums
        # diverge -- put one in a field _checksum_ok sums over.
        buf[5] = 0xFF

        unsigned_ref, signed_ref = tarfile.calc_chksums(  # ty:ignore[unresolved-attribute]
            bytes(buf),
        )

        assert unsigned_ref != signed_ref

        assert tar_reader._checksum_ok(bytes(buf), unsigned_ref)

        assert tar_reader._checksum_ok(bytes(buf), signed_ref)

        assert not tar_reader._checksum_ok(bytes(buf), unsigned_ref + 1)

    def test_gnu_binary_size_encoding(self) -> None:
        # A GNU binary-encoded field (high bit set on the first byte) for a
        # value too large for a 12-byte octal field.
        field = (0o200).to_bytes(1, "big") + (12_345_678_901).to_bytes(11, "big")

        assert tar_reader._nti(field) == 12_345_678_901


def _octal_fields(rng: random.Random, count: int) -> list[bytes]:
    fields: list[bytes] = []
    for _ in range(count):
        width = rng.choice((8, 12))
        kind = rng.random()
        if kind < 0.55:
            value = rng.randrange(0, 8 ** (width - 2))
            digits = ("%o" % value).encode()
            pad = rng.choice((b"\x00", b" ", b"\x00 ", b" \x00", b""))
            lead = rng.choice((b"", b" ", b"  ", b"0" * (width - len(digits) - 2)))
            field = (lead + digits + pad)[:width]
            field = field.ljust(width, rng.choice((b"\x00", b" ")))
        elif kind < 0.65:
            field = rng.choice((b"\x00", b" ")) * width
        elif kind < 0.75:
            field = bytes([rng.choice((0o200, 0o377))]) + rng.randbytes(width - 1)
        elif kind < 0.85:
            field = b"644\x00" + rng.randbytes(width - 4)
        else:
            field = rng.randbytes(width)
        fields.append(field)
    return fields


def test_nti_matches_tarfile_nti() -> None:
    rng = random.Random(2026_08_20)
    for field in _octal_fields(rng, 5000):
        try:
            expected: object = tarfile.nti(field)  # ty:ignore[unresolved-attribute]
        except tarfile.HeaderError:
            expected = "error"
        try:
            actual: object = tar_reader._nti(field)
        except tar_reader._NotFastCompatible:
            actual = "error"
        assert actual == expected, field


def test_nts_matches_tarfile_nts() -> None:
    rng = random.Random(7)
    samples = [
        b"pkg/file.txt\x00\x00\x00",
        b"\x00garbage",
        b"no-terminator",
        "café".encode() + b"\x00",
        b"\xff\xfe broken utf8\x00",
        b"",
    ]
    samples.extend(rng.randbytes(rng.randrange(0, 40)) for _ in range(2000))
    for field in samples:
        expected = tarfile.nts(field, "utf-8", "surrogateescape")  # ty:ignore[unresolved-attribute]
        assert tar_reader._nts(field) == expected, field


def test_checksum_ok_matches_tarfile_calc_chksums() -> None:
    rng = random.Random(11)
    for _ in range(2000):
        buf = rng.randbytes(tar_reader.BLOCKSIZE)
        unsigned, signed = tarfile.calc_chksums(buf)  # ty:ignore[unresolved-attribute]
        assert tar_reader._checksum_ok(buf, unsigned)
        assert tar_reader._checksum_ok(buf, signed)
        other = rng.randrange(0, 1 << 20)
        assert tar_reader._checksum_ok(buf, other) == (other in (unsigned, signed))


def test_parse_header_matches_tarfile_frombuf() -> None:
    rng = random.Random(3)
    names = [
        "pkg-1.0/PKG-INFO",
        "pkg-1.0/pkg/nested/deep.py",
        "a" * 99,
        "d" * 60 + "/" + "f" * 90,
        "pkg-1.0/unicode-éè.txt",
        "pkg-1.0/dir/",
    ]
    for _ in range(300):
        depth = rng.randrange(1, 6)
        names.append(
            "/".join(
                "".join(rng.choice("abcdefgh-_.") for _ in range(rng.randrange(1, 30)))
                for _ in range(depth)
            ),
        )
    for name in names:
        info = tarfile.TarInfo(name=name)
        info.size = rng.randrange(0, 1 << 20)
        info.mtime = rng.randrange(0, 1 << 31)
        info.mode = rng.choice((0o644, 0o755, 0o600, 0o777))
        if name.endswith("/"):
            info.type = tarfile.DIRTYPE
            info.size = 0
        buf = info.tobuf(tarfile.USTAR_FORMAT, "utf-8", "surrogateescape")
        assert len(buf) == tar_reader.BLOCKSIZE
        parsed = tar_reader._parse_header(buf)
        assert parsed is not None
        expected = tarfile.TarInfo.frombuf(buf, "utf-8", "surrogateescape")
        parsed_name, typeflag, mode, size, mtime = parsed
        # frombuf already strips a directory's trailing slash, as _parse_header does.
        assert parsed_name == expected.name
        assert typeflag == expected.type
        assert (mode, size, mtime) == (expected.mode, expected.size, expected.mtime)


class TestDirectoryCache:
    def test_odd_names_match_real_tarfile(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "odd.tar.gz"
        members = {
            "pkg-1.0/a/one.txt": b"1",
            "pkg-1.0/b/two.txt": b"2",
            "pkg-1.0/a/three.txt": b"3",
            "pkg-1.0/a//four.txt": b"4",
            "pkg-1.0/./five.txt": b"5",
            "pkg-1.0/a/b/c/d/six.txt": b"6",
            "pkg-1.0/a/b/c/seven.txt": b"7",
            "pkg-1.0/a/...": b"dots",
            "pkg-1.0/a/.hidden": b"h",
            "pkg-1.0/eight.txt": b"8",
        }
        _write_tar(archive_path, members, format=tarfile.USTAR_FORMAT)
        _assert_matches_real_tarfile(tmp_path, archive_path)

    def test_directory_level_escape_is_rejected(self, tmp_path: Path) -> None:
        from cpip.core.errors import InstallationError

        archive_path = tmp_path / "escape.tar"
        with archive_path.open("wb") as fp:
            _write_raw_member(fp, "pkg-1.0/ok.txt", b"ok")
            _write_raw_member(fp, "pkg-1.0/sub/../../../escaped.txt", b"no")
            _write_eof_marker(fp)
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(
            InstallationError, match=r"escaped\.txt.*outside the destination"
        ):
            tar_reader.fast_untar(str(archive_path), str(dest), "r")
        assert not (tmp_path / "escaped.txt").exists()

    def test_trailing_dot_segments_take_the_per_member_path(
        self, tmp_path: Path
    ) -> None:
        archive_path = tmp_path / "dots.tar"
        with archive_path.open("wb") as fp:
            _write_raw_member(fp, "pkg-1.0/a/file.txt", b"a")
            _write_raw_member(fp, "pkg-1.0/a/.", b"", typeflag=tarfile.DIRTYPE)
            _write_raw_member(fp, "pkg-1.0/a/..", b"", typeflag=tarfile.DIRTYPE)
            _write_eof_marker(fp)
        dest = tmp_path / "dest"
        dest.mkdir()
        names = tar_reader.fast_untar(str(archive_path), str(dest), "r")
        assert names == ["pkg-1.0/a/file.txt", "pkg-1.0/a/.", "pkg-1.0/a/.."]
        assert (dest / "pkg-1.0" / "a" / "file.txt").read_bytes() == b"a"

    @pytest.mark.parametrize("name", ["/foo", "/a/b", "//foo"])
    def test_absolute_member_names_are_rejected(
        self, tmp_path: Path, name: str
    ) -> None:
        from cpip.core.errors import InstallationError

        archive_path = tmp_path / "absolute.tar"
        with archive_path.open("wb") as fp:
            _write_raw_member(fp, "pkg-1.0/ok.txt", b"ok")
            _write_raw_member(fp, name, b"no")
            _write_eof_marker(fp)
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(InstallationError, match="outside the destination"):
            tar_reader.fast_untar(str(archive_path), str(dest), "r")
        assert not (dest / "foo").exists()
        assert not (dest / "a").exists()
