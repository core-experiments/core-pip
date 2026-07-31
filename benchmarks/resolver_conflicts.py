"""Cold resolver benchmarks with deliberately conflicting version graphs."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cpip.index.provider import CandidateProvider
from cpip.resolution.resolver import Resolver

from .cache_materialization import make_wheel_internal


SIZES = (20, 40, 80, 256)


def create_conflict_graph(root: Path, size: int) -> Path:
    wheelhouse = root / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        version = f"{index}.0.0"
        dependencies = [f"B == {version}"]
        if index != 1:
            dependencies.append(f"C == {index - 1}.0.0")
        make_wheel_internal(
            wheelhouse,
            "A",
            version,
            requires=tuple(dependencies),
        )
        make_wheel_internal(
            wheelhouse,
            "B",
            version,
            requires=(f"C == {version}",),
        )
        make_wheel_internal(wheelhouse, "C", version)
    return wheelhouse


def create_range_conflict_graph(root: Path, size: int) -> Path:
    wheelhouse = root / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        version = f"{index}.0.0"
        dependencies = [f"B >= {index}, < {index + 1}"]
        if index != 1:
            dependencies.append(f"C >= {index - 1}, < {index}")
        make_wheel_internal(
            wheelhouse,
            "A",
            version,
            requires=tuple(dependencies),
        )
        make_wheel_internal(
            wheelhouse,
            "B",
            version,
            requires=(f"C >= {index}, < {index + 1}",),
        )
        make_wheel_internal(wheelhouse, "C", version)
    return wheelhouse


def create_project_chain(root: Path, size: int) -> Path:
    wheelhouse = root / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(size):
        dependencies = () if index == size - 1 else (f"chain-{index + 1} == 1.0",)
        make_wheel_internal(
            wheelhouse,
            f"chain-{index}",
            "1.0",
            requires=dependencies,
        )
    return wheelhouse


class ColdConflictResolution:
    params = SIZES
    param_names = ("versions",)
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 180

    @staticmethod
    def setup_cache() -> dict[int, str]:
        root = Path.cwd() / "resolver-conflicts"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {size: os.fspath(create_conflict_graph(root, size)) for size in SIZES}

    def time_resolve(self, wheelhouses: dict[int, str], versions: int) -> None:
        provider = CandidateProvider.from_options(
            find_links=[wheelhouses[versions]],
            no_index=True,
        )
        plan = Resolver(provider=provider, ignore_installed=True).resolve(["A"])
        assert {candidate.canonical_name for candidate in plan.candidates} == {
            "a",
            "b",
            "c",
        }


class ColdRangeConflictResolution(ColdConflictResolution):
    @staticmethod
    def setup_cache() -> dict[int, str]:
        root = Path.cwd() / "resolver-range-conflicts"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {
            size: os.fspath(create_range_conflict_graph(root, size)) for size in SIZES
        }


class WarmConflictResolution(ColdConflictResolution):
    """Measure repeated resolution after populating the wheel metadata cache."""

    def setup(self, wheelhouses: dict[int, str], versions: int) -> None:
        cache = Path(wheelhouses[versions]).parent / "cache"
        cache.mkdir(exist_ok=True)
        self.provider = CandidateProvider.from_options(
            find_links=[wheelhouses[versions]],
            no_index=True,
            wheel_cache_dir=cache,
        )
        Resolver(provider=self.provider, ignore_installed=True).resolve(["A"])

    def time_resolve(self, wheelhouses: dict[int, str], versions: int) -> None:
        del wheelhouses
        plan = Resolver(provider=self.provider, ignore_installed=True).resolve(["A"])
        assert {candidate.canonical_name for candidate in plan.candidates} == {
            "a",
            "b",
            "c",
        }


class ColdProjectChainResolution(ColdConflictResolution):
    @staticmethod
    def setup_cache() -> dict[int, str]:
        root = Path.cwd() / "resolver-project-chain"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {size: os.fspath(create_project_chain(root, size)) for size in SIZES}

    def time_resolve(self, wheelhouses: dict[int, str], versions: int) -> None:
        provider = CandidateProvider.from_options(
            find_links=[wheelhouses[versions]],
            no_index=True,
        )
        plan = Resolver(provider=provider, ignore_installed=True).resolve(["chain-0"])
        assert len(plan.candidates) == versions
