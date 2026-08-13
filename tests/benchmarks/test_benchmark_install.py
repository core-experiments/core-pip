"""Benchmarks for wheel inspection and installation.

Installation is the last phase of every ``pip install`` run: pip reads the
wheel metadata, unpacks the archive and writes the ``RECORD`` for the
installed distribution.

``test_unzip_wheel_many_files``, ``test_unpack_sdist_many_files``, and
``test_install_wheel_many_files`` port uv's ``uv-bench`` many-files suite
(``crates/uv-bench/benches/uv.rs``) at the same 10,000-file scale. One uv
benchmark was deliberately not ported: ``prepare_wheel_many_files`` measures
extract-then-``validate_and_heal_record`` as a step distinct from a full
install, but cpip has no isolated equivalent -- RECORD writing happens
inline during install (``wheel_transaction.py``), not as a separate
heal-a-shipped-RECORD pass, so there is no matching production code path to
benchmark in isolation.
"""

from __future__ import annotations

import itertools
import zipfile
from pathlib import Path

from benchmark_support import reset_caches
from cpip.core.hashes import Hashes, hash_file
from cpip.core.wheel import read_wheel_metadata, validate_wheel
from cpip.install.target import InstallTarget
from cpip.install.unpacking import untar_file, unzip_file
from cpip.install.wheel_transaction import WheelInstaller
from pytest_codspeed import BenchmarkFixture


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
    benchmark: BenchmarkFixture,
    payload_wheel: Path,
    tmp_path: Path,
) -> None:
    counter = itertools.count()

    def unzip() -> None:
        destination = tmp_path / f"unpacked-{next(counter)}"
        unzip_file(str(payload_wheel), str(destination), flatten=False)

    benchmark(unzip)


def test_install_wheel(
    benchmark: BenchmarkFixture,
    payload_wheel: Path,
    tmp_path: Path,
) -> None:
    counter = itertools.count()

    def install() -> None:
        destination = tmp_path / f"target-{next(counter)}"
        target = InstallTarget.from_options("payload-pkg", target=str(destination))
        WheelInstaller(target, pycompile=False).install(payload_wheel)

    benchmark(install)


def test_unzip_wheel_many_files(
    benchmark: BenchmarkFixture,
    many_files_wheel: Path,
    tmp_path: Path,
) -> None:
    """Port of uv's ``unzip_wheel_many_files`` (crates/uv-bench/benches/uv.rs):
    ``unzip_file`` at 10,000 files instead of ``test_unzip_wheel``'s 300,
    where per-member archive overhead dominates instead of being swamped by
    fixed per-call cost.
    """
    counter = itertools.count()

    def unzip() -> None:
        destination = tmp_path / f"unpacked-many-{next(counter)}"
        unzip_file(str(many_files_wheel), str(destination), flatten=False)

    benchmark(unzip)


def test_unpack_sdist_many_files(
    benchmark: BenchmarkFixture,
    many_files_sdist: Path,
    tmp_path: Path,
) -> None:
    """Port of uv's ``unpack_sdist_many_files``. cpip had no many-files sdist
    unpack benchmark at all before this -- ``test_sdist_metadata_build*``
    only exercise small, build-backend-driven sdists.
    """
    counter = itertools.count()

    def untar() -> None:
        destination = tmp_path / f"untarred-many-{next(counter)}"
        untar_file(str(many_files_sdist), str(destination))

    benchmark(untar)


def test_install_wheel_many_files(
    benchmark: BenchmarkFixture,
    many_files_wheel: Path,
    tmp_path: Path,
) -> None:
    """Port of uv's ``install_wheel_many_files``: a full
    ``WheelInstaller.install()`` at 10,000 files, versus
    ``test_install_wheel``'s 300 -- large enough that a regression in the
    archive-reader selected by ``open_wheel_archive`` (as in PR #22's
    ``wheel_layout``/``zipfile.ZipFile``-fallback regression) shows up as a
    much larger relative swing.
    """
    counter = itertools.count()

    def install() -> None:
        destination = tmp_path / f"target-many-{next(counter)}"
        target = InstallTarget.from_options("many-files-pkg", target=str(destination))
        WheelInstaller(target, pycompile=False).install(many_files_wheel)

    benchmark(install)


def test_hash_wheel_file(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    digest, _ = hash_file(str(payload_wheel))
    hashes = Hashes({"sha256": [digest.hexdigest()]})

    def check_hash() -> None:
        hashes.check_against_path(str(payload_wheel))

    benchmark(check_hash)
