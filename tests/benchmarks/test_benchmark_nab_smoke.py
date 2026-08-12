"""Port of nab's offline deterministic smoke suite.

Source: ``nab-python/benchmarks/deterministic_smoke.py`` and
``nab-python/benchmarks/smoke/{fixture,scenarios}.toml`` in
https://github.com/notatallshaw/nab -- the upstream project
``cpip._vendor.nab_resolver`` is vendored from. Every resolve in that suite
is checked for its exact pins, not just wall time; these benchmarks preserve
that discipline.

Of nab's 11 smoke scenarios, 4 have no cpip equivalent and are intentionally
not ported:

* ``strategy-lowest`` / ``strategy-lowest-direct`` -- cpip's
  :class:`~cpip.resolution.api.ResolutionEngine` has no "prefer lowest
  candidate" resolution-strategy knob; it always walks candidates
  highest-first.
* ``universal-aligned`` / ``universal-independent`` -- cpip has no
  cross-target (multi-Python/multi-platform) "universal" resolve mode.

nab also resolves against an arbitrary configured target Python version
independent of the host interpreter; cpip's marker evaluation
(``cpip.core.packaging.default_environment``) always reads the actual
running interpreter. ``test_nab_smoke_extra_and_python_marker`` accounts for
that by computing its marker-gated expectation from ``sys.version_info``
instead of hardcoding nab's Python-3.11 assumption.
"""

from __future__ import annotations

import sys
from pathlib import Path

from benchmark_support import reset_caches
from cpip.core.errors import ResolutionError
from cpip.index.provider import CandidateProvider
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.models import ResolutionResult
from pytest_codspeed import BenchmarkFixture


def resolve(
    wheelhouse: Path,
    requirements: list[str],
    *,
    constraints: list[str] | None = None,
) -> ResolutionResult:
    reset_caches()
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
        constraints=constraints or [],
    )
    return resolver.resolve(requirements)


def pins(result: ResolutionResult) -> dict[str, str]:
    return {
        candidate.canonical_name: str(candidate.version)
        for candidate in result.candidates
    }


def test_nab_smoke_basic_highest(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    def resolve_basic() -> dict[str, str]:
        return pins(resolve(nab_smoke_wheelhouse, ["nab-smoke-basic"]))

    assert benchmark(resolve_basic) == {
        "nab-smoke-basic": "1.0.0",
        "nab-smoke-basic-leaf": "2.0.0",
    }


def test_nab_smoke_constraint_ceiling(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    def resolve_constrained() -> dict[str, str]:
        return pins(
            resolve(
                nab_smoke_wheelhouse,
                ["nab-smoke-constrained>=1.0.0"],
                constraints=["nab-smoke-constrained<3.0.0"],
            ),
        )

    assert benchmark(resolve_constrained) == {"nab-smoke-constrained": "2.0.0"}


def test_nab_smoke_extra_and_python_marker(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    marker_leaf_version = "1.0.0" if sys.version_info < (3, 12) else "2.0.0"

    def resolve_extra() -> dict[str, str]:
        return pins(resolve(nab_smoke_wheelhouse, ["nab-smoke-extra-app[speed]"]))

    assert benchmark(resolve_extra) == {
        "nab-smoke-extra-app": "1.0.0",
        "nab-smoke-extra-base": "1.0.0",
        "nab-smoke-extra-speed": "1.0.0",
        "nab-smoke-marker-leaf": marker_leaf_version,
    }


def test_nab_smoke_strategy_highest(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    requirements = ["nab-smoke-strategy-app==1.0.0", "nab-smoke-strategy-direct>=1.0.0"]

    def resolve_strategy() -> dict[str, str]:
        return pins(resolve(nab_smoke_wheelhouse, requirements))

    assert benchmark(resolve_strategy) == {
        "nab-smoke-strategy-app": "1.0.0",
        "nab-smoke-strategy-direct": "2.0.0",
        "nab-smoke-strategy-transitive": "2.0.0",
    }


def test_nab_smoke_pip_deep_backtracking(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    def resolve_backtracking() -> dict[str, str]:
        return pins(resolve(nab_smoke_wheelhouse, ["nab-smoke-pip-a"]))

    assert benchmark(resolve_backtracking) == {
        "nab-smoke-pip-a": "1.0.0",
        "nab-smoke-pip-b": "1.0.0",
        "nab-smoke-pip-c": "1.0.0",
    }


def test_nab_smoke_pip_deep_backtracking_unsatisfiable(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    def resolve_unsatisfiable() -> int:
        reset_caches()
        try:
            resolve(nab_smoke_wheelhouse, ["nab-smoke-pip-unsat-a"])
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError(
            "pip-deep-backtracking-unsatisfiable unexpectedly resolved"
        )

    assert benchmark(resolve_unsatisfiable) > 0


def test_nab_smoke_deep_backjump(
    benchmark: BenchmarkFixture,
    nab_smoke_wheelhouse: Path,
) -> None:
    requirements = ["nab-smoke-backjump-pivot", "nab-smoke-backjump-link-1"]

    def resolve_backjump() -> dict[str, str]:
        return pins(resolve(nab_smoke_wheelhouse, requirements))

    assert benchmark(resolve_backjump) == {
        "nab-smoke-backjump-pivot": "1.0.0",
        "nab-smoke-backjump-zgate": "1.0.0",
        "nab-smoke-backjump-link-1": "4.0.0",
        "nab-smoke-backjump-link-2": "4.0.0",
        "nab-smoke-backjump-link-3": "4.0.0",
        "nab-smoke-backjump-link-4": "4.0.0",
        "nab-smoke-backjump-link-5": "4.0.0",
        "nab-smoke-backjump-link-6": "4.0.0",
        "nab-smoke-backjump-alt-1": "5.0.0",
        "nab-smoke-backjump-alt-2": "5.0.0",
        "nab-smoke-backjump-alt-3": "5.0.0",
        "nab-smoke-backjump-alt-4": "5.0.0",
        "nab-smoke-backjump-alt-5": "5.0.0",
        "nab-smoke-backjump-alt-6": "5.0.0",
    }
