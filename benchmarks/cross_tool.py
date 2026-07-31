"""Compare core-pip and uv over the same deterministic workloads.

The ASV suite is useful for tracking pip over time.  This module is deliberately
separate: Hyperfine runs both tools as peer commands and reports their relative
performance on the same machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Mapping, cast

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 and older
    import tomli as tomllib

from .uv_scenarios import create_offline_scenarios


class Benchmark(str, Enum):
    RESOLVE_COLD = "resolve-cold"
    RESOLVE_WARM = "resolve-warm"
    INSTALL_COLD = "install-cold"
    INSTALL_WARM = "install-warm"
    STARTUP_VERSION = "startup-version"
    STARTUP_VERSION_COLD = "startup-version-cold"
    STARTUP_HELP = "startup-help"
    STARTUP_HELP_COLD = "startup-help-cold"
    STARTUP_FAST_INSTALL = "startup-fast-install"
    STARTUP_FALLBACK_INSTALL = "startup-fallback-install"
    STARTUP_FULL_FALLBACK_INSTALL = "startup-full-fallback-install"


class Tool(str, Enum):
    CORE_PIP = "core-pip"
    UV_PIP = "uv-pip"


class Command:
    __slots__ = ("name", "command", "prepare")

    def __init__(self, name: str, command: list[str], prepare: str) -> None:
        self.name = name
        self.command = command
        self.prepare = prepare


class ScenarioPaths:
    """Typed view of one generated scenario fixture."""

    __slots__ = (
        "name",
        "wheelhouse",
        "requirements",
        "input",
        "incremental_input",
        "expected_projects",
    )

    def __init__(self, values: Mapping[str, object]) -> None:
        self.name = cast(str, values["name"])
        self.wheelhouse = Path(cast(str, values["wheelhouse"]))
        self.requirements = cast(tuple[str, ...], values["requirements"])
        self.input = Path(cast(str, values["input"]))
        self.incremental_input = Path(cast(str, values["incremental_input"]))
        self.expected_projects = cast(int, values["expected_projects"])


def _as_scenario(values: ScenarioPaths | Mapping[str, object]) -> ScenarioPaths:
    return values if isinstance(values, ScenarioPaths) else ScenarioPaths(values)


def _quote(path: Path) -> str:
    import shlex

    return shlex.quote(os.fspath(path))


def _run(command: list[str]) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_QUIET": "1",
        }
    )
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode:
        if result.stderr:
            sys.stderr.buffer.write(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, command)


def _uv(path: str | None) -> str:
    executable = path or shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is not installed; pass --uv-path or install uv")
    return executable


def _command(
    scenario: ScenarioPaths | Mapping[str, object],
    tool: Tool,
    benchmark: Benchmark,
    state: Path,
    *,
    uv_path: str | None,
) -> Command:
    scenario = _as_scenario(scenario)
    wheelhouse = scenario.wheelhouse
    input_file = scenario.input
    cache = state / "cache"
    output = state / ("pylock.toml" if tool is Tool.CORE_PIP else "requirements.txt")
    target = state / "target"
    common = [
        "--no-index",
        "--find-links",
        os.fspath(wheelhouse),
    ]

    if benchmark in {Benchmark.STARTUP_VERSION, Benchmark.STARTUP_VERSION_COLD}:
        if tool is Tool.CORE_PIP:
            command = [
                "env",
                "PYTHONDONTWRITEBYTECODE=",
                f"PYTHONPYCACHEPREFIX={state / 'pycache'}",
                sys.executable,
                "-m",
                "pip",
                "--version",
            ]
        else:
            command = [_uv(uv_path), "--version"]
        prepare = (
            f"rm -rf {_quote(state / 'pycache')}"
            if benchmark is Benchmark.STARTUP_VERSION_COLD
            else "true"
        )
        return Command(tool.value, command, prepare)

    if benchmark in {Benchmark.STARTUP_HELP, Benchmark.STARTUP_HELP_COLD}:
        if tool is Tool.CORE_PIP:
            command = [
                "env",
                "PYTHONDONTWRITEBYTECODE=",
                f"PYTHONPYCACHEPREFIX={state / 'pycache'}",
                sys.executable,
                "-m",
                "pip",
                "--help",
            ]
        else:
            command = [_uv(uv_path), "pip", "--help"]
        prepare = (
            f"rm -rf {_quote(state / 'pycache')}"
            if benchmark is Benchmark.STARTUP_HELP_COLD
            else "true"
        )
        return Command(tool.value, command, prepare)

    if benchmark.value.startswith("resolve"):
        if tool is Tool.CORE_PIP:
            command = [
                "env",
                f"PIP_CACHE_DIR={os.fspath(cache)}",
                "PYTHONDONTWRITEBYTECODE=",
                f"PYTHONPYCACHEPREFIX={state / 'pycache'}",
                sys.executable,
                "-m",
                "pip",
                "lock",
                *common,
                "--output",
                os.fspath(output),
                "--quiet",
                "-r",
                os.fspath(input_file),
            ]
        else:
            command = [
                _uv(uv_path),
                "pip",
                "compile",
                os.fspath(input_file),
                *common,
                "--cache-dir",
                os.fspath(cache),
                "--output-file",
                os.fspath(output),
                "--quiet",
            ]
        output_reset = f"rm -f {_quote(output)}"
        if benchmark is Benchmark.RESOLVE_COLD:
            prepare = f"rm -rf {_quote(cache)} {_quote(output)}"
        else:
            prepare = output_reset
        return Command(tool.value, command, prepare)

    common.extend(
        [
            "--cache-dir",
            os.fspath(cache),
            "--target",
            os.fspath(target),
            "--no-compile",
        ]
    )
    if tool is Tool.CORE_PIP:
        command = [
            "env",
            "PYTHONDONTWRITEBYTECODE=",
            f"PYTHONPYCACHEPREFIX={state / 'pycache'}",
            sys.executable,
            "-m",
            "pip",
            "install",
            "--ignore-installed",
            *common,
            "-r",
            os.fspath(input_file),
        ]
        if benchmark is Benchmark.STARTUP_FULL_FALLBACK_INSTALL:
            command.append("--upgrade")
        if benchmark not in {
            Benchmark.STARTUP_FALLBACK_INSTALL,
            Benchmark.STARTUP_FULL_FALLBACK_INSTALL,
        }:
            command.insert(-2, "--quiet")
    else:
        command = [
            _uv(uv_path),
            "pip",
            "install",
            "--python",
            sys.executable,
            *common,
            "-r",
            os.fspath(input_file),
        ]
        if benchmark is Benchmark.STARTUP_FULL_FALLBACK_INSTALL:
            command.append("--upgrade")
        if benchmark not in {
            Benchmark.STARTUP_FALLBACK_INSTALL,
            Benchmark.STARTUP_FULL_FALLBACK_INSTALL,
        }:
            command.append("--quiet")
    if benchmark is Benchmark.INSTALL_COLD:
        prepare = f"rm -rf {_quote(cache)} {_quote(target)}"
    else:
        prepare = f"rm -rf {_quote(target)}"
    return Command(tool.value, command, prepare)


def _prepare_warm(command: Command) -> None:
    _run(command.command)
    for argument in command.command:
        if argument.endswith("/target"):
            shutil.rmtree(argument, ignore_errors=True)
    output = next(
        (
            Path(argument)
            for argument in command.command
            if argument.endswith(("pylock.toml", "requirements.txt"))
        ),
        None,
    )
    if output is not None:
        output.unlink(missing_ok=True)


def _version(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return (result.stdout or result.stderr).strip()


def _fixture_digest(wheelhouse: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(wheelhouse.glob("*.whl")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _normalize_output(path: Path) -> set[tuple[str, str]]:
    if path.name == "pylock.toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return {
            (str(package["name"]).lower().replace("_", "-"), str(package["version"]))
            for package in data.get("packages", [])
            if "name" in package and "version" in package
        }
    matches = re.findall(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)",
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    return {(name.lower().replace("_", "-"), version) for name, version in matches}


def _validate_scenario(
    scenario: ScenarioPaths, *, uv_path: str | None, root: Path
) -> None:
    """Run both resolvers once and verify the deterministic fixture agrees."""
    outputs: dict[Tool, set[tuple[str, str]]] = {}
    for tool in Tool:
        state = root / f"validate-{tool.value}"
        command = _command(
            scenario, tool, Benchmark.RESOLVE_COLD, state, uv_path=uv_path
        )
        state.mkdir(parents=True, exist_ok=True)
        _run(command.command)
        output = state / ("pylock.toml" if tool is Tool.CORE_PIP else "requirements.txt")
        outputs[tool] = _normalize_output(output)
    if outputs[Tool.CORE_PIP] != outputs[Tool.UV_PIP]:
        raise RuntimeError(
            "core-pip and uv selected different deterministic resolutions for "
            f"{scenario.name}: {outputs}"
        )


def run_comparison(
    scenario_name: str,
    scenario: ScenarioPaths,
    benchmark: Benchmark,
    *,
    tools: tuple[Tool, ...],
    uv_path: str | None,
    warmup: int,
    runs: int,
    output: Path | None,
) -> None:
    hyperfine = shutil.which("hyperfine")
    if hyperfine is None:
        raise RuntimeError("hyperfine is not installed")

    with tempfile.TemporaryDirectory(prefix="core-pip-benchmark-") as directory:
        root = Path(directory)
        commands: list[Command] = []
        for tool in tools:
            state = root / tool.value
            state.mkdir()
            command = _command(scenario, tool, benchmark, state, uv_path=uv_path)
            if benchmark in {
                Benchmark.RESOLVE_WARM,
                Benchmark.INSTALL_WARM,
                Benchmark.STARTUP_FAST_INSTALL,
                Benchmark.STARTUP_FALLBACK_INSTALL,
                Benchmark.STARTUP_FULL_FALLBACK_INSTALL,
            }:
                _prepare_warm(command)
            commands.append(command)

        json_path = output or (Path.cwd() / f"{scenario_name}-{benchmark.value}.json")
        args = [hyperfine, "--warmup", str(warmup), "--runs", str(runs)]
        args.extend(["--export-json", os.fspath(json_path)])
        for command in commands:
            args.extend(["--command-name", command.name])
        for command in commands:
            args.extend(["--prepare", command.prepare])
        for command in commands:
            import shlex

            args.append(shlex.join(command.command))
        subprocess.run(args, check=True)

        manifest = {
            "schema": 1,
            "scenario": scenario_name,
            "benchmark": benchmark.value,
            "bytecode_policy": "dedicated-pycache-prefix",
            "tools": [command.name for command in commands],
            "python": _version([sys.executable, "--version"]),
            "pip": _version([sys.executable, "-m", "pip", "--version"]),
            "uv": _version([_uv(uv_path), "--version"]),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "fixture_sha256": _fixture_digest(
                scenario.wheelhouse
            ),
            "expected_projects": scenario.expected_projects,
            "requirements": scenario.requirements,
            "hyperfine": json.loads(json_path.read_text(encoding="utf-8")),
        }
        json_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append")
    parser.add_argument(
        "--benchmark", action="append", choices=[item.value for item in Benchmark]
    )
    parser.add_argument("--tool", action="append", choices=[item.value for item in Tool])
    parser.add_argument("--uv-path")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmarks/results"))
    args = parser.parse_args(argv)

    if args.warmup < 0 or args.runs < 1:
        parser.error("--warmup must be non-negative and --runs must be positive")
    selected_tools = tuple(Tool(value) for value in (args.tool or [item.value for item in Tool]))
    selected_benchmarks = tuple(
        Benchmark(value) for value in (args.benchmark or [Benchmark.RESOLVE_COLD.value, Benchmark.RESOLVE_WARM.value])
    )
    root = Path(tempfile.mkdtemp(prefix="core-pip-scenarios-"))
    try:
        scenarios = create_offline_scenarios(root)
        names = tuple(args.scenario or scenarios)
        unknown = set(names) - set(scenarios)
        if unknown:
            parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            scenario = ScenarioPaths(scenarios[name])
            _validate_scenario(scenario, uv_path=args.uv_path, root=root)
            for benchmark in selected_benchmarks:
                output = args.output_dir / f"{name}-{benchmark.value}.json"
                run_comparison(
                    name,
                    scenario,
                    benchmark,
                    tools=selected_tools,
                    uv_path=args.uv_path,
                    warmup=args.warmup,
                    runs=args.runs,
                    output=output,
                )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
