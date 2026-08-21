"""Every predicate the codebase derives from a requirement's specifiers, pinned.

The bodies below are verbatim copies of the implementations in use when this
file was written (``_oracle_*``); ``CANDIDATES`` maps each meaning to the
implementation the codebase currently exposes for it. A refactor that gives
each meaning a single canonical home swaps the candidate, never the oracle:
the oracle is the behaviour, the candidate is the code under test.

Three distinct meanings of "exact pin" exist and must stay distinct:

* ``names_exact_version`` -- *some* clause is ``==``/``===`` without a
  wildcard (yank/hash policy: the set admits at most one release).
* ``first_pinned_version`` -- the parsed version of the *first* ``==`` clause.
* ``sole_pinned_version`` -- the parsed version iff the set is exactly one
  ``==`` clause without a wildcard.
"""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    Version,
    is_windows_path,
    parse_requirement,
)

from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.provider import (
    CandidateProvider,
    is_unnamed_direct_requirement_internal,
)
from cpip.resolution.nab_types import _exact_pin

# --- oracles: verbatim copies of the implementations in use at freeze time ---


def _oracle_names_exact_version(requirement: Requirement) -> bool:
    return any(
        spec.operator in {"==", "==="} and not spec.version.endswith(".*")
        for spec in requirement.specifier.specifiers
    )


def _oracle_first_pinned_version(requirement: Requirement) -> Version | None:
    for specifier in requirement.specifier.specifiers:
        if specifier.operator == "==" and not specifier.version.endswith(".*"):
            return specifier.parsed_version
    return None


def _oracle_sole_pinned_version(requirement: Requirement) -> Version | None:
    clauses = requirement.specifier.specifiers
    if len(clauses) != 1:
        return None
    clause = clauses[0]
    if clause.operator != "==" or clause.version.endswith("*"):
        return None
    return clause.parsed_version


def _oracle_explicitly_allows_prereleases(specifier: SpecifierSet) -> bool:
    return any(
        clause.operator != "==="
        and not clause.version.endswith(".*")
        and clause.parsed_version.is_prerelease
        for clause in specifier.specifiers
    )


def _oracle_is_unnamed_direct(requirement: Requirement) -> bool:
    return (
        requirement.url is not None
        or requirement.raw.startswith("file:")
        or requirement.raw.startswith((".", "/", "~"))
        or is_windows_path(requirement.raw)
    )


# --- candidates: what the codebase exposes for each meaning today ---

CANDIDATES: dict[
    str, tuple[Callable[[Requirement], object], Callable[[Requirement], object]]
] = {
    "names_exact_version": (
        _oracle_names_exact_version,
        CandidateEvaluator.is_exact_pin,
    ),
    "first_pinned_version": (
        _oracle_first_pinned_version,
        CandidateProvider.exact_version_internal,
    ),
    "sole_pinned_version": (_oracle_sole_pinned_version, _exact_pin),
    "explicitly_allows_prereleases": (
        lambda requirement: _oracle_explicitly_allows_prereleases(
            requirement.specifier
        ),
        lambda requirement: _current_explicitly_allows_prereleases(
            requirement.specifier
        ),
    ),
    "is_unnamed_direct": (_oracle_is_unnamed_direct, lambda r: r.is_unnamed_direct),
    "is_unnamed_direct (provider)": (
        _oracle_is_unnamed_direct,
        is_unnamed_direct_requirement_internal,
    ),
}


def _current_explicitly_allows_prereleases(specifier: SpecifierSet) -> bool:
    """Today the scan is memoized privately inside SpecifierSet.contains, so
    reach it the way a caller does -- ask about a prerelease with prereleases
    disallowed, which forces the memo -- then read the memo."""
    specifier.contains(Version("0.0.0.dev0"), allow_prereleases=False)
    return bool(specifier._explicitly_allows_prereleases)


# --- generators ---

PIECES_VERSIONS = (
    "1.0",
    "1.0.0",
    "2.1.3",
    "0.9",
    "1.0a1",
    "1.0rc2",
    "2.0.dev3",
    "1.5.post1",
    "3!1.0",
)
OPERATORS = ("==", "!=", "<=", ">=", "<", ">", "~=", "===")


def _random_requirements(rng: random.Random, count: int) -> list[Requirement]:
    out: list[Requirement] = []
    for index in range(count):
        clauses = []
        for _ in range(rng.randint(0, 3)):
            operator = rng.choice(OPERATORS)
            version = rng.choice(PIECES_VERSIONS)
            if operator in ("==", "!=") and rng.random() < 0.3 and "!" not in version:
                version = version.split(".dev")[0].split("a")[0].split("rc")[0] + ".*"
            if operator == "~=" and "." not in version:
                version += ".0"
            clauses.append(operator + version)
        name = rng.choice(("pkg", "Pkg_Name", "a-b"))
        extras = rng.choice(("", "[x]", "[x,y]"))
        text = f"{name}{extras}{','.join(clauses)}"
        if rng.random() < 0.1:
            text = rng.choice(
                (
                    f"{name} @ https://h/{name}-1.0.whl",
                    "./local/path",
                    "/abs/path/pkg.whl",
                    "~/home/pkg",
                    "file:///srv/pkg.whl",
                    "C:\\wheels\\pkg.whl",
                ),
            )
        try:
            out.append(parse_requirement(text))
        except ValueError:
            continue
    return out


CURATED = [
    "pkg",
    "pkg==1.0",
    "pkg===1.0",
    "pkg==1.*",
    "pkg==1.0,<2",
    "pkg==1.0,!=1.1",
    "pkg>=1.0",
    "pkg~=1.0",
    "pkg==1.0a1",
    "pkg>=1.0rc1",
    "pkg>=1.0,<2.0a1",
    "pkg!=1.*",
    "pkg==1.0.*,>=1.0.1",
    "pkg[extra]==2.0",
    "pkg @ https://h/p.whl",
    "./path",
    "/abs",
    "~/x",
    "file:///x",
    "C:\\x\\y",
    "pkg>1.0.dev1",
    "pkg<=1!1.0",
]


@pytest.mark.parametrize("meaning", sorted(CANDIDATES))
def test_predicate_matches_its_oracle(meaning: str) -> None:
    oracle, candidate = CANDIDATES[meaning]
    rng = random.Random(20260821)
    requirements = [parse_requirement(text) for text in CURATED]
    requirements += _random_requirements(rng, 2000)
    assert requirements
    for requirement in requirements:
        assert candidate(requirement) == oracle(requirement), (meaning, requirement.raw)


def test_the_three_pin_meanings_are_distinct() -> None:
    multi = parse_requirement("pkg==1.0,<2")
    assert CANDIDATES["names_exact_version"][0](multi) is True
    assert CANDIDATES["first_pinned_version"][0](multi) == Version("1.0")
    assert CANDIDATES["sole_pinned_version"][0](multi) is None
    arbitrary = parse_requirement("pkg===1.0")
    assert CANDIDATES["names_exact_version"][0](arbitrary) is True
    assert CANDIDATES["first_pinned_version"][0](arbitrary) is None
    assert CANDIDATES["sole_pinned_version"][0](arbitrary) is None
    wildcard = parse_requirement("pkg==1.*")
    assert CANDIDATES["names_exact_version"][0](wildcard) is False
    assert CANDIDATES["sole_pinned_version"][0](wildcard) is None
