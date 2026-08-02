from __future__ import annotations

import sys
from pathlib import Path

from cpip_benchmark.cli import BENCHMARKS, build_commands
from cpip_benchmark.hyperfine import Command, Hyperfine
from cpip_benchmark.workloads import workload_manifest


def test_builds_all_offline_commands(tmp_path: Path) -> None:
    for benchmark in BENCHMARKS:
        commands = build_commands(
            benchmark,
            workload="offline",
            workspace=tmp_path / benchmark,
            cpip_python=sys.executable,
            cpip_console=None,
            cpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
        assert {command.name.split()[0] for command in commands} == {"cpip", "uv"}
        assert all(command.command for command in commands)
        assert all(command.command[:3] == [sys.executable, "-m", "cpip_benchmark.runner"] for command in commands)


def test_generated_fragments_are_not_posix_specific(tmp_path: Path) -> None:
    forbidden = ("rm -rf", "mkdir -p")
    for benchmark in BENCHMARKS:
        commands = build_commands(
            benchmark,
            workload="offline",
            workspace=tmp_path / benchmark,
            cpip_python=sys.executable,
            cpip_console=None,
            cpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
        for command in commands:
            fragments = [command.prepare or "", " ".join(command.command)]
            assert not any(token in fragment for token in forbidden for fragment in fragments)


def test_direct_launcher_uses_generated_wrapper(tmp_path: Path) -> None:
    commands = build_commands(
        "startup-help",
        workload="offline",
        workspace=tmp_path,
        cpip_python=sys.executable,
        cpip_console=None,
        cpip_launcher="direct",
        uv_path="uv",
        python=sys.executable,
    )

    cpip = commands[0].command
    assert str(tmp_path / "cpip-direct.py") in cpip
    assert (tmp_path / "cpip-direct.py").read_text(encoding="utf-8").startswith(
        "from __future__ import annotations\nfrom cpip.cli.entrypoint import main\n",
    )


def test_hyperfine_dry_run_contains_prepare_and_names() -> None:
    run = Hyperfine(
        name="example",
        commands=[
            Command(
                "cpip (example)",
                "rm -rf target",
                ["python", "-m", "cpip", "--help"],
            ),
        ],
        setup=None,
        warmup=0,
        min_runs=None,
        runs=1,
        verbose=False,
        json=True,
        ignore_failure=False,
    )

    args = run.args()
    assert args[:3] == ["hyperfine", "--export-json", "example.json"]
    assert "--prepare" in args
    assert "cpip (example)" in args


def test_offline_workload_contains_installable_wheels(tmp_path: Path) -> None:
    manifest = workload_manifest(tmp_path, workload="offline")
    wheelhouse = Path(manifest["wheelhouse"])
    requirements = Path(manifest["source_requirements"])

    assert requirements.read_text(encoding="utf-8").strip() == "application"
    assert (wheelhouse / "application-1.0.0-py3-none-any.whl").is_file()
    assert len(list(wheelhouse.glob("*.whl"))) > 20


def test_live_workload_writes_trio_files(tmp_path: Path) -> None:
    manifest = workload_manifest(tmp_path, workload="live")

    assert "sphinx" in Path(manifest["source_requirements"]).read_text(encoding="utf-8")
    assert "sphinx==" in Path(manifest["install_requirements"]).read_text(encoding="utf-8")
