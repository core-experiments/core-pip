"""Cold- and warm-cache benchmarks for resolution and installation."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


VERSIONS = 24


def record_row(path: str, data: bytes) -> tuple[str, str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return path, f"sha256={digest.decode()}", str(len(data))


def make_wheel_internal(
    wheelhouse: Path,
    project: str,
    version: str,
    *,
    requires: tuple[str, ...] = (),
) -> Path:
    dist = project.replace("-", "_")
    wheel = wheelhouse / f"{dist}-{version}-py3-none-any.whl"
    files = {
        f"{dist}/__init__.py": f"VERSION = {version!r}\n".encode(),
        f"{dist}-{version}.dist-info/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {project}\n"
            f"Version: {version}\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
        ).encode(),
        f"{dist}-{version}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: pip-asv\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ).encode(),
    }
    record = f"{dist}-{version}.dist-info/RECORD"
    rows = [record_row(path, data) for path, data in files.items()]
    rows.append((record, "", ""))
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
        output = []
        for row in rows:
            output.append(",".join(row))
        archive.writestr(record, "\n".join(output) + "\n")
    return wheel


def make_sdist_internal(wheelhouse: Path, project: str, version: str) -> Path:
    dist = project.replace("-", "_")
    root_name = f"{dist}-{version}"
    source_root = Path(tempfile.mkdtemp(prefix="pip-asv-sdist-")) / root_name
    package = source_root / dist
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text(
        f"VERSION = {version!r}\n", encoding="utf-8"
    )
    source_root.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                "requires = []",
                'build-backend = "pip.build.build_backend"',
                "",
                "[project]",
                f'name = "{project}"',
                f'version = "{version}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    archive = wheelhouse / f"{root_name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source_root, arcname=root_name)
    shutil.rmtree(source_root.parent)
    return archive


def create_workload(root: Path) -> dict[str, str]:
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    # The highest-ranked candidate is an sdist, followed by many wheels. A
    # successful lazy resolution should not materialize the lower candidates.
    make_sdist_internal(wheelhouse, "bench-root", f"{VERSIONS + 1}.0")
    for version in range(VERSIONS, 0, -1):
        make_wheel_internal(wheelhouse, "bench-root", f"{version}.0")
    return {
        "root": os.fspath(root),
        "wheelhouse": os.fspath(wheelhouse),
        "cache": os.fspath(root / "cache"),
        "uv_cache": os.fspath(root / "uv-cache"),
        "report": os.fspath(root / "report.json"),
        "target": os.fspath(root / "target"),
        "uv_target": os.fspath(root / "uv-target"),
    }


def pip_command_internal(state: dict[str, str], *, install: bool) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        state["wheelhouse"],
        "--cache-dir",
        state["cache"],
        "--no-build-isolation",
    ]
    if install:
        command.extend(["--target", state["target"]])
    else:
        command.extend(["--dry-run", "--report", state["report"]])
    command.append("bench-root")
    return command


def uv_command(state: dict[str, str]) -> list[str]:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is not installed in the ASV environment")
    return [
        executable,
        "pip",
        "install",
        "--python",
        sys.executable,
        "--no-index",
        "--find-links",
        state["wheelhouse"],
        "--cache-dir",
        state["uv_cache"],
        "--no-build-isolation",
        "--compile-bytecode",
        "--target",
        state["uv_target"],
        "bench-root",
    ]


def run_command_internal(command: list[str]) -> None:
    env = os.environ.copy()
    env.update(
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
        env=env,
    )


def run_pip(state: dict[str, str], *, install: bool) -> None:
    run_command_internal(pip_command_internal(state, install=install))


def run_uv(state: dict[str, str]) -> None:
    run_command_internal(uv_command(state))


class CacheBenchmark:
    number = 1
    repeat = 7
    rounds = 2
    warmup_time = 0
    timeout = 180

    @staticmethod
    def setup_cache() -> dict[str, str]:
        root = Path.cwd() / "cache-workload"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return create_workload(root)


class ColdResolve(CacheBenchmark):
    def setup(self, state: dict[str, str]) -> None:
        shutil.rmtree(state["cache"], ignore_errors=True)
        Path(state["report"]).unlink(missing_ok=True)

    def time_resolve_cold(self, state: dict[str, str]) -> None:
        run_pip(state, install=False)


class WarmResolve(CacheBenchmark):
    @staticmethod
    def setup_cache() -> dict[str, str]:
        state = CacheBenchmark.setup_cache()
        run_pip(state, install=False)
        return state

    def setup(self, state: dict[str, str]) -> None:
        Path(state["report"]).unlink(missing_ok=True)

    def time_resolve_warm(self, state: dict[str, str]) -> None:
        run_pip(state, install=False)


class ColdInstall(CacheBenchmark):
    def setup(self, state: dict[str, str]) -> None:
        shutil.rmtree(state["cache"], ignore_errors=True)
        shutil.rmtree(state["target"], ignore_errors=True)

    def time_install_cold(self, state: dict[str, str]) -> None:
        run_pip(state, install=True)


class WarmInstall(CacheBenchmark):
    @staticmethod
    def setup_cache() -> dict[str, str]:
        state = CacheBenchmark.setup_cache()
        run_pip(state, install=True)
        shutil.rmtree(state["target"], ignore_errors=True)
        return state

    def setup(self, state: dict[str, str]) -> None:
        shutil.rmtree(state["target"], ignore_errors=True)

    def time_install_warm(self, state: dict[str, str]) -> None:
        run_pip(state, install=True)


class UvColdInstall(CacheBenchmark):
    def setup(self, state: dict[str, str]) -> None:
        shutil.rmtree(state["uv_cache"], ignore_errors=True)
        shutil.rmtree(state["uv_target"], ignore_errors=True)

    def time_install_cold(self, state: dict[str, str]) -> None:
        run_uv(state)


class UvWarmInstall(CacheBenchmark):
    @staticmethod
    def setup_cache() -> dict[str, str]:
        state = CacheBenchmark.setup_cache()
        run_uv(state)
        shutil.rmtree(state["uv_target"], ignore_errors=True)
        return state

    def setup(self, state: dict[str, str]) -> None:
        shutil.rmtree(state["uv_target"], ignore_errors=True)

    def time_install_warm(self, state: dict[str, str]) -> None:
        run_uv(state)
