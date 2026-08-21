"""Every predicate the codebase derives from a requirement's specifiers, pinned.

The bodies below are verbatim copies of the implementations in use when this
file was written (``_oracle_*``); ``CANDIDATES`` maps each meaning to the
implementation the codebase currently exposes for it. A refactor that gives
each meaning a single canonical home swaps the candidate, never the oracle:
the oracle is the behaviour, the candidate is the code under test.

Two distinct meanings of "exact pin" exist and must stay distinct:

* ``names_exact_version`` -- *some* clause is ``==``/``===`` without a
  wildcard (yank/hash policy: the set admits at most one release); this is
  ``SpecifierSet.is_pinned``.
* ``sole_pinned_version`` -- the parsed version iff the set is exactly one
  ``==`` clause without a wildcard; this is ``SpecifierSet.exact_version``.

A third, "the first ``==`` clause's version", used to pick the catalog
release for a requirement; it folded into ``exact_version`` (a multi-clause
pin now takes the general path).
"""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    is_windows_path,
    parse_requirement,
)
from cpip.core.versions import Version


# --- oracles: verbatim copies of the implementations in use at freeze time ---


def _oracle_names_exact_version(requirement: Requirement) -> bool:
    return any(
        spec.operator in {"==", "==="} and not spec.version.endswith(".*")
        for spec in requirement.specifier.specifiers
    )


def _oracle_sole_pinned_version(requirement: Requirement) -> Version | None:
    clauses = requirement.specifier.specifiers
    if len(clauses) != 1:
        return None
    clause = clauses[0]
    if clause.operator != "==" or clause.version.endswith("*"):
        return None
    return clause.parsed_version


def _oracle_explicitly_allows_prereleases(specifier: SpecifierSet) -> bool:
    # PEP 440 (and pip): a clause naming a prerelease opts the set in, except
    # ``!=`` (excluding a prerelease is not asking for them) and ``===``; a
    # wildcard's prefix counts. The pre-rewrite scan also counted ``!=`` and
    # skipped wildcards; the packaging-library oracle pinned the difference.
    return any(
        clause.operator not in ("===", "!=")
        and Version(
            clause.version[:-2] if clause.version.endswith(".*") else clause.version
        ).is_prerelease
        for clause in specifier.specifiers
    )


def _oracle_is_unnamed_direct(requirement: Requirement) -> bool:
    return (
        requirement.url is not None
        or requirement.raw.startswith("file:")
        or requirement.raw.startswith((".", "/", "~"))
        or is_windows_path(requirement.raw)
    )


# --- candidates: the canonical home of each meaning ---

CANDIDATES: dict[
    str, tuple[Callable[[Requirement], object], Callable[[Requirement], object]]
] = {
    "names_exact_version": (
        _oracle_names_exact_version,
        lambda r: r.specifier.is_pinned,
    ),
    "sole_pinned_version": (
        _oracle_sole_pinned_version,
        lambda r: r.specifier.exact_version,
    ),
    "explicitly_allows_prereleases": (
        lambda r: _oracle_explicitly_allows_prereleases(r.specifier),
        lambda r: r.specifier.explicitly_allows_prereleases,
    ),
    "is_unnamed_direct": (_oracle_is_unnamed_direct, lambda r: r.is_unnamed_direct),
}


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
    assert CANDIDATES["sole_pinned_version"][0](multi) is None
    arbitrary = parse_requirement("pkg===1.0")
    assert CANDIDATES["names_exact_version"][0](arbitrary) is True
    assert CANDIDATES["sole_pinned_version"][0](arbitrary) is None
    wildcard = parse_requirement("pkg==1.*")
    assert CANDIDATES["names_exact_version"][0](wildcard) is False
    assert CANDIDATES["sole_pinned_version"][0](wildcard) is None
