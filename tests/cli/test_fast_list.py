"""The fast ``list``: a directory listing rendered exactly as the normal path
renders it, declining whatever it cannot render identically."""

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
    filename: str = "METADATA",
    build: str | None = None,
    editable_dir: Path | None = None,
    installer: str | None = None,
) -> Path:
    info = root / dirname
    info.mkdir(parents=True, exist_ok=True)
    (info / filename).write_text(f"Name: {name}\nVersion: {version}\n")
    if build is not None:
        (info / "WHEEL").write_text(
            f"Wheel-Version: 1.0\nBuild: {build}\nTag: py3-none-any\n"
        )
    if editable_dir is not None:
        (info / "direct_url.json").write_text(
            json.dumps({"url": editable_dir.as_uri(), "dir_info": {"editable": True}})
        )
    if installer is not None:
        (info / "INSTALLER").write_text(f"{installer}\n")
    return info


def _normal(args: list[str]) -> str:
    from cpip.cli.list import run_list

    out = io.StringIO()
    with redirect_stdout(out):
        assert run_list(args) == 0
    return out.getvalue()


def _fast(args: list[str]) -> str | None:
    out = io.StringIO()
    with redirect_stdout(out):
        status = fast.run_list(args)
    return None if status is None else out.getvalue()


@pytest.mark.parametrize(
    "value",
    ["plain", 'q"uote', "back\\slash", "tab\tnew\nline", "\x01", "café", "\U0001f600"],
)
def test_json_string_matches_json_dumps(value: str) -> None:
    assert fast.json_string(value) == json.dumps(value)


@pytest.mark.parametrize(
    "value, canonical",
    [
        ("1.0", True),
        ("2024.1.0", True),
        ("0", True),
        ("1.00", False),
        ("01.0", False),
        ("1.0rc1", False),
        ("1.0.post1", False),
        ("1!1.0", False),
        ("1.0+local", False),
    ],
)
def test_canonical_release(value: str, canonical: bool) -> None:
    assert fast.canonical_release(value) is canonical


def test_every_format_matches_the_normal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    src = tmp_path / "src" / "editable-pkg"
    src.mkdir(parents=True)
    _dist(
        site, "Zeta_Pkg-1.0.dist-info", "Zeta-Pkg", "1.0", build="7", installer="cpip"
    )
    _dist(
        site,
        "alpha-2.0.1.dist-info",
        "alpha",
        "2.0.1",
        editable_dir=src,
        installer="uv",
    )
    _dist(site, "Mid-0.3.dist-info", "mid", "0.3")
    _dist(site, "argparse-1.4.0.dist-info", "argparse", "1.4.0")  # stdlib name: skipped
    (site / "empty-9.dist-info").mkdir()  # no metadata: skipped
    (site / "notes.txt").write_text("x")
    monkeypatch.setattr(sys, "path", [str(tmp_path / "missing"), str(site)])
    monkeypatch.delenv("CPIP_TARGET_PREFIX", raising=False)

    for args in (
        [],
        ["-v"],
        ["--format=json"],
        ["--format", "json", "-v"],
        ["--format=freeze"],
        ["--format=freeze", "-v"],
        ["--exclude", "mid"],
        ["--exclude", "Zeta-pkg", "--exclude", "alpha"],
        ["--path", str(site)],
        ["--path", str(site), "--format=json", "-vv"],
    ):
        assert _fast(args) == _normal(args), args

    out = _fast([])
    assert out is not None
    assert "Editable project location" in out
    assert "Build" in out
    assert str(src) in out
    assert "argparse" not in out
    assert "empty" not in out


def test_declines_what_it_cannot_render_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    _dist(site, "simple-1.0.dist-info", "simple", "1.0")
    monkeypatch.setattr(sys, "path", [str(site)])
    monkeypatch.delenv("CPIP_TARGET_PREFIX", raising=False)
    assert _fast([]) is not None

    # Options the recognizer does not implement.
    for args in (
        ["--outdated"],
        ["--user"],
        ["--local"],
        ["--not-required"],
        ["--format=xml"],
        ["--editable"],
        ["--pre"],
    ):
        assert _fast(args) is None, args

    # Versions the json and freeze formats would print normalized.
    _dist(site, "pre-1.0rc1.dist-info", "pre", "1.0rc1")
    assert _fast([]) is not None
    assert _fast(["--format=json"]) is None
    assert _fast(["--format=freeze"]) is None

    # An egg-info entry: its editable status is an egg-link question.
    _dist(site, "legacy-0.1.egg-info", "legacy", "0.1", filename="PKG-INFO")
    assert _fast([]) is None

    # A redirected target interpreter.
    monkeypatch.setattr(sys, "path", [str(tmp_path / "other")])
    (tmp_path / "other").mkdir()
    assert _fast([]) is not None
    monkeypatch.setenv("CPIP_TARGET_PREFIX", "/elsewhere")
    assert _fast([]) is None


def test_pip_exclusion_covers_cpip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES

    site = tmp_path / "site"
    for name in (*CPIP_DISTRIBUTION_NAMES, "pip", "keep"):
        _dist(site, f"{name.replace('-', '_')}-1.0.dist-info", name, "1.0")
    monkeypatch.setattr(sys, "path", [str(site)])
    monkeypatch.delenv("CPIP_TARGET_PREFIX", raising=False)

    assert (
        _fast(["--exclude", "pip"])
        == _normal(["--exclude", "pip"])
        == "Package Version\nkeep    1.0\n"
    )
