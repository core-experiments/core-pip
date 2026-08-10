from __future__ import annotations

import json
from pathlib import Path

import pytest
from cpip_benchmark.compare import compare


def write_export(
    directory: Path, name: str, *, cpip_mean: float, uv_mean: float
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "results": [
                    {"command": f"cpip ({name})", "mean": cpip_mean, "stddev": 0.001},
                    {"command": f"uv ({name})", "mean": uv_mean, "stddev": 0.001},
                ],
            },
        ),
        encoding="utf-8",
    )


def write_meta(directory: Path, *, python_version: str) -> None:
    (directory / "meta.json").write_text(
        json.dumps(
            {
                "cpip_python_version": python_version,
                "uv_version": "uv 0.12.1",
                "git_commit": "deadbeef",
            },
        ),
        encoding="utf-8",
    )


def test_compare_reports_delta_per_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "lock-cold", cpip_mean=0.100, uv_mean=0.050)
    write_export(after, "lock-cold", cpip_mean=0.080, uv_mean=0.050)
    write_meta(before, python_version="Python 3.10.20")
    write_meta(after, python_version="Python 3.10.20")

    assert compare(before, after) == 0

    out = capsys.readouterr().out
    assert "lock-cold" in out
    assert "-20.0%" in out
    assert "+0.0%" in out


def test_compare_warns_on_mismatched_interpreter(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "startup-help", cpip_mean=0.050, uv_mean=0.010)
    write_export(after, "startup-help", cpip_mean=0.020, uv_mean=0.010)
    write_meta(before, python_version="Python 3.14.6")
    write_meta(after, python_version="Python 3.10.20")

    assert compare(before, after) == 0

    err = capsys.readouterr().err
    assert "cpip_python_version differs" in err
    assert "3.14.6" in err
    assert "3.10.20" in err


def test_compare_warns_when_metadata_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "startup-help", cpip_mean=0.050, uv_mean=0.010)
    write_export(after, "startup-help", cpip_mean=0.020, uv_mean=0.010)

    assert compare(before, after) == 0

    err = capsys.readouterr().err
    assert "no meta.json" in err


def test_compare_rejects_no_overlap(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "lock-cold", cpip_mean=0.100, uv_mean=0.050)
    write_export(after, "install-cold", cpip_mean=0.080, uv_mean=0.050)

    assert compare(before, after) == 1

    err = capsys.readouterr().err
    assert "No matching benchmark names" in err


def test_compare_reports_names_present_in_only_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "lock-cold", cpip_mean=0.100, uv_mean=0.050)
    write_export(before, "install-cold", cpip_mean=0.100, uv_mean=0.050)
    write_export(after, "lock-cold", cpip_mean=0.080, uv_mean=0.050)
    write_meta(before, python_version="Python 3.10.20")
    write_meta(after, python_version="Python 3.10.20")

    assert compare(before, after) == 0

    out = capsys.readouterr().out
    assert "install-cold" in out.rsplit("Skipped", 1)[-1]
