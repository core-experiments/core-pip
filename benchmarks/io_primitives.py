"""Benchmarks for local index discovery and requirements-file parsing."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import cast

from pip.index.directory_index import DirectoryIndex
from pip.network.http import NetworkSession
from pip.resolution.req_file import parse_requirements

INDEX_COUNTS = (10, 100, 1_000, 10_000)
REQUIREMENT_COUNTS = (10, 100, 1_000, 10_000)
INDEX_MODES = ("wheels", "mixed")
REQUIREMENT_MODES = ("flat", "nested", "constraints")


def _wheel_name(index: int) -> str:
    return f"index-bench-{index:05}-1.0-py3-none-any.whl"


def create_index_workload(root: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for count in INDEX_COUNTS:
        for mode in INDEX_MODES:
            path = root / f"{count}-{mode}"
            path.mkdir(parents=True)
            for index in range(count):
                (path / _wheel_name(index)).touch()
            if mode == "mixed":
                (path / "index-bench-archive-1.0.tar.gz").touch()
                (path / "simple.html").write_text("<html></html>", encoding="utf-8")
                (path / "not-a-distribution.txt").touch()
            paths[f"{count}-{mode}"] = os.fspath(path)
    return paths


def create_requirements_workload(root: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for count in REQUIREMENT_COUNTS:
        for mode in REQUIREMENT_MODES:
            path = root / f"{count}-{mode}.txt"
            lines = [f"index-bench-{index}>=1,<4" for index in range(count)]
            if mode == "flat":
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            elif mode == "nested":
                child = path.with_name(f"{path.stem}-child.txt")
                child.write_text("\n".join(lines) + "\n", encoding="utf-8")
                path.write_text(f"-r {child.name}\n", encoding="utf-8")
            else:
                constraints = path.with_name(f"{path.stem}-constraints.txt")
                constraints.write_text("\n".join(lines) + "\n", encoding="utf-8")
                path.write_text(
                    f"-c {constraints.name}\n" + "\n".join(lines) + "\n",
                    encoding="utf-8",
                )
            paths[f"{count}-{mode}"] = os.fspath(path)
    return paths


class DirectoryIndexScaling:
    """Measure scanning local wheelhouses at increasing file counts."""

    params = (
        tuple(str(count) for count in INDEX_COUNTS),
        INDEX_MODES,
    )
    param_names = ("file_count", "mode")
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 300

    @staticmethod
    def setup_cache() -> dict[str, object]:
        root = Path.cwd() / "directory-index-workload"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {"paths": create_index_workload(root)}

    def setup(self, state: dict[str, object], file_count: str, mode: str) -> None:
        paths = cast(dict[str, str], state["paths"])
        self.path = paths[f"{file_count}-{mode}"]

    def time_scan(self, state: dict[str, object], file_count: str, mode: str) -> None:
        DirectoryIndex(self.path).scan()


class RequirementsFileParsing:
    """Measure parsing flat files and files that include other files."""

    params = (
        tuple(str(count) for count in REQUIREMENT_COUNTS),
        REQUIREMENT_MODES,
    )
    param_names = ("requirement_count", "mode")
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 300

    @staticmethod
    def setup_cache() -> dict[str, object]:
        root = Path.cwd() / "requirements-file-workload"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {"paths": create_requirements_workload(root)}

    def setup(
        self, state: dict[str, object], requirement_count: str, mode: str
    ) -> None:
        paths = cast(dict[str, str], state["paths"])
        self.path = paths[f"{requirement_count}-{mode}"]
        self.session = NetworkSession()

    def time_parse(
        self, state: dict[str, object], requirement_count: str, mode: str
    ) -> None:
        parse_requirements(self.path, self.session)
