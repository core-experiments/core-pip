from __future__ import annotations

import sys
from pathlib import Path

from cpip_benchmark.cli import (
    BENCHMARKS,
    build_commands,
    default_benchmarks,
    expand_workloads,
)
from cpip_benchmark.hyperfine import Command, Hyperfine
from cpip_benchmark.workloads import (
    OFFICIAL_WORKLOAD_NAMES,
    OFFICIAL_WORKLOADS,
    fixture_root,
    workload_manifest,
)


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
        assert all(
            command.command[:3] == [sys.executable, "-m", "cpip_benchmark.runner"]
            for command in commands
        )


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
            assert not any(
                token in fragment for token in forbidden for fragment in fragments
            )


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
    assert (
        (tmp_path / "cpip-direct.py")
        .read_text(encoding="utf-8")
        .startswith(
            "from __future__ import annotations\nfrom cpip.cli.entrypoint import main\n",
        )
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
    assert (
        Path(manifest["incremental_wheelhouse"])
        / "incremental_application-2.0.0-py3-none-any.whl"
    ).is_file()
    assert len(list(wheelhouse.glob("*.whl"))) > 20


def test_incremental_install_restores_old_target_before_each_run(
    tmp_path: Path,
) -> None:
    commands = build_commands(
        "install-incremental-warm",
        workload="offline",
        workspace=tmp_path,
        cpip_python=sys.executable,
        cpip_console=None,
        cpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    assert all(command.prepare is not None for command in commands)
    assert all(
        "incremental-base.txt" in (command.prepare or "") for command in commands
    )
    assert all(
        "incremental-update.txt" in " ".join(command.command) for command in commands
    )
    assert all("--upgrade" in command.command for command in commands)


def test_trio_workload_writes_official_files(tmp_path: Path) -> None:
    manifest = workload_manifest(tmp_path, workload="trio")

    assert "sphinx" in Path(manifest["source_requirements"]).read_text(encoding="utf-8")
    assert "sphinx==" in Path(manifest["install_requirements"]).read_text(
        encoding="utf-8"
    )


def test_trio_install_uses_the_prepared_cpip_cache(tmp_path: Path) -> None:
    commands = build_commands(
        "install-warm",
        workload="trio",
        workspace=tmp_path,
        cpip_python=sys.executable,
        cpip_console=None,
        cpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    cpip = commands[0].command
    cache_option = cpip.index("--cache-dir")
    assert cpip[cache_option + 1] == str(tmp_path / "cache" / "cpip")


def test_every_official_workload_builds_lock_commands(tmp_path: Path) -> None:
    for workload in OFFICIAL_WORKLOADS:
        commands = build_commands(
            "lock-cold",
            workload=workload.name,
            workspace=tmp_path / workload.name,
            cpip_python=sys.executable,
            cpip_console=None,
            cpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )

        assert {command.name.split()[0] for command in commands} == {"cpip", "uv"}
        assert all(command.command for command in commands)


def test_every_compiled_official_workload_builds_install_commands(
    tmp_path: Path,
) -> None:
    compiled = [workload for workload in OFFICIAL_WORKLOADS if workload.compiled]
    assert len(compiled) == 9

    for workload in compiled:
        commands = build_commands(
            "install-warm",
            workload=workload.name,
            workspace=tmp_path / workload.name,
            cpip_python=sys.executable,
            cpip_console=None,
            cpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )

        assert all("compiled" in " ".join(command.command) for command in commands)


def test_source_only_workload_rejects_install_benchmark(tmp_path: Path) -> None:
    try:
        build_commands(
            "install-cold",
            workload="home-assistant",
            workspace=tmp_path,
            cpip_python=sys.executable,
            cpip_console=None,
            cpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
    except ValueError as error:
        assert "no official compiled install workload" in str(error)
    else:
        raise AssertionError("source-only workload accepted install benchmark")


def test_airflow2_applies_constraints_to_both_resolvers(tmp_path: Path) -> None:
    commands = build_commands(
        "lock-cold",
        workload="airflow2",
        workspace=tmp_path,
        cpip_python=sys.executable,
        cpip_console=None,
        cpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    for command in commands:
        constraint = command.command.index("--constraint")
        assert command.command[constraint + 1].endswith("airflow2-constraints.txt")


def test_transformers_project_uses_native_project_inputs(tmp_path: Path) -> None:
    commands = build_commands(
        "lock-cold",
        workload="transformers-project",
        workspace=tmp_path,
        cpip_python=sys.executable,
        cpip_console=None,
        cpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    cpip, uv = commands
    assert any(argument.endswith("/transformers") for argument in cpip.command)
    assert any(
        argument.endswith("/transformers/pyproject.toml") for argument in uv.command
    )


def test_official_defaults_match_available_fixtures() -> None:
    assert default_benchmarks("home-assistant") == ("lock-cold", "lock-warm")
    assert default_benchmarks("jupyter") == (
        "lock-cold",
        "lock-warm",
        "install-cold",
        "install-warm",
    )


def test_live_expands_to_every_official_workload() -> None:
    assert expand_workloads("live") == OFFICIAL_WORKLOAD_NAMES
    assert expand_workloads("trio") == ("trio",)


def test_registry_covers_every_official_fixture() -> None:
    root = fixture_root()
    source_fixtures = {str(path.relative_to(root)) for path in root.rglob("*.in")} | {
        "transformers/pyproject.toml"
    }
    compiled_fixtures = {
        str(path.relative_to(root)) for path in (root / "compiled").glob("*.txt")
    }

    assert {workload.source for workload in OFFICIAL_WORKLOADS} == source_fixtures
    assert {
        workload.compiled for workload in OFFICIAL_WORKLOADS if workload.compiled
    } == compiled_fixtures
    assert {
        workload.constraint for workload in OFFICIAL_WORKLOADS if workload.constraint
    } == {"airflow2-constraints.txt"}
