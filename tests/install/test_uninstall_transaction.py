from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from cpip.core.errors import InstallationError
from cpip.install.requirements import RequirementInstaller
from cpip.install.target import InstallTarget
from cpip.install.wheel_transaction import WheelInstaller


def wheel_internal(directory: Path) -> Path:
    path = directory / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo/__init__.py", "value = 1\n")
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("demo-1.0.dist-info/RECORD", "")
    return path


def install_internal(path: Path, target: Path) -> None:
    WheelInstaller(
        InstallTarget.from_options("demo", target=str(target)),
        pycompile=False,
    ).install(
        path,
    )


def test_uninstall_preserves_unrelated_and_unsafe_record_paths(tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    install_internal(wheel_internal(tmp_path), target)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    record = target / "demo-1.0.dist-info" / "RECORD"
    with record.open("a", encoding="utf-8") as file:
        file.write(f"{outside},,\n../outside.txt,,\n")

    assert RequirementInstaller().uninstall("demo", paths=[str(target)])
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_removes_symlink_without_following_target(tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    install_internal(wheel_internal(tmp_path), target)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = target / "demo-link"
    link.symlink_to(outside)
    with (target / "demo-1.0.dist-info" / "RECORD").open(
        "a",
        encoding="utf-8",
    ) as record:
        record.write("demo-link,,\n")

    assert RequirementInstaller().uninstall("demo", paths=[str(target)])
    assert not os.path.lexists(link)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_requires_record(tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    install_internal(wheel_internal(tmp_path), target)
    (target / "demo-1.0.dist-info" / "RECORD").unlink()

    with pytest.raises(InstallationError, match="no RECORD file was found"):
        RequirementInstaller().uninstall("demo", paths=[str(target)])
