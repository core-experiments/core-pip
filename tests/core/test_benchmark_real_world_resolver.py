from __future__ import annotations

from pathlib import Path

from benchmarks.real_world_resolver import SIZES, create_cases
from pip.resolution.fast_local_wheelhouse import resolve


def test_real_world_resolver_cases_have_expected_outcomes(tmp_path: Path) -> None:
    cases = create_cases(tmp_path)

    for case in cases.values():
        for size in SIZES:
            state = case[size]
            plan = resolve(
                [str(state["wheelhouse"])],
                list(state["requirements"]),
                cache_dir=str(tmp_path / f"cache-{state['wheelhouse'].name}"),
            )
            assert (plan is not None) is state["expected"]
