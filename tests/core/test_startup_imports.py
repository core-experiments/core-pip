from __future__ import annotations

import json
import sys
from pathlib import Path

from import_harness import ROOT, run_cpip

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
