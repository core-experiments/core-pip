"""open_wheel_archive serves a resolver layout without re-reading the directory."""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

import pytest
from cpip.core.wheel import wheel_candidate_from_path
from cpip.index.candidate_materialization import _open_resolver_wheel_archive
from cpip.install.wheel_archive_runtime import RawWheelArchive, open_wheel_archive


def _write_wheel(path: Path, *, big_member: bool = False, bzip2: bool = False) -> None:
    dist_info = "demo-1.0.dist-info"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("demo/__init__.py", b"VALUE = 1\n")
        stored = zipfile.ZipInfo("demo/data.bin")
        stored.compress_type = zipfile.ZIP_STORED
        archive.writestr(stored, bytes(range(256)) * 4)
        script = zipfile.ZipInfo("demo-1.0.data/scripts/demo-tool")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        script.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(script, b"#!python\nprint('hi')\n")
        if big_member:
            archive.writestr("demo/big.bin", b"\0" * (1024 * 1024 + 1))
        if bzip2:
            odd = zipfile.ZipInfo("demo/odd.txt")
            odd.compress_type = zipfile.ZIP_BZIP2
            archive.writestr(odd, b"bzip2 member\n")
        archive.writestr(
            f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")


def _members(archive) -> dict[str, tuple[int, int, bytes]]:  # noqa: ANN001
    return {
        info.filename: (info.file_size, info.external_attr, archive.read(info.filename))
        for info in archive.infolist()
        if not info.is_dir()
    }


@pytest.fixture
def wheel(tmp_path: Path) -> Path:
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    _write_wheel(path)
    return path


def test_layout_open_matches_zipfile_including_modes(wheel: Path) -> None:
    candidate = wheel_candidate_from_path(os.fspath(wheel))
    layout = candidate.wheel_layout
    assert isinstance(layout, tuple)
    assert all(len(member) == 7 for member in layout[1])

    with open_wheel_archive(os.fspath(wheel), candidate) as archive:
        assert isinstance(archive, RawWheelArchive)
        raw = _members(archive)
    with zipfile.ZipFile(wheel) as archive:
        expected = _members(archive)
    assert raw == expected
    assert (raw["demo-1.0.data/scripts/demo-tool"][1] >> 16) & 0o777 == 0o755


def test_layout_open_does_not_read_the_central_directory(
    wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cpip.resolution import archive as archive_module

    candidate = wheel_candidate_from_path(os.fspath(wheel))

    def no_scan(self):  # noqa: ANN001, ANN202
        raise AssertionError("central directory re-read despite a layout")

    monkeypatch.setattr(archive_module.WheelArchive, "read_central_directory", no_scan)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("zipfile used"))
    with open_wheel_archive(os.fspath(wheel), candidate) as archive:
        assert archive.read("demo/__init__.py") == b"VALUE = 1\n"


@pytest.mark.parametrize("kind", ["big_member", "bzip2"])
def test_layout_outside_the_streaming_contract_falls_back_to_zipfile(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    _write_wheel(path, **{kind: True})
    candidate = wheel_candidate_from_path(os.fspath(path))
    assert isinstance(candidate.wheel_layout, tuple)
    with open_wheel_archive(os.fspath(path), candidate) as archive:
        assert isinstance(archive, zipfile.ZipFile)
        assert archive.read("demo/__init__.py") == b"VALUE = 1\n"


def test_legacy_six_field_layout_falls_back_to_zipfile(wheel: Path) -> None:
    candidate = wheel_candidate_from_path(os.fspath(wheel))
    layout = candidate.wheel_layout
    assert isinstance(layout, tuple)
    legacy = candidate.copy_with(
        wheel_layout=(layout[0], tuple(member[:6] for member in layout[1]), layout[2]),
    )
    with open_wheel_archive(os.fspath(wheel), legacy) as archive:
        assert isinstance(archive, zipfile.ZipFile)


def test_resolver_archive_reports_modes_like_zipfile(wheel: Path) -> None:
    with _open_resolver_wheel_archive(os.fspath(wheel)) as archive:
        assert not isinstance(archive, zipfile.ZipFile)
        fast = {name: info.external_attr for name, info in archive.NameToInfo.items()}
    with zipfile.ZipFile(wheel) as archive:
        expected = {info.filename: info.external_attr for info in archive.infolist()}
    assert fast == expected


def test_materialized_candidate_layout_carries_modes(wheel: Path) -> None:
    """The materializer's layout (built through the resolver reader) has the
    same member records, modes included, as one built through zipfile."""
    from cpip.core.wheel import wheel_candidate

    with _open_resolver_wheel_archive(os.fspath(wheel)) as archive:
        via_fast = wheel_candidate(
            os.fspath(wheel),
            archive=archive,
            dist_info_dir="demo-1.0.dist-info",
        ).wheel_layout
    with zipfile.ZipFile(wheel) as archive:
        via_zip = wheel_candidate(
            os.fspath(wheel),
            archive=archive,
            dist_info_dir="demo-1.0.dist-info",
        ).wheel_layout
    assert via_fast == via_zip
