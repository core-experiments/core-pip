"""Benchmarks for wheel inspection and installation.

Installation is the last phase of every ``pip install`` run: pip reads the
wheel metadata, unpacks the archive and writes the ``RECORD`` for the
installed distribution.
"""

from __future__ import annotations

import itertools
import zipfile
from pathlib import Path

from benchmark_support import reset_caches
from pytest_codspeed import BenchmarkFixture
from cpip.core.hashes import Hashes, hash_file
from cpip.core.wheel import read_wheel_metadata, validate_wheel
from cpip.install.target import InstallTarget
from cpip.install.unpacking import unzip_file
from cpip.install.wheel_transaction import WheelInstaller


def test_read_wheel_metadata(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    def read_metadata() -> object:
        reset_caches()
        return read_wheel_metadata(payload_wheel)

    assert benchmark(read_metadata) is not None


def test_validate_wheel(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    def validate() -> str:
        with zipfile.ZipFile(payload_wheel) as archive:
            return validate_wheel(archive, "payload-pkg")

    assert benchmark(validate).endswith(".dist-info")


def test_unzip_wheel(
    benchmark: BenchmarkFixture, payload_wheel: Path, tmp_path: Path
) -> None:
    counter = itertools.count()

    def unzip() -> None:
        destination = tmp_path / f"unpacked-{next(counter)}"
        unzip_file(str(payload_wheel), str(destination), flatten=False)

    benchmark(unzip)


def test_install_wheel(
    benchmark: BenchmarkFixture, payload_wheel: Path, tmp_path: Path
) -> None:
    counter = itertools.count()

    def install() -> None:
        destination = tmp_path / f"target-{next(counter)}"
        target = InstallTarget.from_options("payload-pkg", target=str(destination))
        WheelInstaller(target, pycompile=False).install(payload_wheel)

    benchmark(install)


def test_hash_wheel_file(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    digest, _ = hash_file(str(payload_wheel))
    hashes = Hashes({"sha256": [digest.hexdigest()]})

    def check_hash() -> None:
        hashes.check_against_path(str(payload_wheel))

    benchmark(check_hash)
