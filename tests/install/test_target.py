import os
from pathlib import Path

import pytest
from cpip.install.target import InstallTarget


def test_target_mode_uses_one_contained_destination(tmp_path: Path) -> None:
    target = InstallTarget.from_options("demo", target=os.fspath(tmp_path))

    assert target.purelib == os.fspath(tmp_path)
    assert target.platlib == os.fspath(tmp_path)
    assert target.scripts == os.fspath((tmp_path / "bin").resolve())
    assert target.data == os.fspath(tmp_path)


def test_target_mode_applies_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = InstallTarget.from_options("demo", target="/target", root=os.fspath(root))

    assert target.purelib == os.fspath(root / "target")
    assert target.scripts == os.fspath(root / "target" / "bin")


def test_destination_rejects_path_escape(tmp_path: Path) -> None:
    target = InstallTarget.from_options("demo", target=os.fspath(tmp_path))

    with pytest.raises(ValueError, match="escapes"):
        target.destination("../outside")
