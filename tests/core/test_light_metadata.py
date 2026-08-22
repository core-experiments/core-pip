"""LightDistribution reads each per-distribution file once."""

from __future__ import annotations

from pathlib import Path

from cpip.core.light_metadata import LightDistributionStore


def _install(site: Path, name: str, version: str) -> Path:
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    return info


def test_installer_and_direct_url_are_read_once(tmp_path: Path) -> None:
    info = _install(tmp_path, "pkg", "1.0")
    (info / "INSTALLER").write_text("cpip\n")
    (info / "direct_url.json").write_text(
        '{"url": "file:///src/pkg", "dir_info": {"editable": true}}'
    )
    [dist] = LightDistributionStore(paths=[str(tmp_path)]).iter()

    assert dist.installer == "cpip"
    assert dist.direct_url is not None
    assert dist.editable
    assert dist.editable_project_location == "/src/pkg"

    # The answer is the instance's; a later rewrite is not re-read.
    (info / "INSTALLER").write_text("other\n")
    (info / "direct_url.json").unlink()
    assert dist.installer == "cpip"
    assert dist.direct_url is not None
    assert dist.editable

    [fresh] = LightDistributionStore(paths=[str(tmp_path)]).iter()
    assert fresh.installer == "other"
    assert fresh.direct_url is None
    assert not fresh.editable


def test_missing_files_are_remembered_as_missing(tmp_path: Path) -> None:
    _install(tmp_path, "pkg", "1.0")
    [dist] = LightDistributionStore(paths=[str(tmp_path)]).iter()
    assert dist.installer == ""
    assert dist.direct_url is None
    assert dist.installer == ""
    assert dist.direct_url is None
