"""Offline cpip versus uv benchmarks over compact uv-derived scenarios."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .uv_scenarios import (
    BACKTRACKING_SCENARIOS,
    SCENARIOS,
    create_offline_scenarios,
)


TOOLS = ("cpip", "uv-pip")
SCENARIO_NAMES = tuple(item.name for item in SCENARIOS) + BACKTRACKING_SCENARIOS
RESOLVE_MODES = ("cold", "warm", "incremental", "noop")
INSTALL_MODES = ("cold", "warm")


def run_internal(command: list[str]) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "CPIP_DISABLE_CPIP_VERSION_CHECK": "1",
            "CPIP_NO_INPUT": "1",
            "CPIP_QUIET": "1",
        }
    )
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )


def uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is not installed in the ASV environment")
    return executable


def resolve_command(
    scenario: dict[str, object],
    tool: str,
    *,
    cache: Path,
    output: Path,
    incremental: bool = False,
    locked_input: Path | None = None,
) -> list[str]:
    wheelhouse = str(scenario["wheelhouse"])
    if tool == "cpip":
        input_file = str(
            locked_input
            or (scenario["incremental_input"] if incremental else scenario["input"])
        )
        command = [
            sys.executable,
            "-m",
            "cpip",
            "lock",
            "-r",
            input_file,
            "--no-index",
            "--find-links",
            wheelhouse,
            "--output",
            os.fspath(output),
            "--quiet",
        ]
        return command
    input_file = str(
        scenario["incremental_input"] if incremental else scenario["input"]
    )
    return [
        uv(),
        "pip",
        "compile",
        input_file,
        "--no-index",
        "--find-links",
        wheelhouse,
        "--cache-dir",
        os.fspath(cache),
        "--output-file",
        os.fspath(output),
        "--quiet",
    ]


def install_command_internal(
    scenario: dict[str, object], tool: str, *, cache: Path, target: Path
) -> list[str]:
    common = [
        "--no-index",
        "--find-links",
        str(scenario["wheelhouse"]),
        "--cache-dir",
        os.fspath(cache),
        "--target",
        os.fspath(target),
        "-r",
        str(scenario["input"]),
    ]
    if tool == "cpip":
        return [
            sys.executable,
            "-m",
            "cpip",
            "install",
            "--ignore-installed",
            "--no-compile",
            *common,
        ]
    return [uv(), "pip", "install", "--python", sys.executable, *common]


class OfflineBenchmark:
    number = 1
    repeat = 5
    rounds = 2
    warmup_time = 0
    timeout = 600

    @staticmethod
    def setup_cache() -> dict[str, dict[str, object]]:
        root = Path.cwd() / "uv-offline-scenarios"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return create_offline_scenarios(root)


class OfflineResolution(OfflineBenchmark):
    params = (SCENARIO_NAMES, TOOLS, RESOLVE_MODES)
    param_names = ("scenario", "tool", "cache_state")

    def setup(
        self,
        scenarios: dict[str, dict[str, object]],
        scenario: str,
        tool: str,
        cache_state: str,
    ) -> None:
        state = scenarios[scenario]
        work = Path.cwd() / "uv-offline-work" / scenario / tool
        work.mkdir(parents=True, exist_ok=True)
        self.cache = work / "cache"
        suffix = "toml" if tool == "cpip" else "txt"
        self.output = work / f"resolved.{suffix}"
        self.baseline = work / (
            "pylock.baseline.toml" if tool == "cpip" else "baseline.txt"
        )
        self.command = resolve_command(
            state, tool, cache=self.cache, output=self.output
        )
        if cache_state == "cold":
            shutil.rmtree(self.cache, ignore_errors=True)
            self.output.unlink(missing_ok=True)
        elif cache_state == "warm":
            marker = work / "warm-ready"
            if not marker.exists():
                run_internal(self.command)
                marker.touch()
            self.output.unlink(missing_ok=True)
        elif cache_state == "noop":
            if not self.output.exists():
                run_internal(self.command)
        else:
            if tool == "uv-pip" and not self.baseline.exists():
                run_internal(
                    resolve_command(
                        state,
                        tool,
                        cache=self.cache,
                        output=self.baseline,
                    )
                )
            if tool == "uv-pip":
                shutil.copyfile(self.baseline, self.output)
            else:
                self.output.unlink(missing_ok=True)
            self.command = resolve_command(
                state,
                tool,
                cache=self.cache,
                output=self.output,
                incremental=True,
            )

    def time_resolve(
        self,
        scenarios: dict[str, dict[str, object]],
        scenario: str,
        tool: str,
        cache_state: str,
    ) -> None:
        run_internal(self.command)


class OfflineInstallation(OfflineBenchmark):
    params = (SCENARIO_NAMES, TOOLS, INSTALL_MODES)
    param_names = ("scenario", "tool", "cache_state")

    def setup(
        self,
        scenarios: dict[str, dict[str, object]],
        scenario: str,
        tool: str,
        cache_state: str,
    ) -> None:
        state = scenarios[scenario]
        work = Path.cwd() / "uv-offline-install" / scenario / tool
        work.mkdir(parents=True, exist_ok=True)
        cache = work / "cache"
        target = work / "target"
        command = install_command_internal(state, tool, cache=cache, target=target)
        if cache_state == "cold":
            shutil.rmtree(cache, ignore_errors=True)
        else:
            marker = work / "warm-ready"
            if not marker.exists():
                shutil.rmtree(target, ignore_errors=True)
                run_internal(command)
                marker.touch()
        shutil.rmtree(target, ignore_errors=True)
        self.command = command

    def time_install(
        self,
        scenarios: dict[str, dict[str, object]],
        scenario: str,
        tool: str,
        cache_state: str,
    ) -> None:
        run_internal(self.command)
