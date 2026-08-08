from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    from cpip._vendor import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PACKAGES = ROOT / "tests" / "cli" / "data" / "packages"
SIMPLEWHEEL = PACKAGES / "simplewheel-2.0-py2.py3-none-any.whl"
MARKER = "@@CPIP_IMPORTED_MODULES@@"

SCRIPT = """
from __future__ import annotations

import json
import runpy
import sys

sys.argv = ["cpip", *json.loads(sys.argv[1])]
try:
    runpy.run_module("cpip", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
print("@@CPIP_IMPORTED_MODULES@@" + json.dumps(sorted(sys.modules)))
"""

DIRECT_SCRIPT = """
from __future__ import annotations

import json
import sys

sys.argv = ["cpip", *json.loads(sys.argv[1])]
from cpip.cli.entrypoint import main

try:
    main()
except SystemExit:
    pass
print("@@CPIP_IMPORTED_MODULES@@" + json.dumps(sorted(sys.modules)))
"""


def imported_modules(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    direct: bool = False,
) -> set[str]:
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(SRC)
    process_env.pop("CPIP_QUIET", None)
    if env:
        process_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", DIRECT_SCRIPT if direct else SCRIPT, json.dumps(args)],
        cwd=cwd or ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout + result.stderr
    marker_line = next(
        line for line in reversed(output.splitlines()) if line.startswith(MARKER)
    )
    return set(json.loads(marker_line.removeprefix(MARKER)))


def run_cpip(
    args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(SRC)
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "cpip", *args],
        cwd=cwd or ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )


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


# --- Design guard: lazy startup imports were removed ---------------------------------
# The command entrypoint now imports the command registry eagerly at module load so
# that ``cpip --help`` and other top-level routes resolve through a single, flat
# import graph instead of deferring registration. This test pins that decision so
# the lazy-import path is not accidentally reintroduced.


def test_entrypoint_eagerly_loads_command_registry() -> None:
    modules = imported_modules(["--help"])

    assert "cpip.cli.registry" in modules


def test_direct_launcher_eagerly_loads_command_registry() -> None:
    modules = imported_modules(["--help"], direct=True)

    assert "cpip.cli.registry" in modules
