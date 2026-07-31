"""Deterministic worst-case-shaped benchmarks for the general resolver.

These cases intentionally exercise candidate ordering and deep rejection. They
are metadata-only wheelhouses, so results measure resolver behavior rather than
network or build-system variance.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from cpip.core.errors import ResolutionError
from cpip.index.provider import CandidateProvider
from cpip.resolution.resolver import Resolver

from .cache_materialization import make_wheel_internal


SIZES = (32, 128, 512)
SAT_SIZES = (3, 4, 5)


def create_late_conflict_graph(root: Path, size: int) -> Path:
    wheelhouse = root / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        version = f"{index}.0.0"
        # Every candidate forces a different B version. Only A's oldest
        # candidate is compatible with the root's exact B requirement.
        make_wheel_internal(
            wheelhouse,
            "A",
            version,
            requires=(f"B == {version}", f"C == {version}"),
        )
        make_wheel_internal(wheelhouse, "B", version)
        make_wheel_internal(wheelhouse, "C", version)
    make_wheel_internal(wheelhouse, "root-b", "1.0.0", requires=("B == 1.0.0",))
    make_wheel_internal(wheelhouse, "root", "1.0.0", requires=("A", "root-b == 1.0.0"))
    return wheelhouse


def create_shared_dependency_graph(root: Path, size: int) -> Path:
    wheelhouse = root / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(size):
        name = f"leaf-{index}"
        make_wheel_internal(
            wheelhouse,
            name,
            "1.0.0",
            requires=("shared >= 1, < 2",),
        )
    make_wheel_internal(wheelhouse, "shared", "1.0.0")
    requires = tuple(f"leaf-{index} == 1.0.0" for index in range(size))
    make_wheel_internal(wheelhouse, "root", "1.0.0", requires=requires)
    return wheelhouse


def create_sat_graph(root: Path, variables: int, *, omit: int | None) -> Path:
    """Encode bounded SAT as package candidates and exact dependencies.

    Each clause package has one candidate for every assignment except its
    forbidden assignment. Requiring all clause packages is therefore
    satisfiable iff at least one global variable assignment was not forbidden.
    """

    wheelhouse = root / str(variables)
    wheelhouse.mkdir(parents=True)
    for index in range(variables):
        for value in (0, 1):
            make_wheel_internal(wheelhouse, f"variable-{index}", f"{value}.0.0")
    assignments = range(1 << variables)
    forbidden = [assignment for assignment in assignments if assignment != omit]
    for clause_index, forbidden_assignment in enumerate(forbidden):
        clause = f"clause-{clause_index}"
        candidate_version = 1
        for assignment in assignments:
            if assignment == forbidden_assignment:
                continue
            dependencies = tuple(
                f"variable-{index} == {(assignment >> index) & 1}.0.0"
                for index in range(variables)
            )
            make_wheel_internal(
                wheelhouse,
                clause,
                f"{candidate_version}.0.0",
                requires=dependencies,
            )
            candidate_version += 1
    make_wheel_internal(
        wheelhouse,
        "sat-root",
        "1.0.0",
        requires=tuple(
            [f"variable-{index}" for index in range(variables)]
            + [f"clause-{index}" for index in range(len(forbidden))]
        ),
    )
    return wheelhouse


class GeneralResolverAdversarial:
    params = SIZES
    param_names = ("packages",)
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 180

    @staticmethod
    def _setup(root_name: str, creator: Callable[[Path, int], Path]) -> dict[int, str]:
        root = Path.cwd() / root_name
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {size: os.fspath(creator(root, size)) for size in SIZES}

    def resolve(self, wheelhouses: dict[int, str], packages: int, root: str) -> int:
        provider = CandidateProvider.from_options(
            find_links=[wheelhouses[packages]], no_index=True
        )
        plan = Resolver(
            provider=provider, ignore_installed=True, compute_source_hashes=False
        ).resolve([root])
        return len(plan.candidates)


class LateConflictResolution(GeneralResolverAdversarial):
    @staticmethod
    def setup_cache() -> dict[int, str]:
        return GeneralResolverAdversarial._setup(
            "resolver-late-conflicts", create_late_conflict_graph
        )

    def time_resolve(self, wheelhouses: dict[int, str], packages: int) -> None:
        assert self.resolve(wheelhouses, packages, "root") == 5


class SharedDependencyResolution(GeneralResolverAdversarial):
    @staticmethod
    def setup_cache() -> dict[int, str]:
        return GeneralResolverAdversarial._setup(
            "resolver-shared-dependencies", create_shared_dependency_graph
        )

    def time_resolve(self, wheelhouses: dict[int, str], packages: int) -> None:
        provider = CandidateProvider.from_options(
            find_links=[wheelhouses[packages]], no_index=True
        )
        plan = Resolver(
            provider=provider, ignore_installed=True, compute_source_hashes=False
        ).resolve(["root"])
        assert len(plan.candidates) == packages + 2


class SeededResolution(GeneralResolverAdversarial):
    """Measure repeated resolution using the previous successful candidates."""

    @staticmethod
    def setup_cache() -> dict[int, str]:
        return GeneralResolverAdversarial._setup(
            "resolver-seeded", create_shared_dependency_graph
        )

    def setup(self, wheelhouses: dict[int, str], packages: int) -> None:
        self.resolver = Resolver(
            provider=CandidateProvider.from_options(
                find_links=[wheelhouses[packages]], no_index=True
            ),
            ignore_installed=True,
            compute_source_hashes=False,
        )
        self.resolver.resolve(["root"])

    def time_resolve(self, wheelhouses: dict[int, str], packages: int) -> None:
        del wheelhouses
        plan = self.resolver.resolve(["root"])
        assert len(plan.candidates) == packages + 2

    def time_resolve_changed(self, wheelhouses: dict[int, str], packages: int) -> None:
        del wheelhouses
        plan = self.resolver.resolve(["root", "leaf-0==1.0.0"])
        assert len(plan.candidates) == packages + 2


class SatResolution:
    params = SAT_SIZES
    param_names = ("variables",)
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 180

    @staticmethod
    def setup_cache() -> dict[str, dict[int, str]]:
        root = Path.cwd() / "resolver-sat"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return {
            "sat": {
                size: os.fspath(create_sat_graph(root / "sat", size, omit=0))
                for size in SAT_SIZES
            },
            "unsat": {
                size: os.fspath(create_sat_graph(root / "unsat", size, omit=None))
                for size in SAT_SIZES
            },
        }

    def time_satisfiable(
        self, wheelhouses: dict[str, dict[int, str]], variables: int
    ) -> None:
        provider = CandidateProvider.from_options(
            find_links=[wheelhouses["sat"][variables]], no_index=True
        )
        plan = Resolver(
            provider=provider, ignore_installed=True, compute_source_hashes=False
        ).resolve(["sat-root"])
        assert plan.candidates

    def time_unsatisfiable(
        self, wheelhouses: dict[str, dict[int, str]], variables: int
    ) -> None:
        provider = CandidateProvider.from_options(
            find_links=[wheelhouses["unsat"][variables]], no_index=True
        )
        try:
            Resolver(
                provider=provider, ignore_installed=True, compute_source_hashes=False
            ).resolve(["sat-root"])
        except ResolutionError:
            return
        raise AssertionError("SAT benchmark unexpectedly resolved")
