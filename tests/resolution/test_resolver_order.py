from __future__ import annotations

from pathlib import Path

import pytest
from cpip.index.provider import CandidateProvider
from cpip.resolution.resolver import Resolver

from .wheel_helpers import make_wheel


def _make_sat_graph(wheelhouse: Path, order: tuple[str, ...]) -> None:
    variables = 4
    for index in range(variables):
        for value in (0, 1):
            make_wheel(
                wheelhouse,
                f"variable-{index}",
                f"variable_{index}",
                f"{value}.0.0",
            )
    assignments = range(1 << variables)
    forbidden = [assignment for assignment in assignments if assignment != 0]
    for clause_index, forbidden_assignment in enumerate(forbidden):
        version = 1
        for assignment in assignments:
            if assignment == forbidden_assignment:
                continue
            requires = [
                f"variable-{index} == {(assignment >> index) & 1}.0.0"
                for index in range(variables)
            ]
            make_wheel(
                wheelhouse,
                f"clause-{clause_index}",
                f"clause_{clause_index}",
                f"{version}.0.0",
                requires=requires,
            )
            version += 1
    make_wheel(
        wheelhouse,
        "sat-root",
        "sat_root",
        "1.0.0",
        requires=list(order),
    )


@pytest.mark.parametrize(
    "order_kind", ["forward", "reverse", "rotated", "striped"]
)
def test_resolution_is_order_invariant_under_candidate_activity_perturbation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order_kind: str
) -> None:
    names = tuple(
        [f"variable-{index}" for index in range(4)]
        + [f"clause-{index}" for index in range(15)]
    )
    orders = {
        "forward": names,
        "reverse": tuple(reversed(names)),
        "rotated": names[1:] + names[:1],
        "striped": names[::2] + names[1::2],
    }
    wheelhouse = tmp_path / order_kind
    wheelhouse.mkdir()
    _make_sat_graph(wheelhouse, orders[order_kind])
    resolver = Resolver(
        provider=CandidateProvider.from_options(
            find_links=[wheelhouse.as_posix()], no_index=True
        ),
        ignore_installed=True,
        compute_source_hashes=False,
    )
    original_bump = resolver.bump_conflict_activity
    bumps = 0

    def perturb_activity(*names: str) -> None:
        nonlocal bumps
        original_bump(*names)
        bumps += 1
        if bumps % 256 == 0:
            resolver.conflict_activity[:] = [
                (activity + 1) // 2 for activity in resolver.conflict_activity
            ]

    monkeypatch.setattr(resolver, "bump_conflict_activity", perturb_activity)

    plan = resolver.resolve(["sat-root"])

    assert len(plan.candidates) == 20
