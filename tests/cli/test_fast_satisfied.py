"""The already-satisfied install recognizer: plain names, all installed."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from cpip.cli import fast


def _dist(
    root: Path, dirname: str, name: str, version: str, filename: str = "METADATA"
) -> None:
    (root / dirname).mkdir(parents=True, exist_ok=True)
    (root / dirname / filename).write_text(f"Name: {name}\nVersion: {version}\n")


@pytest.mark.parametrize(
    "token, expected",
    [
        ("simple", ("simple", "simple", "", None)),
        (" Simple_Pkg ", ("Simple_Pkg", "simple-pkg", "", None)),
        ("simple==2.0", ("simple==2.0", "simple", "==", (2,))),
        ("simple >= 1.0.0", ("simple >= 1.0.0", "simple", ">=", (1,))),
        ("simple<3", ("simple<3", "simple", "<", (3,))),
    ],
)
def test_plain_requirements(token: str, expected: tuple) -> None:
    assert fast.parse_plain_requirement(token) == expected


@pytest.mark.parametrize(
    "token",
    [
        "",
        "-e",
        "./wheel.whl",
        "simple[extra]",
        "simple; python_version > '3'",
        "simple @ https://example.invalid/s.whl",
        "https://example.invalid/s.whl",
        "simple~=1.0",
        "simple!=1.0",
        "simple===1.0",
        "simple==1.*",
        "simple>=1,<2",
        "simple==1.0a1",
        "simple==1.0.post1",
        "simple==1!1.0",
        "simple==1.0+local",
        "simple=1",
        "-simple",
        "simple-",
        "sïmple",
    ],
)
def test_other_requirement_shapes_decline(token: str) -> None:
    assert fast.parse_plain_requirement(token) is None


def test_arguments_accept_only_the_recognized_options() -> None:
    options = fast.parse_satisfied_arguments(
        ["--no-index", "-f", "/wh", "--find-links=/other", "-q", "simple", "other>=1"]
    )
    assert options is not None
    assert options.find_links == ["/wh", "/other"]
    assert options.quiet
    assert [item[1] for item in options.requirements] == ["simple", "other"]

    for args in (
        ["simple", "--target", "/t"],
        ["simple", "--upgrade"],
        ["simple", "-U"],
        ["simple", "--ignore-installed"],
        ["simple", "--force-reinstall"],
        ["simple", "-r", "req.txt"],
        ["simple", "-v"],
        ["simple", "--user"],
        ["simple", "--report", "r.json"],
        ["-f"],
        ["--no-index"],
        [],
    ):
        assert fast.parse_satisfied_arguments(args) is None, args


def test_installed_versions_first_root_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _dist(first, "Simple_Pkg-1.0.dist-info", "Simple-Pkg", "1.0")
    _dist(second, "simple_pkg-2.0.dist-info", "simple-pkg", "2.0")
    _dist(second, "legacy-0.1.egg-info", "legacy", "0.1", filename="PKG-INFO")
    (second / "flat-0.2.egg-info").write_text("Name: flat\nVersion: 0.2\n")
    _dist(second, "renamed-9.9.dist-info", "something-else", "9.9")
    monkeypatch.setattr(
        sys, "path", [str(tmp_path / "missing"), str(first), str(second)]
    )

    assert fast.installed_versions(
        {"simple-pkg", "legacy", "flat", "renamed", "absent"}
    ) == {
        "simple-pkg": "1.0",
        "legacy": "0.1",
        "flat": "0.2",
    }


def test_installed_versions_leaves_hard_cases_to_the_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "site"
    _dist(root, "simple-1.0.dist-info", "simple", "1.0")

    # Two entries for one name in one directory: the stdlib's listing order decides.
    _dist(root, "simple-2.0.dist-info", "simple", "2.0")
    monkeypatch.setattr(sys, "path", [str(root)])
    assert fast.installed_versions({"simple"}) is None
    (root / "simple-2.0.dist-info" / "METADATA").unlink()
    (root / "simple-2.0.dist-info").rmdir()
    assert fast.installed_versions({"simple"}) == {"simple": "1.0"}

    # A zip on sys.path and an egg directory are the stdlib's to read.
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("zipped-1.0.dist-info/METADATA", "Name: zipped\nVersion: 1.0\n")
    monkeypatch.setattr(sys, "path", [str(root), str(archive)])
    assert fast.installed_versions({"simple"}) is None
    egg = tmp_path / "old-1.0.egg"
    egg.mkdir()
    monkeypatch.setattr(sys, "path", [str(root), str(egg)])
    assert fast.installed_versions({"simple"}) is None

    # Another distribution finder on sys.meta_path.
    class Finder:
        @staticmethod
        def find_distributions(context=None):  # noqa: ANN001, ANN205
            return iter(())

    monkeypatch.setattr(sys, "path", [str(root)])
    monkeypatch.setattr(sys, "meta_path", [Finder(), *sys.meta_path])
    assert fast.installed_versions({"simple"}) is None


def test_run_reports_satisfied_requirements_like_the_normal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "site"
    _dist(root, "simple-2.0.0.dist-info", "simple", "2.0.0")
    _dist(root, "other-1.5.dist-info", "other", "1.5")
    monkeypatch.setattr(sys, "path", [str(root)])
    monkeypatch.delenv("CPIP_TARGET_PREFIX", raising=False)
    monkeypatch.delenv("CPIP_RESOLVER_DEBUG", raising=False)

    import cpip.cli.config as config

    class Config:
        find_links: list[str] = []

    monkeypatch.setattr(config, "load_source_config", lambda command: Config())

    args = ["--no-index", "-f", "/wh", "simple", "Other>=1.2", "simple==2"]
    assert fast.run_satisfied_install(args) == 0
    assert capsys.readouterr().out == (
        "Looking in links: /wh\n"
        "Requirement already satisfied: simple\n"
        "Requirement already satisfied: Other>=1.2\n"
        "Requirement already satisfied: simple==2\n"
    )

    assert fast.run_satisfied_install(["-q", "simple"]) == 0
    assert capsys.readouterr().out == ""

    # Not satisfied, not installed, not a release version, or not ours to say.
    assert fast.run_satisfied_install(["simple>2"]) is None
    assert fast.run_satisfied_install(["simple<2"]) is None
    assert fast.run_satisfied_install(["missing"]) is None
    _dist(root, "pre-1.0rc1.dist-info", "pre", "1.0rc1")
    assert fast.run_satisfied_install(["pre"]) is None
    monkeypatch.setenv("CPIP_TARGET_PREFIX", "/elsewhere")
    assert fast.run_satisfied_install(["simple"]) is None
    monkeypatch.delenv("CPIP_TARGET_PREFIX")
    Config.find_links = ["/configured"]
    assert fast.run_satisfied_install(["simple"]) is None
    assert capsys.readouterr().out == ""
