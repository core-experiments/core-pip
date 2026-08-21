"""cpip.core.packaging against the real ``packaging`` library, as a ratchet.

A seeded generator produces version and specifier texts well outside what
real indexes serve (every PEP 440 spelling, leading zeros, ``v`` prefixes,
whitespace, junk suffixes). Each observable -- validity, normalised text,
``is_prerelease``, ``base_version``, pairwise ordering and hashing, and
specifier containment with and without prereleases -- is compared with
``packaging`` and every disagreement is classified by cause.

Two assertions make this a ratchet rather than a snapshot:

* every disagreement must match a cause listed in ``KNOWN_DIVERGENCES`` --
  a new kind of disagreement fails the test with the offending texts;
* every listed cause must still be observed -- a cause that no longer
  reproduces must be deleted from the list, so the list only shrinks.

The causes are documented where they are classified. Most are cpip bugs
relative to PEP 440 that a rewrite of ``Version``/``Specifier`` is expected
to remove; one is a ``packaging`` bug (``~=`` with an unnormalised operand),
and one is a deliberate leniency in cpip's specifier grammar.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cpip.core.packaging import SpecifierSet
from cpip.core.versions import InvalidVersion, Version
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet as TheirSpecifierSet
from packaging.version import InvalidVersion as TheirInvalidVersion
from packaging.version import Version as TheirVersion

SEED = 20260820
VERSION_SAMPLES = 6000
PAIRWISE_SAMPLE = 300
SPECIFIER_SAMPLES = 3000
CONTAINS_PER_SPECIFIER = 6


@dataclass(frozen=True)
class Divergence:
    observable: str
    specifier: str | None
    version: str
    ours: object
    theirs: object


# --- generators ---

PRE_LABELS = ("a", "b", "rc", "c", "alpha", "beta", "pre", "preview")
SEPARATORS = ("", ".", "-", "_")


def version_texts(rng: random.Random, count: int) -> list[str]:
    out: list[str] = []
    for _ in range(count):
        parts = [
            str(rng.randrange(0, 4))
            if rng.random() < 0.9
            else "0" * rng.randint(1, 3) + str(rng.randrange(10))
            for _ in range(rng.randint(1, 4))
        ]
        text = ".".join(parts)
        if rng.random() < 0.15:
            text = f"{rng.randrange(0, 3)}!{text}"
        if rng.random() < 0.3:
            number = str(rng.randrange(0, 3)) if rng.random() < 0.8 else ""
            text += rng.choice(SEPARATORS) + rng.choice(PRE_LABELS) + number
        if rng.random() < 0.2:
            text += rng.choice((".post", "-post", "_post", "-", ".r", ".rev"))
            text += str(rng.randrange(0, 3))
        if rng.random() < 0.15:
            number = str(rng.randrange(0, 3)) if rng.random() < 0.8 else ""
            text += rng.choice((".dev", "-dev", "_dev", "dev")) + number
        if rng.random() < 0.12:
            text += "+" + rng.choice(("local", "ubuntu1", "1", "abc.2", "a-b", "x_y"))
        if rng.random() < 0.08:
            text = "v" + text
        if rng.random() < 0.05:
            text = text.upper()
        if rng.random() < 0.05:
            text = f" {text} "
        if rng.random() < 0.03:
            text += rng.choice(("..", "+", "!", "-", "x"))
        out.append(text)
    return out


OPERATORS = ("==", "!=", "<=", ">=", "<", ">", "~=", "===")


def specifier_texts(rng: random.Random, count: int, versions: list[str]) -> list[str]:
    out: list[str] = []
    for _ in range(count):
        clauses = []
        for _ in range(rng.randint(1, 3)):
            operator = rng.choice(OPERATORS)
            version = rng.choice(versions).strip()
            if operator in ("==", "!=") and rng.random() < 0.3 and "+" not in version:
                version += ".*"
            clauses.append(operator + version)
        out.append(",".join(clauses))
    return out


# --- the comparison ---


def _parse_both(text: str) -> tuple[Version | None, TheirVersion | None, bool]:
    """(ours, theirs, agree_on_validity)."""
    try:
        ours = Version(text)
    except InvalidVersion:
        ours = None
    try:
        theirs = TheirVersion(text)
    except TheirInvalidVersion:
        theirs = None
    return ours, theirs, (ours is None) == (theirs is None)


def _order(a: Any, b: Any) -> tuple[bool, bool, bool]:
    return (a < b, a == b, a > b)


def _normalized_specifier(text: str) -> str | None:
    """The same specifier with every operand in packaging's canonical form,
    or None when an operand cannot be normalised (wildcards, ``===``)."""
    clauses = []
    for clause in text.split(","):
        match = re.match(r"(===|==|!=|<=|>=|<|>|~=)(.*)", clause)
        assert match is not None
        operator, operand = match.groups()
        if operator == "===" or operand.endswith(".*"):
            return None
        clauses.append(operator + str(TheirVersion(operand)))
    return ",".join(clauses)


def collect_divergences() -> list[Divergence]:
    rng = random.Random(SEED)
    divergences: list[Divergence] = []
    texts = version_texts(rng, VERSION_SAMPLES)
    parsed: dict[str, tuple[Version, TheirVersion]] = {}
    for text in texts:
        ours, theirs, agree = _parse_both(text)
        if not agree:
            divergences.append(
                Divergence("validity", None, text, ours is not None, theirs is not None)
            )
            continue
        if ours is None or theirs is None:
            continue
        parsed[text] = (ours, theirs)
        for observable, mine, yours in (
            ("str", str(ours), str(theirs)),
            ("is_prerelease", ours.is_prerelease, theirs.is_prerelease),
            ("base_version", ours.base_version, theirs.base_version),
        ):
            if mine != yours:
                divergences.append(Divergence(observable, None, text, mine, yours))

    sample = list(parsed.items())[:PAIRWISE_SAMPLE]
    for index, (text_a, (ours_a, theirs_a)) in enumerate(sample):
        for text_b, (ours_b, theirs_b) in sample[index:]:
            mine, yours = _order(ours_a, ours_b), _order(theirs_a, theirs_b)
            if mine != yours:
                divergences.append(Divergence("ordering", text_a, text_b, mine, yours))
            if ours_a == ours_b and (hash(ours_a) == hash(ours_b)) != (
                hash(theirs_a) == hash(theirs_b)
            ):
                divergences.append(Divergence("hash", text_a, text_b, None, None))

    operands = list(parsed)[:400]
    for text in specifier_texts(rng, SPECIFIER_SAMPLES, operands):
        try:
            theirs_set = TheirSpecifierSet(text)
        except InvalidSpecifier:
            theirs_set = None
        try:
            ours_set = SpecifierSet(text)
        except ValueError:
            ours_set = None
        if (ours_set is None) != (theirs_set is None):
            divergences.append(
                Divergence(
                    "specifier_validity",
                    text,
                    "",
                    ours_set is not None,
                    theirs_set is not None,
                )
            )
            continue
        if ours_set is None or theirs_set is None:
            continue
        # packaging >= 24 admits prereleases by default when nothing else
        # matches; cpip's allow_prereleases=False is packaging's "only when a
        # clause names one", which is what SpecifierSet.prereleases reports.
        modes = ((False, bool(theirs_set.prereleases)), (True, True))
        for _ in range(CONTAINS_PER_SPECIFIER):
            candidate = rng.choice(operands)
            ours_version, theirs_version = parsed[candidate]
            for allow, prereleases in modes:
                mine = ours_set.contains(ours_version, allow_prereleases=allow)
                yours = theirs_set.contains(theirs_version, prereleases=prereleases)
                if mine != yours:
                    divergences.append(
                        Divergence(
                            f"contains(allow={allow})", text, candidate, mine, yours
                        )
                    )
    return divergences


# --- classification ---


def _packaging_disagrees_with_its_normalised_self(d: Divergence) -> bool:
    if d.specifier is None or not d.observable.startswith("contains"):
        return False
    normalised = _normalized_specifier(d.specifier)
    if normalised is None:
        return False
    allow = d.observable.endswith("True)")
    theirs_set = TheirSpecifierSet(normalised)
    prereleases = True if allow else bool(theirs_set.prereleases)
    return (
        theirs_set.contains(TheirVersion(d.version), prereleases=prereleases) == d.ours
    )


KNOWN_DIVERGENCES: dict[str, Callable[[Divergence], bool]] = {
    # cpip's specifier grammar accepts texts packaging rejects: local labels
    # on ordering/compatible operators, "v" prefixes and leading zeros in
    # operands, r/rev post spellings, wildcards after a prerelease, and ``~=``
    # with a single release segment. Validity only -- both agree on
    # everything that both accept.
    "specifier grammar is more lenient than packaging": lambda d: (
        d.observable == "specifier_validity" and d.ours is True and d.theirs is False
    ),
    # packaging computes the ``~=`` prefix from the operand text as written,
    # so "~=v2.1" and "~=2.0001" match nothing; cpip computes it from the
    # parsed release. cpip is right here.
    "packaging's ~= prefix is taken from the unnormalised operand": (
        _packaging_disagrees_with_its_normalised_self
    ),
}


def classify(divergence: Divergence) -> str | None:
    for cause, matches in KNOWN_DIVERGENCES.items():
        if matches(divergence):
            return cause
    return None


def test_every_divergence_from_packaging_has_a_known_cause() -> None:
    divergences = collect_divergences()
    unknown = [d for d in divergences if classify(d) is None]
    assert not unknown, f"{len(unknown)} unexplained divergences, first: {unknown[:10]}"


def test_every_known_cause_still_reproduces() -> None:
    observed = {classify(d) for d in collect_divergences()}
    stale = sorted(set(KNOWN_DIVERGENCES) - observed)
    assert not stale, f"no longer observed, delete from KNOWN_DIVERGENCES: {stale}"


def test_agreement_is_the_common_case() -> None:
    """The ratchet is only meaningful if the generator mostly produces texts
    both libraries agree on; guard against a generator drifting into junk."""
    divergences = collect_divergences()
    assert len(divergences) < 0.2 * (
        VERSION_SAMPLES + SPECIFIER_SAMPLES * CONTAINS_PER_SPECIFIER
    )
