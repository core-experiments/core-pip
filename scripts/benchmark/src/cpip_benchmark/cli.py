from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cpip_benchmark.hyperfine import Command, Hyperfine
from cpip_benchmark.workloads import workload_manifest

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
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def tool_command(command: list[str], *, env: dict[str, str] | None = None) -> list[str]:
    wrapped = [sys.executable, "-m", "cpip_benchmark.runner", "run"]
    for name, value in (env or {}).items():
        wrapped.extend(["--env", f"{name}={value}"])
    wrapped.extend(command)
    return wrapped


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
) -> list[str]:
    if cpip_console is not None:
        command = [cpip_console, *args]
    elif cpip_launcher == "direct":
        command = [cpip_python, str(cpip_direct_launcher(workspace)), *args]
    else:
        command = [cpip_python, "-m", "cpip", *args]
    return [
        *tool_command(command, env={"PYTHONPATH": str(repo_root() / "src")}),
    ]


def uv_command(uv_path: str, args: list[str]) -> list[str]:
    return tool_command([uv_path, *args])


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
    parts.extend(shell_command(command.command) for command in commands)
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
    cpip_cache = workspace / "cache" / "cpip"
    uv_cache = workspace / "cache" / "uv"
    cpip_output = workspace / "cpip.out"
    uv_output = workspace / "uv.out"
    cpip_target = workspace / "target" / "cpip"
    uv_target = workspace / "target" / "uv"
    wheelhouse = manifest.get("wheelhouse")
    source_requirements = manifest["source_requirements"]
    install_requirements = manifest["install_requirements"]

    def cpip(args: list[str]) -> list[str]:
        return cpip_command(
            args,
            workspace=workspace,
            cpip_python=cpip_python,
            cpip_console=cpip_console,
            cpip_launcher=cpip_launcher,
        )

    if benchmark == "startup-help":
        return [
            Command("cpip (startup-help)", None, cpip(["--help"])),
            Command("uv (startup-help)", None, uv_command(uv_path, ["--help"])),
        ]
    if benchmark == "startup-version":
        return [
            Command("cpip (startup-version)", None, cpip(["--version"])),
            Command("uv (startup-version)", None, uv_command(uv_path, ["--version"])),
        ]
    if benchmark == "startup-install-help":
        return [
            Command("cpip (startup-install-help)", None, cpip(["install", "--help"])),
            Command(
                "uv (startup-install-help)",
                None,
                uv_command(uv_path, ["pip", "install", "--help"]),
            ),
        ]
    if benchmark == "startup-lock-help":
        return [
            Command("cpip (startup-lock-help)", None, cpip(["lock", "--help"])),
            Command(
                "uv (startup-lock-help)",
                None,
                uv_command(uv_path, ["pip", "compile", "--help"]),
            ),
        ]
    if benchmark == "startup-list-help":
        return [
            Command("cpip (startup-list-help)", None, cpip(["list", "--help"])),
            Command(
                "uv (startup-list-help)",
                None,
                uv_command(uv_path, ["pip", "list", "--help"]),
            ),
        ]
    if benchmark == "startup-invalid-command":
        return [
            Command(
                "cpip (startup-invalid-command)",
                None,
                cpip(["definitely-not-a-command"]),
            ),
            Command(
                "uv (startup-invalid-command)",
                None,
                uv_command(uv_path, ["definitely-not-a-command"]),
            ),
        ]
    if benchmark == "startup-list-empty":
        return [
            Command(
                "cpip (startup-list-empty)",
                cleanup_command([cpip_target], mkdir=[cpip_target]),
                cpip(["list", "--format=json", "--path", str(cpip_target)]),
            ),
            Command(
                "uv (startup-list-empty)",
                cleanup_command([uv_target], mkdir=[uv_target]),
                uv_command(
                    uv_path,
                    ["pip", "list", "--format=json", "--target", str(uv_target)],
                ),
            ),
        ]
    if benchmark == "startup-fast-lock":
        if wheelhouse is None:
            raise ValueError("startup-fast-lock requires the offline workload")
        cpip_args = [
            "lock",
            "--quiet",
            "--no-index",
            "--find-links",
            wheelhouse or "",
            "--output",
            str(cpip_output),
            "-r",
            source_requirements,
        ]
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
        uv_args.extend(["--no-index", "--find-links", wheelhouse])
        cpip_run = cpip(cpip_args)
        cpip_run[4:4] = ["--env", f"CPIP_CACHE_DIR={cpip_cache}"]
        return [
            Command(
                "cpip (startup-fast-lock)", cleanup_command([cpip_output]), cpip_run
            ),
            Command(
                "uv (startup-fast-lock)",
                cleanup_command([uv_output]),
                uv_command(uv_path, uv_args),
            ),
        ]
    if benchmark == "startup-fast-install":
        if wheelhouse is None:
            raise ValueError("startup-fast-install requires the offline workload")
        cpip_args = [
            "install",
            "--quiet",
            "--ignore-installed",
            "--no-compile",
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
        cpip_args.extend(
            ["--no-index", "--find-links", wheelhouse, "--cache-dir", str(cpip_cache)]
        )
        uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            Command(
                "cpip (startup-fast-install)",
                cleanup_command([cpip_target]),
                cpip(cpip_args),
            ),
            Command(
                "uv (startup-fast-install)",
                cleanup_command([uv_target]),
                uv_command(uv_path, uv_args),
            ),
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
        if wheelhouse is None:
            cpip_args.extend(["-r", source_requirements])
        else:
            cpip_args.extend(
                ["--no-index", "--find-links", wheelhouse, "-r", source_requirements]
            )
            uv_args.extend(["--no-index", "--find-links", wheelhouse])
        cpip_run = cpip(cpip_args)
        cpip_run[4:4] = ["--env", f"CPIP_CACHE_DIR={cpip_cache}"]
        return [
            Command(f"cpip ({benchmark})", cpip_prepare, cpip_run),
            Command(f"uv ({benchmark})", uv_prepare, uv_command(uv_path, uv_args)),
        ]

    if benchmark.startswith("install-"):
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
                    "--cache-dir",
                    str(cpip_cache),
                ]
            )
            uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            Command(f"cpip ({benchmark})", cpip_prepare, cpip(cpip_args)),
            Command(f"uv ({benchmark})", uv_prepare, uv_command(uv_path, uv_args)),
        ]

    raise ValueError(f"Unknown benchmark: {benchmark}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark cpip against uv with hyperfine."
    )
    parser.add_argument("--benchmark", "-b", choices=BENCHMARKS, action="append")
    parser.add_argument("--workload", choices=("offline", "live"), default="offline")
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

    min_runs = args.min_runs
    if args.runs is None and min_runs is None:
        min_runs = 10

    benchmarks = args.benchmark or list(BENCHMARKS)
    with tempfile.TemporaryDirectory(prefix="cpip-bench-") as temporary:
        workspace_root = Path(temporary)
        for benchmark in benchmarks:
            workspace = workspace_root / benchmark
            workspace.mkdir()
            commands = build_commands(
                benchmark,
                workload=args.workload,
                workspace=workspace,
                cpip_python=args.cpip_python,
                cpip_console=args.cpip_console,
                cpip_launcher=args.cpip_launcher,
                uv_path=args.uv_path,
                python=args.python,
            )
            setup = None
            if benchmark.endswith("warm"):
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
                name=benchmark,
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
