"""Ports of pip-relevant Criterion benchmarks from uv."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from pip.core.packaging import SpecifierSet
from pip.core.wheel import validate_wheel
from pip.install.target import InstallTarget
from pip.install.unpacking import untar_file, unzip_file
from pip.install.wheel_transaction import WheelInstaller

from .uv_scenarios import create_many_files_archives


class VersionSpecifierParsing:
    params = (">=3.8", ">=3.8,<4", ">=2.5, !=3.0.*, !=3.1.*, !=3.2.*, <4")
    param_names = ("specifiers",)
    number = 1000

    def time_parse_version_specifiers(self, specifiers: str) -> None:
        SpecifierSet(specifiers)


class ManyFilesArchive:
    number = 1
    repeat = 5
    rounds = 2
    warmup_time = 0
    timeout = 300

    @staticmethod
    def setup_cache() -> dict[str, str]:
        root = Path.cwd() / "uv-many-files"
        if root.exists():
            shutil.rmtree(root)
        return create_many_files_archives(root)


class UnpackSdistManyFiles(ManyFilesArchive):
    def setup(self, state: dict[str, str]) -> None:
        self.destination = Path.cwd() / "uv-many-files-sdist"
        shutil.rmtree(self.destination, ignore_errors=True)

    def time_unpack_sdist_many_files(self, state: dict[str, str]) -> None:
        untar_file(state["sdist"], str(self.destination))


class UnzipWheelManyFiles(ManyFilesArchive):
    def setup(self, state: dict[str, str]) -> None:
        self.destination = Path.cwd() / "uv-many-files-wheel"
        shutil.rmtree(self.destination, ignore_errors=True)

    def time_unzip_wheel_many_files(self, state: dict[str, str]) -> None:
        unzip_file(state["wheel"], str(self.destination), flatten=False)


class PrepareWheelManyFiles(ManyFilesArchive):
    def setup(self, state: dict[str, str]) -> None:
        self.destination = Path.cwd() / "uv-many-files-prepare"
        shutil.rmtree(self.destination, ignore_errors=True)

    def time_prepare_wheel_many_files(self, state: dict[str, str]) -> None:
        unzip_file(state["wheel"], str(self.destination), flatten=False)
        with zipfile.ZipFile(state["wheel"]) as archive:
            validate_wheel(archive, "manyfiles")


class InstallWheelManyFiles(ManyFilesArchive):
    def setup(self, state: dict[str, str]) -> None:
        self.destination = Path.cwd() / "uv-many-files-install"
        shutil.rmtree(self.destination, ignore_errors=True)
        target = InstallTarget.from_options("manyfiles", target=str(self.destination))
        self.installer = WheelInstaller(target, pycompile=False)

    def time_install_wheel_many_files(self, state: dict[str, str]) -> None:
        self.installer.install(state["wheel"])
