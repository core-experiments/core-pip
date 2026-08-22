"""The fast ``freeze``: the normal path's lines for an environment it can
describe without the full version parser, VCS or logging."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from cpip.cli import fast


def _dist(
    root: Path,
    dirname: str,
    name: str,
    version: str,
    *,
    direct_url: dict | None = None,
    filename: str = "METADATA",
) -> Path:  # noqa: E501
    info = root / dirname
    info.mkdir(parents=True, exist_ok=True)
    (info / filename).write_text(f"Name: {name}\nVersion: {version}\n")
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
    return info


def _normal(args: list[str]) -> str:
    from cpip.cli.freeze import run_freeze

    out = io.StringIO()
    with redirect_stdout(out):
        assert run_freeze(args) == 0
    return out.getvalue()


def _fast(args: list[str]) -> str | None:
    out = io.StringIO()
    with redirect_stdout(out):
        status = fast.run_freeze(args)
    return None if status is None else out.getvalue()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _dist(first, "Zeta_Pkg-1.0.dist-info", "Zeta-Pkg", "1.0")
    _dist(first, "shadowed-1.0.dist-info", "shadowed", "1.0")
    _dist(second, "shadowed-2.0.dist-info", "shadowed", "2.0")
    _dist(second, "alpha-2.0.1.dist-info", "alpha", "2.0.1")
    _dist(
        second,
        "from_url-0.5.dist-info",
        "from-url",
        "0.5",
        direct_url={
            "url": "https://example.invalid/from_url-0.5.tar.gz",
            "archive_info": {"hashes": {"sha256": "ab" * 32}},
        },
    )
    _dist(second, "cpip-0.0.1.dist-info", "cpip", "0.0.1")
    _dist(second, "argparse-1.4.0.dist-info", "argparse", "1.4.0")
    (second / "empty-9.dist-info").mkdir()
    monkeypatch.setattr(
        sys, "path", [str(tmp_path / "missing"), str(first), str(second)]
    )
    monkeypatch.delenv("CPIP_TARGET_PREFIX", raising=False)
    return tmp_path


def test_matches_the_normal_path(site: Path) -> None:
    for args in (
        [],
        ["--all"],
        ["--exclude", "alpha"],
        ["--exclude", "cpip"],
        ["--exclude-editable"],
        ["--path", str(site / "second")],
        ["--path", str(site / "second"), "--path", str(site / "first"), "--all"],
    ):
        assert _fast(args) == _normal(args), args

    out = _fast([])
    assert out == (
        "alpha==2.0.1\n"
        "from-url @ https://example.invalid/from_url-0.5.tar.gz#sha256="
        + "ab"
        * 32
        + "\n"
        "shadowed==1.0\n"
        "Zeta-Pkg==1.0\n"
    )


def test_editables_are_the_normal_path_unless_excluded(
    site: Path, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _dist(
        site / "first",
        "editable_pkg-3.0.dist-info",
        "editable-pkg",
        "3.0",
        direct_url={"url": src.as_uri(), "dir_info": {"editable": True}},
    )
    assert _fast([]) is None
    assert _fast(["--exclude-editable"]) == _normal(["--exclude-editable"])
    assert "editable" not in (_fast(["--exclude-editable"]) or "editable")


def test_declines_what_needs_the_normal_path(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for args in (["-r", "req.txt"], ["--user"], ["-v"], ["--format=json"]):
        assert _fast(args) is None, args

    _dist(site / "first", "pre-1.0rc1.dist-info", "pre", "1.0rc1")
    assert _fast([]) is None
    (site / "first" / "pre-1.0rc1.dist-info" / "METADATA").unlink()
    (site / "first" / "pre-1.0rc1.dist-info").rmdir()
    assert _fast([]) is not None

    _dist(site / "first", "bad_name-1.0.dist-info", "bad-name-", "1.0")
    assert _fast([]) is None
    (site / "first" / "bad_name-1.0.dist-info" / "METADATA").unlink()
    (site / "first" / "bad_name-1.0.dist-info").rmdir()

    _dist(site / "first", "legacy-0.1.egg-info", "legacy", "0.1", filename="PKG-INFO")
    assert _fast([]) is None
    (site / "first" / "legacy-0.1.egg-info" / "PKG-INFO").unlink()
    (site / "first" / "legacy-0.1.egg-info").rmdir()

    monkeypatch.setenv("CPIP_TARGET_PREFIX", "/elsewhere")
    assert _fast([]) is None
