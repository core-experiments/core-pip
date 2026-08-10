from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cpip_benchmark.hyperfine import Command, Hyperfine, env_prefix
from cpip_benchmark.workloads import (
    OFFICIAL_WORKLOAD_NAMES,
    OFFICIAL_WORKLOADS,
    WORKLOAD_NAMES,
    official_workload,
    workload_manifest,
)

BENCHMARKS = (
    "startup-help",
    "startup-version",
    "startup-install-help",
    "startup-lock-help",
    "startup-list-help",
    "startup-invalid-command",
    "startup-list-empty",
    "startup-fast-lock",
    "startup-fast-install",
    "lock-cold",
    "lock-warm",
    "install-cold",
    "install-warm",
    "install-incremental-warm",
)

OFFICIAL_LOCK_BENCHMARKS = ("lock-cold", "lock-warm")
OFFICIAL_INSTALL_BENCHMARKS = ("install-cold", "install-warm")


def expand_workloads(selector: str) -> tuple[str, ...]:
    if selector == "live":
        return OFFICIAL_WORKLOAD_NAMES
    return (selector,)


def default_benchmarks(workload: str) -> tuple[str, ...]:
    if workload == "offline":
        return BENCHMARKS
    definition = official_workload(workload)
    if definition is None:
        raise ValueError(f"Unknown workload: {workload}")
    if definition.compiled is None:
        return OFFICIAL_LOCK_BENCHMARKS
    return (*OFFICIAL_LOCK_BENCHMARKS, *OFFICIAL_INSTALL_BENCHMARKS)


def supports_benchmark(workload: str, benchmark: str) -> bool:
    if workload == "offline":
        return True
    if benchmark.startswith("startup-fast-"):
        return False
    if not benchmark.startswith("install-"):
        return True
    if benchmark == "install-incremental-warm":
        return True
    definition = official_workload(workload)
    return definition is not None and definition.compiled is not None


def print_workloads() -> None:
    print("offline\tlock + install\tGenerated local wheelhouse (no network)")
    print(
        f"live\tsuite\tAll {len(OFFICIAL_WORKLOADS)} official uv workloads "
        "(install where compiled)",
    )
    for workload in OFFICIAL_WORKLOADS:
        capabilities = "lock + install" if workload.compiled else "lock"
        python = f"; Python {workload.python}" if workload.python else ""
        print(
            f"{workload.name}\t{capabilities}\t{workload.description}{python}",
        )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def tool_command(
    command: list[str], *, env: dict[str, str] | None = None
) -> tuple[list[str], dict[str, str]]:
    """Attach ``env`` to ``command`` for hyperfine to run.

    On POSIX this is a plain ``(command, env)`` pair: ``Hyperfine.args()``
    renders ``env`` as a shell assignment prefix, so no extra process is
    spawned just to set variables. Windows' shell doesn't support that
    syntax, so there we fall back to wrapping the command through
    ``cpip_benchmark.runner run --env ...``, which does the assignment
    in-process before exec'ing the real command.
    """
    if not env:
        return command, {}
    if os.name == "nt":
        wrapped = [sys.executable, "-m", "cpip_benchmark.runner", "run"]
        for name, value in env.items():
            wrapped.extend(["--env", f"{name}={value}"])
        wrapped.extend(command)
        return wrapped, {}
    return command, env


def cpip_direct_launcher(workspace: Path) -> Path:
    launcher = workspace / "cpip-direct.py"
    if not launcher.exists():
        launcher.write_text(
            "from __future__ import annotations\n"
            "from cpip.cli.entrypoint import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
    return launcher


def cpip_command(
    args: list[str],
    *,
    workspace: Path,
    cpip_python: str,
    cpip_console: str | None,
    cpip_launcher: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    if cpip_console is not None:
        command = [cpip_console, *args]
    elif cpip_launcher == "direct":
        command = [cpip_python, str(cpip_direct_launcher(workspace)), *args]
    else:
        command = [cpip_python, "-m", "cpip", *args]
    env = {"PYTHONPATH": str(repo_root() / "src")}
    env.update(extra_env or {})
    return tool_command(command, env=env)


def uv_command(uv_path: str, args: list[str]) -> list[str]:
    return [uv_path, *args]


def cleanup_command(paths: list[Path], *, mkdir: list[Path] | None = None) -> str:
    command = [sys.executable, "-m", "cpip_benchmark.runner", "cleanup"]
    for path in paths:
        command.extend(["--path", str(path)])
    for path in mkdir or []:
        command.extend(["--mkdir", str(path)])
    return shlex.join(command)


def prepare_with_cache(
    cwd: Path, *, cache: Path, outputs: list[Path], cold: bool
) -> str:
    paths = []
    if cold:
        paths.append(cache)
    paths.extend(outputs)
    return cleanup_command(paths, mkdir=[cwd])


def shell_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def warm_setup(commands: list[Command], cleanup: list[Path]) -> str:
    parts = [cleanup_command(cleanup)]
    parts.extend(
        env_prefix(command.env) + shell_command(command.command) for command in commands
    )
    return " && ".join(parts)


def build_commands(
    benchmark: str,
    *,
    workload: str,
    workspace: Path,
    cpip_python: str,
    cpip_console: str | None,
    cpip_launcher: str,
    uv_path: str,
    python: str,
) -> list[Command]:
    manifest = workload_manifest(workspace / "workload", workload=workload)
    workload_name = manifest["workload"]
    cpip_cache = workspace / "cache" / "cpip"
    uv_cache = workspace / "cache" / "uv"
    cpip_output = workspace / "cpip.out"
    uv_output = workspace / "uv.out"
    cpip_target = workspace / "target" / "cpip"
    uv_target = workspace / "target" / "uv"
    wheelhouse = manifest.get("wheelhouse")
    source_requirements = manifest["source_requirements"]
    cpip_source = manifest.get("cpip_source", source_requirements)
    source_kind = manifest.get("source_kind", "requirements")
    constraint_requirements = manifest.get("constraint_requirements")
    install_requirements = manifest.get("install_requirements")
    incremental_wheelhouse = manifest["incremental_wheelhouse"]
    incremental_base = manifest["incremental_base_requirements"]
    incremental_update = manifest["incremental_update_requirements"]

    def cpip(
        args: list[str], *, extra_env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        return cpip_command(
            args,
            workspace=workspace,
            cpip_python=cpip_python,
            cpip_console=cpip_console,
            cpip_launcher=cpip_launcher,
            extra_env=extra_env,
        )

    def label(tool: str) -> str:
        return f"{tool} ({workload_name}/{benchmark})"

    def cpip_step(
        prepare: str | None, args: list[str], *, extra_env: dict[str, str] | None = None
    ) -> Command:
        command, env = cpip(args, extra_env=extra_env)
        return Command(label("cpip"), prepare, command, env)

    def uv_step(prepare: str | None, args: list[str]) -> Command:
        return Command(label("uv"), prepare, uv_command(uv_path, args))

    if benchmark == "startup-help":
        return [
            cpip_step(None, ["--help"]),
            uv_step(None, ["--help"]),
        ]
    if benchmark == "startup-version":
        return [
            cpip_step(None, ["--version"]),
            uv_step(None, ["--version"]),
        ]
    if benchmark == "startup-install-help":
        return [
            cpip_step(None, ["install", "--help"]),
            uv_step(None, ["pip", "install", "--help"]),
        ]
    if benchmark == "startup-lock-help":
        return [
            cpip_step(None, ["lock", "--help"]),
            uv_step(None, ["pip", "compile", "--help"]),
        ]
    if benchmark == "startup-list-help":
        return [
            cpip_step(None, ["list", "--help"]),
            uv_step(None, ["pip", "list", "--help"]),
        ]
    if benchmark == "startup-invalid-command":
        return [
            cpip_step(None, ["definitely-not-a-command"]),
            uv_step(None, ["definitely-not-a-command"]),
        ]
    if benchmark == "startup-list-empty":
        return [
            cpip_step(
                cleanup_command([cpip_target], mkdir=[cpip_target]),
                ["list", "--format=json", "--path", str(cpip_target)],
            ),
            uv_step(
                cleanup_command([uv_target], mkdir=[uv_target]),
                ["pip", "list", "--format=json", "--target", str(uv_target)],
            ),
        ]
    if benchmark == "startup-fast-lock":
        # Isolates per-invocation overhead (process start, arg parsing,
        # provider setup) from graph-resolution cost: a single dependency-free
        # package against an already-warm cache, versus lock-warm's full
        # offline graph. If this doesn't scale with lock-warm as the graph
        # grows, the difference is resolution work, not fixed overhead.
        if wheelhouse is None:
            raise ValueError("startup-fast-lock requires the offline workload")
        trivial_requirements = workspace / "trivial.in"
        trivial_requirements.write_text("leaf-0\n", encoding="utf-8")
        cpip_args = [
            "lock",
            "--quiet",
            "--no-index",
            "--find-links",
            wheelhouse or "",
            "--output",
            str(cpip_output),
            "-r",
            str(trivial_requirements),
        ]
        uv_args = [
            "pip",
            "compile",
            str(trivial_requirements),
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--output-file",
            str(uv_output),
            "--python",
            python,
        ]
        uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            cpip_step(
                cleanup_command([cpip_output]),
                cpip_args,
                extra_env={"CPIP_CACHE_DIR": str(cpip_cache)},
            ),
            uv_step(cleanup_command([uv_output]), uv_args),
        ]
    if benchmark == "startup-fast-install":
        # Same isolation as startup-fast-lock, for the install path: one
        # dependency-free wheel against an already-warm cache, versus
        # install-warm's full offline requirement set.
        if wheelhouse is None:
            raise ValueError("startup-fast-install requires the offline workload")
        trivial_install = workspace / "trivial-install.txt"
        trivial_install.write_text("leaf-0==1.1.0\n", encoding="utf-8")
        cpip_args = [
            "install",
            "--quiet",
            "--ignore-installed",
            "--no-compile",
            "--cache-dir",
            str(cpip_cache),
            "--target",
            str(cpip_target),
            "-r",
            str(trivial_install),
        ]
        uv_args = [
            "pip",
            "install",
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--target",
            str(uv_target),
            "--python",
            python,
            "-r",
            str(trivial_install),
        ]
        cpip_args.extend(["--no-index", "--find-links", wheelhouse])
        uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            cpip_step(cleanup_command([cpip_target]), cpip_args),
            uv_step(cleanup_command([uv_target]), uv_args),
        ]

    if benchmark.startswith("lock-"):
        cold = benchmark.endswith("cold")
        cpip_prepare = prepare_with_cache(
            workspace,
            cache=cpip_cache,
            outputs=[cpip_output],
            cold=cold,
        )
        uv_prepare = prepare_with_cache(
            workspace,
            cache=uv_cache,
            outputs=[uv_output],
            cold=cold,
        )
        cpip_args = ["lock", "--quiet", "--output", str(cpip_output)]
        uv_args = [
            "pip",
            "compile",
            source_requirements,
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--output-file",
            str(uv_output),
            "--python",
            python,
        ]
        if source_kind == "project":
            if wheelhouse is not None:
                raise ValueError("project workloads cannot use the offline wheelhouse")
            cpip_args.append(cpip_source)
        elif wheelhouse is None:
            cpip_args.extend(["-r", cpip_source])
        else:
            cpip_args.extend(
                ["--no-index", "--find-links", wheelhouse, "-r", cpip_source]
            )
            uv_args.extend(["--no-index", "--find-links", wheelhouse])
        if constraint_requirements is not None:
            cpip_args.extend(["--constraint", constraint_requirements])
            uv_args.extend(["--constraint", constraint_requirements])
        return [
            cpip_step(
                cpip_prepare,
                cpip_args,
                extra_env={"CPIP_CACHE_DIR": str(cpip_cache)},
            ),
            uv_step(uv_prepare, uv_args),
        ]

    if benchmark == "install-incremental-warm":
        cpip_common = [
            "--quiet",
            "--no-compile",
            "--cache-dir",
            str(cpip_cache),
            "--target",
            str(cpip_target),
            "--no-index",
            "--find-links",
            incremental_wheelhouse,
        ]
        uv_common = [
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--target",
            str(uv_target),
            "--python",
            python,
            "--no-index",
            "--find-links",
            incremental_wheelhouse,
        ]
        cpip_base, cpip_base_env = cpip(
            [
                "install",
                "--ignore-installed",
                *cpip_common,
                "-r",
                incremental_base,
            ],
        )
        uv_base = uv_command(
            uv_path,
            ["pip", "install", *uv_common, "-r", incremental_base],
        )
        cpip_update, cpip_update_env = cpip(
            ["install", "--upgrade", *cpip_common, "-r", incremental_update],
        )
        uv_update = uv_command(
            uv_path,
            [
                "pip",
                "install",
                "--upgrade",
                *uv_common,
                "-r",
                incremental_update,
            ],
        )
        cpip_prepare = " && ".join(
            (
                cleanup_command([cpip_target]),
                env_prefix(cpip_base_env) + shell_command(cpip_base),
            ),
        )
        uv_prepare = " && ".join(
            (
                cleanup_command([uv_target]),
                shell_command(uv_base),
            ),
        )
        return [
            Command(label("cpip"), cpip_prepare, cpip_update, cpip_update_env),
            Command(label("uv"), uv_prepare, uv_update),
        ]

    if benchmark.startswith("install-"):
        if install_requirements is None:
            raise ValueError(
                f"{workload_name} has no official compiled install workload; "
                "run a lock benchmark instead",
            )
        cold = benchmark.endswith("cold")
        cpip_prepare = prepare_with_cache(
            workspace,
            cache=cpip_cache,
            outputs=[cpip_target],
            cold=cold,
        )
        uv_prepare = prepare_with_cache(
            workspace,
            cache=uv_cache,
            outputs=[uv_target],
            cold=cold,
        )
        cpip_args = [
            "install",
            "--quiet",
            "--ignore-installed",
            "--no-compile",
            "--cache-dir",
            str(cpip_cache),
            "--target",
            str(cpip_target),
            "-r",
            install_requirements,
        ]
        uv_args = [
            "pip",
            "install",
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--target",
            str(uv_target),
            "--python",
            python,
            "-r",
            install_requirements,
        ]
        if wheelhouse is not None:
            cpip_args.extend(
                [
                    "--no-index",
                    "--find-links",
                    wheelhouse,
                ]
            )
            uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            cpip_step(cpip_prepare, cpip_args),
            uv_step(uv_prepare, uv_args),
        ]

    raise ValueError(f"Unknown benchmark: {benchmark}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark cpip against uv with hyperfine."
    )
    parser.add_argument("--benchmark", "-b", choices=BENCHMARKS, action="append")
    parser.add_argument(
        "--workload",
        choices=WORKLOAD_NAMES,
        default="offline",
        help="Workload to run; 'live' expands to every official uv workload",
    )
    parser.add_argument(
        "--list-workloads",
        action="store_true",
        help="List workload capabilities and exit",
    )
    parser.add_argument("--cpip-python", default=sys.executable)
    parser.add_argument("--cpip-console")
    parser.add_argument(
        "--cpip-launcher", choices=("module", "direct"), default="module"
    )
    parser.add_argument("--uv-path", default=shutil.which("uv") or "uv")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--min-runs", type=int)
    parser.add_argument("--runs", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    if args.list_workloads:
        print_workloads()
        return

    min_runs = args.min_runs
    if args.runs is None and min_runs is None:
        min_runs = 10

    workloads = expand_workloads(args.workload)
    runs: list[tuple[str, str]] = []
    for workload in workloads:
        benchmarks = tuple(args.benchmark or default_benchmarks(workload))
        for benchmark in benchmarks:
            if supports_benchmark(workload, benchmark):
                runs.append((workload, benchmark))
            elif args.workload == "live":
                print(
                    f"Skipping {workload}/{benchmark}: unsupported by workload",
                    file=sys.stderr,
                )
            else:
                parser.error(f"{workload} does not support {benchmark}")
    if not runs:
        parser.error("No supported workload and benchmark combinations selected")

    with tempfile.TemporaryDirectory(prefix="cpip-bench-") as temporary:
        workspace_root = Path(temporary)
        for workload, benchmark in runs:
            workspace = workspace_root / workload / benchmark
            workspace.mkdir(parents=True)
            commands = build_commands(
                benchmark,
                workload=workload,
                workspace=workspace,
                cpip_python=args.cpip_python,
                cpip_console=args.cpip_console,
                cpip_launcher=args.cpip_launcher,
                uv_path=args.uv_path,
                python=args.python,
            )
            setup = None
            if benchmark.endswith("warm") or benchmark.startswith("startup-fast-"):
                setup = warm_setup(
                    commands,
                    [
                        workspace / "cache",
                        workspace / "target",
                        workspace / "cpip.out",
                        workspace / "uv.out",
                    ],
                )
            run = Hyperfine(
                name=(
                    benchmark if workload == "offline" else f"{workload}-{benchmark}"
                ),
                commands=commands,
                setup=setup,
                warmup=args.warmup,
                min_runs=min_runs,
                runs=args.runs,
                verbose=args.verbose,
                json=args.json,
                ignore_failure=benchmark == "startup-invalid-command",
            )
            if args.dry_run:
                print(shell_command(run.args()))
            else:
                run.run()
        if args.keep_workspace:
            kept = Path(os.getcwd()) / "cpip-benchmark-workspace"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(workspace_root, kept)
            print(f"Kept workspace at {kept}")


if __name__ == "__main__":
    main()
