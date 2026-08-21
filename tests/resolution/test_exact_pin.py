"""_exact_pin hands back the Specifier's own parsed Version."""

from __future__ import annotations

import pytest
from cpip.core.packaging import Version, parse_requirement
from cpip.resolution.nab_types import _exact_pin


def test_exact_pin_reuses_the_specifiers_parsed_version() -> None:
    requirement = parse_requirement("pkg==1.2.3.post1")
    pinned = _exact_pin(requirement)
    assert pinned == Version("1.2.3.post1")
    # The same object the Specifier parsed at construction -- no re-parse.
    assert pinned is requirement.specifier.specifiers[0].parsed_version


@pytest.mark.parametrize(
    "text",
    [
        "pkg",
        "pkg>=1.0",
        "pkg==1.*",
        "pkg==1.0,!=1.1",
        "pkg===1.0",
        "pkg~=1.0",
        "pkg[extra]>=1,<2",
    ],
)
def test_exact_pin_is_none_for_anything_but_a_single_equality(text: str) -> None:
    assert _exact_pin(parse_requirement(text)) is None
