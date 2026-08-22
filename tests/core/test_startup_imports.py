from __future__ import annotations

import json
import sys
from pathlib import Path

from import_harness import ROOT, baseline_modules, imported_modules, run_cpip

if sys.version_info >= (3, 11):
    import tomllib
else:
    from cpip._vendor import tomli as tomllib


PACKAGES = ROOT / "tests" / "cli" / "data" / "packages"
SIMPLEWHEEL = PACKAGES / "simplewheel-2.0-py2.py3-none-any.whl"


def test_literal_version_matches_project_metadata() -> None:
    import cpip

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert cpip.__version__ == project["project"]["version"]


def test_top_level_help_exits_zero_and_prints_usage() -> None:
    result = run_cpip(["--help"])

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "cpip" in result.stdout


def test_unknown_command_errors() -> None:
    result = run_cpip(["definitely-not-a-command"])

    assert result.returncode == 1
    assert "Unknown command" in result.stderr


def test_version_prints_package_location() -> None:
    result = run_cpip(["--version"])

    assert result.returncode == 0
    assert result.stdout.startswith("cpip ")


def test_fast_list_empty_json_output(tmp_path: Path) -> None:
    result = run_cpip(["list", "--format=json", "--path", str(tmp_path)], cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout == "[]\n"


def test_fast_list_reads_simple_dist_info(tmp_path: Path) -> None:
    dist_info = tmp_path / "demo_pkg-1.2.dist-info"
    dist_info.mkdir()
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )

    result = run_cpip(["list", "--format=json", "--path", str(tmp_path)], cwd=tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout) == [{"name": "demo-pkg", "version": "1.2"}]


def test_fast_lock_produces_output_on_cache_hit(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    output = tmp_path / "pylock.toml"
    args = [
        "lock",
        "--quiet",
        "--no-index",
        "--find-links",
        str(SIMPLEWHEEL),
        "--output",
        str(output),
        "simplewheel==2.0",
    ]
    env = {"CPIP_CACHE_DIR": str(cache_dir)}

    first = run_cpip(args, cwd=tmp_path, env=env)
    second = run_cpip(args, cwd=tmp_path, env=env)

    assert first.returncode == 0
    assert second.returncode == 0
    assert output.is_file()


# Modules the local fast install path must not import: each is several
# milliseconds of interpreter work the path never uses, and together they
# were a third of its startup cost.
FAST_INSTALL_FORBIDDEN = frozenset(
    {
        "dataclasses",
        "email.parser",
        "importlib.resources",
        "inspect",
        "logging",
        "tempfile",
        "json",
        "sqlite3",
        "zipfile",
        "cpip.resolution.models",
        "cpip.resolution.api",
        "cpip.index.provider",
        "cpip.cli.install",
        "cpip.cli.requirements",
    },
)


def test_fast_local_install_stays_import_light(tmp_path: Path) -> None:
    import shutil

    # A wheelhouse holding only wheels: the narrow resolver declines a
    # directory with anything else in it, and then the normal path would run.
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    target = tmp_path / "target"
    args = [
        "install",
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--target",
        str(target),
        "--find-links",
        str(wheelhouse),
        "simplewheel==2.0",
    ]
    env = {"CPIP_CACHE_DIR": str(tmp_path / "cache")}

    modules = imported_modules(args, cwd=tmp_path, env=env)

    assert next(target.glob("simplewheel-2.0.dist-info"), None) is not None
    assert "cpip.cli.fast_install" in modules
    assert not (modules & FAST_INSTALL_FORBIDDEN), sorted(
        modules & FAST_INSTALL_FORBIDDEN
    )


def test_default_install_scans_installed_state_without_importlib_metadata(
    tmp_path: Path,
) -> None:
    """Without --ignore-installed the install lists the target's (and the
    interpreter's) dist-info directories itself; importlib.metadata is only
    for roots and finders a directory listing cannot answer."""
    import shutil

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    target = tmp_path / "target"
    args = [
        "install",
        "--no-index",
        "--target",
        str(target),
        "--find-links",
        str(wheelhouse),
        "simplewheel==2.0",
    ]
    env = {"CPIP_CACHE_DIR": str(tmp_path / "cache")}

    first = imported_modules(args, cwd=tmp_path, env=env) - baseline_modules()
    assert next(target.glob("simplewheel-2.0.dist-info"), None) is not None
    # The second run finds simplewheel already installed in the target.
    second = imported_modules(args, cwd=tmp_path, env=env) - baseline_modules()

    for modules in (first, second):
        assert "cpip.cli.install" in modules
        assert "importlib.metadata" not in modules


SATISFIED_INSTALL_FORBIDDEN = frozenset(
    {
        "cpip.cli.install",
        "cpip.cli.fast_install",
        "cpip.cli.requirements",
        "cpip.index.provider",
        "cpip.resolution.api",
        "logging",
        "argparse",
        "sqlite3",
        "json",
    },
)


def test_already_satisfied_install_answers_before_startup(tmp_path: Path) -> None:
    """``cpip install <name>`` for names already installed in a release
    version is answered by the pre-startup recognizer: the same lines the
    normal path prints, without loading it."""
    import os
    import shutil

    from import_harness import SRC, import_snapshot

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    site = tmp_path / "site"
    imported_modules(
        [
            "install",
            "-q",
            "--no-index",
            "--target",
            str(site),
            "--find-links",
            str(wheelhouse),
            "simplewheel==2.0",
        ],
        cwd=tmp_path,
        env={"CPIP_CACHE_DIR": str(tmp_path / "cache")},
    )
    assert next(site.glob("simplewheel-2.0.dist-info"), None) is not None

    env = {
        "CPIP_CACHE_DIR": str(tmp_path / "cache"),
        "PYTHONPATH": f"{site}{os.pathsep}{SRC}",
    }
    snapshot = import_snapshot(
        ["install", "--find-links", str(wheelhouse), "simplewheel", "simplewheel>=1"],
        cwd=tmp_path,
        env=env,
    )
    assert snapshot.returncode == 0, snapshot.describe()
    assert snapshot.stdout == (
        f"Looking in links: {wheelhouse}\n"
        "Requirement already satisfied: simplewheel\n"
        "Requirement already satisfied: simplewheel>=1\n"
    ), snapshot.describe()
    assert not (set(snapshot.modules) & SATISFIED_INSTALL_FORBIDDEN), sorted(
        set(snapshot.modules) & SATISFIED_INSTALL_FORBIDDEN
    )

    # A specifier the installed version does not meet is the normal path's.
    snapshot = import_snapshot(
        ["install", "--no-index", "--find-links", str(wheelhouse), "simplewheel>=3"],
        cwd=tmp_path,
        env=env,
    )
    assert "Could not find a version that satisfies" in snapshot.stderr, (
        snapshot.describe()
    )
    assert "cpip.cli.install" in snapshot.modules


# Modules a local wheelhouse install on the normal path (no --no-compile, so
# the fast path declines) must not import: each serves a shape this install
# does not have -- installed-state scans, HTML index pages, --group files,
# source builds -- and they cost ~20 ms together.
NORMAL_INSTALL_FORBIDDEN = frozenset(
    {
        "dataclasses",
        "importlib.metadata",
        "cpip.resolution.files.parser",
        "cpip.vcs.versioncontrol",
        "cpip.core.subprocess",
        "html.parser",
        "tomllib",
        "cpip._vendor.tomli",
        "cpip.build.build_backend",
        "email.message",
        "configparser",
        # Not subprocess: cpip's own wrappers defer it, but the stdlib's
        # platform module imports it on Python 3.10.
    },
)


def test_normal_local_install_stays_import_light(tmp_path: Path) -> None:
    import shutil

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    target = tmp_path / "target"
    args = [
        "install",
        "--no-index",
        "--ignore-installed",
        "--target",
        str(target),
        "--find-links",
        str(wheelhouse),
        "simplewheel==2.0",
    ]
    env = {"CPIP_CACHE_DIR": str(tmp_path / "cache")}

    modules = imported_modules(args, cwd=tmp_path, env=env) - baseline_modules()

    assert next(target.glob("simplewheel-2.0.dist-info"), None) is not None
    assert "cpip.cli.install" in modules
    assert not (modules & NORMAL_INSTALL_FORBIDDEN), sorted(
        modules & NORMAL_INSTALL_FORBIDDEN
    )


# The list fast path reads dist-info directories and prints; it must not
# import the typing machinery, the install fast path or the packaging core.
FAST_LIST_FORBIDDEN = frozenset(
    {
        "typing",
        "cpip.cli.fast_install",
        "cpip.core.packaging",
        "cpip.cli.list",
        "cpip.build.query",
        "cpip.core.metadata",
        "argparse",
    }
)


def test_plain_freeze_stays_import_light(tmp_path: Path) -> None:
    """``cpip freeze`` with no options is the fast path over sys.path."""
    import os

    from import_harness import SRC, import_snapshot

    site = tmp_path / "site"
    dist_info = site / "demo_pkg-1.2.dist-info"
    dist_info.mkdir(parents=True)
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": f"{site}{os.pathsep}{SRC}"}

    # --exclude-editable: the harness's own environment may hold editables.
    snapshot = import_snapshot(["freeze", "--exclude-editable"], cwd=tmp_path, env=env)
    assert "demo-pkg==1.2\n" in snapshot.stdout, snapshot.describe()
    forbidden = FAST_LIST_FORBIDDEN | {
        "cpip.cli.freeze",
        "cpip.core.light_metadata",
        "logging",
    }
    assert not (set(snapshot.modules) & forbidden), sorted(
        set(snapshot.modules) & forbidden
    )


def test_plain_list_stays_import_light(tmp_path: Path) -> None:
    """``cpip list`` with no options is the fast path over sys.path."""
    import os

    from import_harness import SRC, import_snapshot

    site = tmp_path / "site"
    dist_info = site / "demo_pkg-1.2.dist-info"
    dist_info.mkdir(parents=True)
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": f"{site}{os.pathsep}{SRC}"}

    snapshot = import_snapshot(["list"], cwd=tmp_path, env=env)
    import re

    assert re.search(r"^demo-pkg +1\.2$", snapshot.stdout, re.M), snapshot.describe()
    assert not (set(snapshot.modules) & FAST_LIST_FORBIDDEN), sorted(
        set(snapshot.modules) & FAST_LIST_FORBIDDEN
    )


def test_fast_list_stays_import_light(tmp_path: Path) -> None:
    dist_info = tmp_path / "demo_pkg-1.2.dist-info"
    dist_info.mkdir()
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )

    modules = (
        imported_modules(
            ["list", "--format=json", "--path", str(tmp_path)],
            cwd=tmp_path,
        )
        - baseline_modules()
    )

    assert "cpip.cli.fast" in modules
    assert not (modules & FAST_LIST_FORBIDDEN), sorted(modules & FAST_LIST_FORBIDDEN)
