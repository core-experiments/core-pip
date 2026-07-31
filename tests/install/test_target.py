import os
from pathlib import Path

import pytest
from cpip.install.target import InstallTarget


def test_target_mode_uses_one_contained_destination(tmp_path: Path) -> None:
    target = InstallTarget.from_options("demo", target=os.fspath(tmp_path))

    assert target.purelib == tmp_path
    assert target.platlib == tmp_path
    assert target.scripts == (tmp_path / "bin").resolve()
    assert target.data == tmp_path


def test_target_mode_applies_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = InstallTarget.from_options("demo", target="/target", root=os.fspath(root))

    assert target.purelib == root / "target"
    assert target.scripts == root / "target" / "bin"


def test_destination_rejects_path_escape(tmp_path: Path) -> None:
    target = InstallTarget.from_options("demo", target=os.fspath(tmp_path))

    with pytest.raises(ValueError, match="escapes"):
        target.destination("../outside")
