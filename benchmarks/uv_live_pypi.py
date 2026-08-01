"""Opt-in live-PyPI ports of uv's resolver and installation workloads."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


CUTOFF = "2024-08-08"
TOOLS = ("core-pip", "uv-pip")
RESOLVE_MODES = ("cold", "warm", "incremental", "noop")
LIVE_CASES = {
    "jupyter": ("jupyter==1.0.0",),
    "airflow": (
        "apache-airflow[all]==2.9.3",
        "apache-airflow-providers-apache-beam>3.0.0",
    ),
    "trio": (
        "sphinx>=4.0,<6.2",
        "jinja2",
        "sphinx_rtd_theme",
        "sphinxcontrib-jquery",
        "sphinxcontrib-trio",
        "towncrier",
        "attrs>=19.2.0",
        "sortedcontainers",
        "idna",
        "outcome",
        "sniffio",
        "exceptiongroup>=1.0.0rc9",
        "immutables>=0.6",
        "pyOpenSSL",
    ),
    "apache-beam-dill": ("dill<0.3.9,>=0.2.2", "apache-beam<=2.49.0"),
    "numpy-numba": ("numpy>=2.1,<2.2", "numba<=0.60,>0.1"),
    "numpy-sparse": ("numpy>=1.24,<2.1.2", "sparse<0.15.4"),
    "sentry": (
        "python-rapidjson<=1.20,>=1.4",
        "sentry-kafka-schemas<=0.1.113,>=0.1.50",
    ),
    "starlette-fastapi": ("starlette<=0.36.0", "fastapi<=0.115.2"),
}
TRIO_COMPILED = (
    "alabaster==0.7.15",
    "attrs==23.2.0",
    "babel==2.14.0",
    "certifi==2023.11.17",
    "cffi==1.16.0",
    "charset-normalizer==3.3.2",
    "click==8.1.7",
    "cryptography==41.0.7",
    "docutils==0.19",
    "exceptiongroup==1.2.0",
    "idna==3.6",
    "imagesize==1.4.1",
    "immutables==0.20",
    "incremental==22.10.0",
    "jinja2==3.1.2",
    "markupsafe==2.1.3",
    "outcome==1.3.0.post0",
    "packaging==23.2",
    "pycparser==2.21",
    "pygments==2.17.2",
    "pyopenssl==23.3.0",
    "requests==2.31.0",
    "sniffio==1.3.0",
    "snowballstemmer==2.2.0",
    "sortedcontainers==2.4.0",
    "sphinx==6.1.3",
    "sphinx-rtd-theme==2.0.0",
    "sphinxcontrib-applehelp==1.0.7",
    "sphinxcontrib-devhelp==1.0.5",
    "sphinxcontrib-htmlhelp==2.0.4",
    "sphinxcontrib-jquery==4.1",
    "sphinxcontrib-jsmath==1.0.1",
    "sphinxcontrib-qthelp==1.0.6",
    "sphinxcontrib-serializinghtml==1.1.9",
    "sphinxcontrib-trio==1.1.2",
    "tomli==2.0.1",
    "towncrier==23.11.0",
    "urllib3==2.1.0",
)


def require_live_benchmarks() -> None:
    if os.environ.get("PIP_BENCH_LIVE") != "1":
        raise NotImplementedError("set PIP_BENCH_LIVE=1 to run live-PyPI cases")


def uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is not installed in the ASV environment")
    return executable


def run_internal(command: list[str]) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_QUIET": "1",
        }
    )
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )


def create_inputs(root: Path) -> dict[str, dict[str, str]]:
    state: dict[str, dict[str, str]] = {}
    for name, requirements in LIVE_CASES.items():
        directory = root / name
        directory.mkdir(parents=True)
        input_file = directory / "requirements.in"
        input_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        incremental = directory / "requirements-incremental.in"
        incremental.write_text(
            "django\n" + input_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        state[name] = {
            "input": os.fspath(input_file),
            "incremental": os.fspath(incremental),
        }
    compiled = root / "trio-compiled.txt"
    compiled.write_text("\n".join(TRIO_COMPILED) + "\n", encoding="utf-8")
    state["trio"]["compiled"] = os.fspath(compiled)
    return state


def resolve_command(
    state: dict[str, str], tool: str, cache: Path, output: Path, *, incremental: bool
) -> list[str]:
    input_file = state["incremental"] if incremental else state["input"]
    if tool == "core-pip":
        return [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--uploaded-prior-to",
            CUTOFF,
            "--cache-dir",
            os.fspath(cache),
            "--report",
            os.fspath(output),
            "-r",
            input_file,
            "--quiet",
        ]
    return [
        uv(),
        "pip",
        "compile",
        input_file,
        "--exclude-newer",
        CUTOFF,
        "--cache-dir",
        os.fspath(cache),
        "--output-file",
        os.fspath(output),
        "--quiet",
    ]


class LiveBenchmark:
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 900

    @staticmethod
    def setup_cache() -> dict[str, dict[str, str]]:
        require_live_benchmarks()
        root = Path.cwd() / "uv-live-pypi"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return create_inputs(root)


class LiveResolution(LiveBenchmark):
    params = (tuple(LIVE_CASES), TOOLS, RESOLVE_MODES)
    param_names = ("scenario", "tool", "cache_state")

    def setup(
        self,
        states: dict[str, dict[str, str]],
        scenario: str,
        tool: str,
        cache_state: str,
    ) -> None:
        work = Path.cwd() / "uv-live-work" / scenario / tool
        work.mkdir(parents=True, exist_ok=True)
        cache = work / "cache"
        output = work / ("report.json" if tool == "core-pip" else "requirements.txt")
        incremental = cache_state == "incremental"
        command = resolve_command(
            states[scenario], tool, cache, output, incremental=incremental
        )
        if cache_state == "cold":
            shutil.rmtree(cache, ignore_errors=True)
            output.unlink(missing_ok=True)
        elif cache_state == "warm":
            marker = work / "warm-ready"
            if not marker.exists():
                run_internal(command)
                marker.touch()
            output.unlink(missing_ok=True)
        elif cache_state == "noop":
            if not output.exists():
                run_internal(command)
        else:
            baseline = resolve_command(
                states[scenario], tool, cache, output, incremental=False
            )
            marker = work / "incremental-ready"
            if not marker.exists():
                run_internal(baseline)
                marker.touch()
        self.command = command

    def time_resolve(
        self,
        states: dict[str, dict[str, str]],
        scenario: str,
        tool: str,
        cache_state: str,
    ) -> None:
        run_internal(self.command)


class LiveTrioInstallation(LiveBenchmark):
    params = (TOOLS, ("cold", "warm"))
    param_names = ("tool", "cache_state")

    def setup(
        self,
        states: dict[str, dict[str, str]],
        tool: str,
        cache_state: str,
    ) -> None:
        work = Path.cwd() / "uv-live-install" / tool
        work.mkdir(parents=True, exist_ok=True)
        cache = work / "cache"
        target = work / "target"
        common = [
            "--cache-dir",
            os.fspath(cache),
            "--target",
            os.fspath(target),
            "-r",
            states["trio"]["compiled"],
        ]
        if tool == "core-pip":
            command = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--ignore-installed",
                "--uploaded-prior-to",
                CUTOFF,
                *common,
            ]
        else:
            command = [
                uv(),
                "pip",
                "install",
                "--python",
                sys.executable,
                "--exclude-newer",
                CUTOFF,
                "--compile-bytecode",
                *common,
            ]
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
        states: dict[str, dict[str, str]],
        tool: str,
        cache_state: str,
    ) -> None:
        run_internal(self.command)
