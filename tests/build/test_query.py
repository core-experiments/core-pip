"""Malformed installed metadata must not take down a query command."""

from __future__ import annotations

from cpip.build.query import (
    PackageDetails,
    check_package_set,
    marker_allows,
    package_set_from_dependencies,
)
from cpip.core.packaging import Version, parse_requirement


class FakeDistribution:
    """The narrow slice of InstalledMetadataDistribution these helpers touch."""

    def __init__(self, name: str, version: str) -> None:
        self.canonical_name = name
        self.raw_name = name
        self.raw_version = version


def test_marker_allows_respects_non_extra_markers() -> None:
    """A platform marker must be evaluated, not assumed true."""

    impossible = parse_requirement('pkg; sys_platform == "definitely-not-a-platform"')

    assert not marker_allows(impossible, frozenset())


def test_marker_allows_keeps_unconditional_dependencies() -> None:
    assert marker_allows(parse_requirement("pkg"), frozenset())


def test_marker_allows_selects_by_extra() -> None:
    requirement = parse_requirement('pkg; extra == "test"')

    assert marker_allows(requirement, frozenset({"test"}))
    assert not marker_allows(requirement, frozenset({"docs"}))
    assert not marker_allows(requirement, frozenset())


def test_package_set_keeps_distributions_with_unparseable_versions() -> None:
    """A legacy version is still installed; dropping it invents a conflict."""

    distributions = [FakeDistribution("broken", "not a version")]

    package_set = package_set_from_dependencies(
        distributions,  # type: ignore[arg-type]
        {"broken": []},
    )

    assert "broken" in package_set
    assert package_set["broken"].version is None


def test_unparseable_version_is_not_reported_as_missing() -> None:
    package_set = {
        "app": PackageDetails(Version("1.0"), (parse_requirement("broken>=2"),)),
        "broken": PackageDetails(None, ()),
    }

    missing, conflicting = check_package_set(package_set)

    assert missing == {}
    assert conflicting == {}


def test_version_conflicts_are_still_reported() -> None:
    package_set = {
        "app": PackageDetails(Version("1.0"), (parse_requirement("dep>=2"),)),
        "dep": PackageDetails(Version("1.0"), ()),
    }

    missing, conflicting = check_package_set(package_set)

    assert missing == {}
    assert list(conflicting) == ["app"]
