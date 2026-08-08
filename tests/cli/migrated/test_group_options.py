"""``--group`` handling for the commands that are not ``install``."""

from __future__ import annotations

from pathlib import Path

import pytest
from cpip.cli.main import main

PACKAGES = Path(__file__).resolve().parents[1] / "data" / "packages"

GROUP_FILE = '[dependency-groups]\nreqs = ["simplewheel==2.0"]\n'


def test_download_reads_dependency_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``download --group`` used to be accepted and then ignored."""
    (tmp_path / "pyproject.toml").write_text(GROUP_FILE, encoding="utf-8")
    destination = tmp_path / "dest"
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "download",
            "--no-index",
            "-f",
            str(PACKAGES),
            "-d",
            str(destination),
            "--group",
            "reqs",
        ],
    )

    assert status == 0
    assert list(destination.glob("simplewheel-2.0*.whl"))


def test_wheel_group_accepts_explicit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wheel --group FILE:NAME`` used to be read as a group literally named
    ``FILE:NAME`` in ``pyproject.toml``."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "pyproject.toml").write_text(GROUP_FILE, encoding="utf-8")
    wheel_dir = tmp_path / "wheels"
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "wheel",
            "--no-index",
            "-f",
            str(PACKAGES),
            "-w",
            str(wheel_dir),
            "--group",
            "sub/pyproject.toml:reqs",
        ],
    )

    assert status == 0
    assert list(wheel_dir.glob("simplewheel-2.0*.whl"))
