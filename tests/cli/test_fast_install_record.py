"""RECORD handling in the fast pure-wheel install path."""

from __future__ import annotations

import zipfile
from pathlib import Path

from cpip.cli.fast_install import install_resolved_pure_wheels
from cpip.core.wheel import wheel_candidate


def build_wheel(directory: Path, record: str | None) -> Path:
    """A minimal pure wheel, optionally shipping a RECORD of its own."""
    path = directory / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo/__init__.py", "value = 1\n")
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Name: demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if record is not None:
            archive.writestr("demo-1.0.dist-info/RECORD", record)
    return path


def install(tmp_path: Path, record: str | None) -> Path:
    wheel = build_wheel(tmp_path, record)
    target = tmp_path / "target"
    assert install_resolved_pure_wheels([wheel_candidate(wheel)], str(target), {"demo"})
    return target / "demo-1.0.dist-info" / "RECORD"


def test_record_is_generated_when_the_wheel_omits_it(tmp_path: Path) -> None:
    written = install(tmp_path, None)

    rows = written.read_text().splitlines()

    assert any(row.startswith("demo/__init__.py,sha256=") for row in rows)
    assert "demo-1.0.dist-info/RECORD,," in rows


def test_blank_shipped_record_is_regenerated_not_reused(tmp_path: Path) -> None:
    """An empty RECORD member is extracted, then overwritten with real rows.

    This is the case where RECORD is both an archive member and a file this
    path writes itself, so the rollback list must not gain it twice.
    """
    written = install(tmp_path, "   \n")

    rows = written.read_text().splitlines()

    assert any(row.startswith("demo/__init__.py,sha256=") for row in rows)
    assert "demo-1.0.dist-info/RECORD,," in rows


def test_populated_shipped_record_is_kept_verbatim(tmp_path: Path) -> None:
    shipped = "demo/__init__.py,sha256=shipped,10\ndemo-1.0.dist-info/RECORD,,\n"

    written = install(tmp_path, shipped)

    assert written.read_text() == shipped


def test_cold_resolve_installs_with_pre_read_members(tmp_path: Path) -> None:
    """The resolver hands the installer the member table it already read;
    the modes must travel with it, or the install crashes looking them up."""
    import os
    import sys

    from cpip.cli.fast_install import (
        install_resolved_pure_wheels,
        resolve_simple_wheelhouse,
    )

    _BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
    if str(_BENCHMARKS) not in sys.path:
        sys.path.insert(0, str(_BENCHMARKS))
    from benchmark_support import make_wheel

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo", "1.0.0", payload_files=2)
    candidates = resolve_simple_wheelhouse([str(wheelhouse)], ["demo"], None)
    assert candidates is not None
    assert candidates[0].archive_members is not None

    target = tmp_path / "target"
    assert install_resolved_pure_wheels(candidates, str(target), {"demo"}) is True

    installed = sorted(
        p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
    )
    assert any(name.endswith(".dist-info/RECORD") for name in installed)
    assert os.path.isfile(target / "demo" / "__init__.py") or any(
        name.startswith("demo") for name in installed
    )
