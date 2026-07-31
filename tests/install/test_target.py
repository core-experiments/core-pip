import os
import sysconfig
from pathlib import Path

import pytest
from cpip.install.target import InstallTarget


def test_target_mode_uses_one_contained_destination(tmp_path: Path) -> None:
    target = InstallTarget.from_options("demo", target=os.fspath(tmp_path))

    assert target.purelib == tmp_path
    assert target.platlib == tmp_path
    assert target.scripts == Path(sysconfig.get_path("scripts"))
    assert target.data == tmp_path


def test_target_mode_applies_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = InstallTarget.from_options("demo", target="/target", root=os.fspath(root))

    assert target.purelib == root / "target"
    scripts = Path(sysconfig.get_path("scripts"))
    if scripts.is_absolute():
        scripts = Path(*scripts.parts[1:])
    assert target.scripts == root / scripts


def test_destination_rejects_path_escape(tmp_path: Path) -> None:
    target = InstallTarget.from_options("demo", target=os.fspath(tmp_path))

    with pytest.raises(ValueError, match="escapes"):
        target.destination("../outside")
