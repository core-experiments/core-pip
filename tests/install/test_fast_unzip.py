from __future__ import annotations

import os
import random
import stat
import zipfile
from pathlib import Path

import pytest
from cpip.core.errors import InstallationError
from cpip.install import unpacking


def _write_zip(
    path: Path,
    members: dict[str, bytes],
    *,
    compress_type: int = zipfile.ZIP_DEFLATED,
    executable: frozenset[str] = frozenset(),
) -> None:
    with zipfile.ZipFile(path, "w", compress_type) as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)

            # A per-member ZipInfo's own compress_type (default ZIP_STORED)
            # takes precedence over the archive-level default passed to
            # ZipFile() above -- set explicitly, or a "compressed" case
            # silently writes stored members instead.
            info.compress_type = compress_type

            if name in executable:
                info.external_attr = 0o100755 << 16

            else:
                info.external_attr = 0o100644 << 16

            archive.writestr(info, data)


def _sample_members() -> dict[str, bytes]:
    return {
        "pkg-1.0/pkg/__init__.py": b"NAME = 'pkg'\n",
        "pkg-1.0/pkg/nested/deep.py": b"VALUE = 1\n",
        "pkg-1.0/empty.txt": b"",
        "pkg-1.0/unicode-éè.txt": "café naïve".encode(),
        "pkg-1.0/binary.bin": bytes(random.Random(0).randbytes(4096)),
    }


def _extract_via_zipfile(archive_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
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


def _assert_matches_real_zipfile(
    tmp_path: Path,
    archive_path: Path,
    *,
    flatten: bool = False,
    check_executable: bool = True,
) -> None:
    fast_dest = tmp_path / "fast"

    fast_dest.mkdir()

    accepted = unpacking._fast_unzip(str(archive_path), str(fast_dest), flatten)

    assert accepted, "expected the fast path to accept this archive"

    real_dest = tmp_path / "real"

    real_dest.mkdir()

    _extract_via_zipfile(archive_path, real_dest)

    fast_tree = _tree(fast_dest)

    real_tree = _tree(real_dest)

    if flatten:
        # unzip_file()'s own leading-dir flatten only applies to the fast
        # path's output here -- compare against a manually-flattened view
        # of the real extraction instead of re-deriving the prefix. The
        # top-level directory placeholder itself (rest == "") vanishes
        # entirely once flattened, rather than surviving under its own name.
        stripped = {}

        for rel, contents in real_tree.items():
            _prefix, _, rest = rel.partition("/")

            if rest:
                stripped[rest] = contents

        real_tree = stripped

    assert fast_tree == real_tree

    if check_executable:
        for rel_path in _tree(real_dest):
            real_mode = os.stat(real_dest / rel_path).st_mode

            if not stat.S_ISREG(real_mode) or not (real_mode & 0o111):
                continue

            fast_path = fast_dest / (
                rel_path.split("/", 1)[-1] if flatten else rel_path
            )

            fast_mode = os.stat(fast_path).st_mode

            assert fast_mode & 0o111, f"{rel_path} should be executable"


class TestFastUnzipMatchesRealZipfile:
    @pytest.mark.parametrize(
        "compress_type", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]
    )
    def test_plain_archive(self, tmp_path: Path, compress_type: int) -> None:
        archive_path = tmp_path / "sample.zip"

        _write_zip(archive_path, _sample_members(), compress_type=compress_type)

        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                assert info.compress_type == compress_type

        _assert_matches_real_zipfile(tmp_path, archive_path)

    def test_flatten_strips_leading_dir(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "sample.zip"

        _write_zip(archive_path, _sample_members())

        _assert_matches_real_zipfile(tmp_path, archive_path, flatten=True)

    def test_executable_files(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "exec.zip"

        members = {"pkg/run.sh": b"#!/bin/sh\necho hi\n"}

        _write_zip(archive_path, members, executable=frozenset({"pkg/run.sh"}))

        _assert_matches_real_zipfile(tmp_path, archive_path)

    def test_many_small_members(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "many.zip"

        members = {f"pkg/module_{i}.py": f"VALUE = {i}\n".encode() for i in range(500)}

        _write_zip(archive_path, members)

        _assert_matches_real_zipfile(tmp_path, archive_path)

    def test_explicit_directory_entries(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "dirs.zip"

        with zipfile.ZipFile(archive_path, "w") as archive:
            for name in ("pkg/", "pkg/sub/", "pkg/sub/deep/"):
                archive.writestr(name, "")

            archive.writestr("pkg/sub/deep/file.txt", "leaf")

        _assert_matches_real_zipfile(tmp_path, archive_path)


class TestFastUnzipDeclines:
    def test_duplicate_member_name_declines(self, tmp_path: Path) -> None:
        """A corrupted *earlier* duplicate-named record must not be able to
        hide behind a valid later one -- WheelArchive declines the whole
        archive up front rather than silently keeping only one record.
        """
        archive_path = tmp_path / "duplicate.zip"

        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("only.txt", "first record")

            archive.writestr("only.txt", "second record")

        dest = tmp_path / "dest"

        dest.mkdir()

        assert unpacking._fast_unzip(str(archive_path), str(dest), False) is False

        assert list(dest.iterdir()) == []

    def test_unzip_file_falls_back_and_still_extracts_duplicate_names(
        self,
        tmp_path: Path,
    ) -> None:
        archive_path = tmp_path / "duplicate.zip"

        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("only.txt", "first record")

            archive.writestr("only.txt", "second record")

        dest = tmp_path / "dest"

        unpacking.unzip_file(str(archive_path), str(dest), flatten=False)

        # The slow zipfile-based path processes both records in order, so
        # the later one wins -- same last-write-wins outcome the fast path
        # would have produced too, had it not declined up front.
        assert (Path(dest) / "only.txt").read_bytes() == b"second record"

    def test_zip64_sentinel_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "zip64.zip"

        _write_zip(
            archive_path, {"big.bin": b"x" * 1024}, compress_type=zipfile.ZIP_STORED
        )

        raw = bytearray(archive_path.read_bytes())

        # A real zip64 archive (a member actually needing extended fields)
        # takes gigabytes to produce; patch the central directory record's
        # compressed-size field to the 0xFFFFFFFF sentinel directly instead,
        # to exercise the same detection a real oversized archive would hit.
        cd_offset = raw.index(zipfile.stringCentralDir)  # ty:ignore[unresolved-attribute]

        compressed_size_offset = cd_offset + 20

        raw[compressed_size_offset : compressed_size_offset + 4] = (
            0xFFFFFFFF
        ).to_bytes(
            4,
            "little",
        )

        archive_path.write_bytes(bytes(raw))

        dest = tmp_path / "dest"

        dest.mkdir()

        assert unpacking._fast_unzip(str(archive_path), str(dest), False) is False

        assert list(dest.iterdir()) == []

    def test_oversized_member_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "large.zip"

        members = {
            "small.txt": b"tiny",
            "large.bin": bytes(random.Random(1).randbytes(2 * 1024 * 1024)),
        }

        _write_zip(archive_path, members, compress_type=zipfile.ZIP_STORED)

        dest = tmp_path / "dest"

        dest.mkdir()

        assert unpacking._fast_unzip(str(archive_path), str(dest), False) is False

        assert list(dest.iterdir()) == []

    def test_not_a_zip_file_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "notazip.zip"

        archive_path.write_bytes(b"not actually a zip archive")

        dest = tmp_path / "dest"

        dest.mkdir()

        assert unpacking._fast_unzip(str(archive_path), str(dest), False) is False

        assert list(dest.iterdir()) == []

    def test_encrypted_flag_declines(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "flagged.zip"

        _write_zip(archive_path, {"only.txt": b"contents"})

        raw = bytearray(archive_path.read_bytes())

        cd_offset = raw.index(zipfile.stringCentralDir)  # ty:ignore[unresolved-attribute]

        flag_offset = cd_offset + 8

        raw[flag_offset] |= 0x1

        archive_path.write_bytes(bytes(raw))

        dest = tmp_path / "dest"

        dest.mkdir()

        assert unpacking._fast_unzip(str(archive_path), str(dest), False) is False

        assert list(dest.iterdir()) == []


class TestFastUnzipSecurity:
    def test_path_traversal_raises_and_matches_slow_path_message(
        self,
        tmp_path: Path,
    ) -> None:
        archive_path = tmp_path / "escape.zip"

        _write_zip(
            archive_path,
            {
                "regular_file.txt": b"fine",
                "../outside_file.txt": b"escape attempt",
            },
        )

        dest = tmp_path / "dest"

        dest.mkdir()

        with pytest.raises(
            InstallationError, match="trying to install outside target directory"
        ):
            unpacking._fast_unzip(str(archive_path), str(dest), False)

    def test_dot_dot_that_stays_inside_is_allowed(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "inside.zip"

        _write_zip(
            archive_path,
            {
                "regular_file1.txt": b"a",
                "dir/dir_file1.txt": b"b",
                "dir/../dir_file2.txt": b"c",
            },
        )

        dest = tmp_path / "dest"

        dest.mkdir()

        assert unpacking._fast_unzip(str(archive_path), str(dest), False) is True

        assert (dest / "regular_file1.txt").read_bytes() == b"a"

        assert (dest / "dir_file2.txt").read_bytes() == b"c"


class TestFastUnzipCorruption:
    def test_corrupted_member_raises_installation_error(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "corrupt.zip"

        _write_zip(
            archive_path, {"only.txt": b"a" * 500}, compress_type=zipfile.ZIP_STORED
        )

        raw = bytearray(archive_path.read_bytes())

        data_offset = 30 + len(b"only.txt")

        raw[data_offset] ^= 0xFF

        archive_path.write_bytes(bytes(raw))

        dest = tmp_path / "dest"

        dest.mkdir()

        with pytest.raises(InstallationError, match="Bad zip member"):
            unpacking._fast_unzip(str(archive_path), str(dest), False)


class TestUnzipFileUsesFastPath:
    def test_unzip_file_end_to_end_via_fast_path(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "sample.zip"

        _write_zip(archive_path, _sample_members())

        dest = tmp_path / "dest"

        unpacking.unzip_file(str(archive_path), str(dest), flatten=False)

        real_dest = tmp_path / "real"

        real_dest.mkdir()

        _extract_via_zipfile(archive_path, real_dest)

        assert _tree(dest) == _tree(real_dest)

    def test_unzip_file_falls_back_for_declined_archive(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "zip64.zip"

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
            with archive.open("big.bin", "w", force_zip64=True) as member:
                member.write(b"x" * 1024)

        dest = tmp_path / "dest"

        unpacking.unzip_file(str(archive_path), str(dest), flatten=False)

        assert (Path(dest) / "big.bin").read_bytes() == b"x" * 1024
