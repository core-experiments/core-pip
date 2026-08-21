"""Version is its own sort key: every ordering built around it agrees with it.

The candidate sort keys put the Version itself into their tuples, and the
cached catalog summaries bisect on the key in ``Version.to_wire()``; both must
order exactly as the Versions do.
"""

from __future__ import annotations

import random

from cpip.core.versions import Version
from cpip.index.links import Link
from cpip.index.source_models import CandidateRecord
from packaging.version import Version as TheirVersion


def _random_versions(rng: random.Random, count: int) -> list[Version]:
    texts = []
    for _ in range(count):
        text = ".".join(str(rng.randrange(0, 4)) for _ in range(rng.randint(1, 4)))
        if rng.random() < 0.15:
            text = f"{rng.randrange(0, 3)}!{text}"
        if rng.random() < 0.25:
            text += rng.choice(("a", "b", "rc")) + str(rng.randrange(0, 3))
        if rng.random() < 0.15:
            text += ".post" + str(rng.randrange(0, 3))
        if rng.random() < 0.15:
            text += ".dev" + str(rng.randrange(0, 3))
        if rng.random() < 0.1:
            text += "+" + rng.choice(("local", "ubuntu1", "1", "abc.2"))
        texts.append(text)
    return [Version(text) for text in texts]


def test_version_sorts_like_the_reference_implementation() -> None:
    rng = random.Random(20260820)
    versions = _random_versions(rng, 3000)
    ours = [str(v) for v in sorted(versions)]
    theirs = [str(v) for v in sorted(TheirVersion(str(v)) for v in versions)]
    # Equal versions have no defined relative order; compare by text.
    assert ours == theirs


def _wire_key(version: Version) -> tuple:
    return version.to_wire()[2]


def test_summary_key_orders_like_the_version() -> None:
    rng = random.Random(20260820)
    versions = _random_versions(rng, 3000)
    assert sorted(versions, key=_wire_key) == sorted(versions)
    assert sorted(versions, key=_wire_key, reverse=True) == sorted(
        versions,
        reverse=True,
    )
    # Equal versions ("1.0" and "1.0.0") keep their input order under both.
    pairs = [Version("1.0"), Version("1.0.0"), Version("1"), Version("1.0.0.0")]
    assert [str(v) for v in sorted(pairs, key=_wire_key)] == [
        str(v) for v in sorted(pairs)
    ]


def test_candidate_record_sort_key_orders_like_versions() -> None:
    rng = random.Random(7)
    records = [
        CandidateRecord(
            name="pkg",
            version=version,
            link=Link(f"https://example.invalid/pkg-{version}-py3-none-any.whl"),
        )
        for version in _random_versions(rng, 400)
    ]
    for prefer_binary in (False, True):
        by_key = sorted(records, key=lambda r: r.sort_key(prefer_binary=prefer_binary))
        by_version = sorted(records, key=lambda r: r.version)
        assert [r.version for r in by_key] == [r.version for r in by_version]
