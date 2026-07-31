import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from email import message_from_string
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from cpip.core import wheel
from cpip.core.errors import UnsupportedWheel

ZipDir = Callable[[Path], ZipFile]


@pytest.fixture
def zip_dir() -> Iterator[ZipDir]:
    def make_zip(path: Path) -> ZipFile:
        buf = BytesIO()
        with ZipFile(buf, "w", allowZip64=True) as z:
            for dirpath, _, filenames in os.walk(path):
                for filename in filenames:
                    file_path = os.path.join(path, dirpath, filename)
                    # Zip files must always have / as path separator
                    archive_path = os.path.relpath(file_path, path).replace(
                        os.pathsep, "/"
                    )
                    z.write(file_path, archive_path)

        return stack.enter_context(ZipFile(buf, "r", allowZip64=True))

    stack = ExitStack()
    with stack:
        yield make_zip


def test_wheel_dist_info_dir_found(tmp_path: Path, zip_dir: ZipDir) -> None:
    expected = "simple-0.1.dist-info"
    dist_info_dir = tmp_path / expected
    dist_info_dir.mkdir()
    dist_info_dir.joinpath("WHEEL").touch()
    assert wheel.wheel_dist_info_dir(zip_dir(tmp_path), "simple") == expected


def test_wheel_dist_info_dir_multiple(tmp_path: Path, zip_dir: ZipDir) -> None:
    dist_info_dir_1 = tmp_path / "simple-0.1.dist-info"
    dist_info_dir_1.mkdir()
    dist_info_dir_1.joinpath("WHEEL").touch()
    dist_info_dir_2 = tmp_path / "unrelated-0.1.dist-info"
    dist_info_dir_2.mkdir()
    dist_info_dir_2.joinpath("WHEEL").touch()
    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_dist_info_dir(zip_dir(tmp_path), "simple")
    assert "multiple .dist-info directories found" in str(e.value)


def test_wheel_dist_info_dir_none(tmp_path: Path, zip_dir: ZipDir) -> None:
    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_dist_info_dir(zip_dir(tmp_path), "simple")
    assert "directory not found" in str(e.value)


def test_wheel_dist_info_dir_wrong_name(tmp_path: Path, zip_dir: ZipDir) -> None:
    dist_info_dir = tmp_path / "unrelated-0.1.dist-info"
    dist_info_dir.mkdir()
    dist_info_dir.joinpath("WHEEL").touch()
    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_dist_info_dir(zip_dir(tmp_path), "simple")
    assert "does not start with 'simple'" in str(e.value)


def test_wheel_version_ok() -> None:
    assert wheel.wheel_version(message_from_string("Wheel-Version: 1.9")) == (1, 9)


def test_parse_wheel_validates_and_returns_metadata(
    tmp_path: Path, zip_dir: ZipDir
) -> None:
    dist_info_dir = tmp_path / "simple-0.1.dist-info"
    dist_info_dir.mkdir()
    dist_info_dir.joinpath("WHEEL").write_text("Wheel-Version: 1.0\n")

    info_dir, metadata = wheel.parse_wheel(zip_dir(tmp_path), "simple")

    assert info_dir == dist_info_dir.name
    assert metadata["Wheel-Version"] == "1.0"


def test_validate_wheel_skips_email_metadata_parser(
    tmp_path: Path, zip_dir: ZipDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist_info_dir = tmp_path / "simple-0.1.dist-info"
    dist_info_dir.mkdir()
    dist_info_dir.joinpath("WHEEL").write_text("Wheel-Version: 1.0\n")

    def fail_parser() -> None:
        raise AssertionError("constructed an email parser")

    monkeypatch.setattr(wheel, "Parser", fail_parser)

    assert wheel.validate_wheel(zip_dir(tmp_path), "simple") == dist_info_dir.name


def test_wheel_metadata_fails_missing_wheel(tmp_path: Path, zip_dir: ZipDir) -> None:
    dist_info_dir = tmp_path / "simple-0.1.0.dist-info"
    dist_info_dir.mkdir()
    dist_info_dir.joinpath("METADATA").touch()

    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_metadata(zip_dir(tmp_path), dist_info_dir.name)
    assert "could not read" in str(e.value)


def test_wheel_metadata_fails_on_bad_encoding(tmp_path: Path, zip_dir: ZipDir) -> None:
    dist_info_dir = tmp_path / "simple-0.1.0.dist-info"
    dist_info_dir.mkdir()
    dist_info_dir.joinpath("METADATA").touch()
    dist_info_dir.joinpath("WHEEL").write_bytes(b"\xff")

    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_metadata(zip_dir(tmp_path), dist_info_dir.name)
    assert "error decoding" in str(e.value)


def test_wheel_version_fails_on_no_wheel_version() -> None:
    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_version(message_from_string(""))
    assert "missing Wheel-Version" in str(e.value)


@pytest.mark.parametrize(
    "version",
    [
        ("",),
        ("1.b",),
        ("1.",),
    ],
)
def test_wheel_version_fails_on_bad_wheel_version(version: str) -> None:
    with pytest.raises(UnsupportedWheel) as e:
        wheel.wheel_version(message_from_string(f"Wheel-Version: {version}"))
    assert "invalid Wheel-Version" in str(e.value)


def test_check_compatibility() -> None:
    name = "test"
    vc = wheel.VERSION_COMPATIBLE

    # Major version is higher - should be incompatible
    higher_v = (vc[0] + 1, vc[1])

    # test raises with correct error
    with pytest.raises(UnsupportedWheel) as e:
        wheel.check_compatibility(higher_v, name)
    assert "is not compatible" in str(e)

    # Should only log.warning - minor version is greater
    higher_v = (vc[0], vc[1] + 1)
    wheel.check_compatibility(higher_v, name)

    # These should work fine
    wheel.check_compatibility(wheel.VERSION_COMPATIBLE, name)

    # E.g if wheel to install is 1.0 and we support up to 1.2
    lower_v = (vc[0], max(0, vc[1] - 1))
    wheel.check_compatibility(lower_v, name)
