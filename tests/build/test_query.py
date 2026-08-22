"""Malformed installed metadata must not take down a query command."""

from __future__ import annotations

from typing import Any

from cpip.build.query import (
    PackageDetails,
    _dependent_index,
    check_package_set,
    marker_allows,
    package_set_from_dependencies,
)
from cpip.core.packaging import canonicalize_name, parse_requirement
from cpip.core.versions import Version


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


class DependentDistribution:
    """The slice of an installed distribution that ``_dependent_index`` reads."""

    def __init__(self, raw_name: str, dependencies: list[str]) -> None:
        self.raw_name = raw_name
        self.canonical_name = canonicalize_name(raw_name)
        self.dependencies = dependencies
        self.dependency_reads = 0

    def iter_dependencies(self) -> list[Any]:
        self.dependency_reads += 1
        if self.dependencies == ["<unreadable>"]:
            raise ValueError("unparseable dependency metadata")
        return [parse_requirement(item) for item in self.dependencies]


def test_dependent_index_groups_by_canonical_name() -> None:
    """Dependents are matched on the canonical name and keep install order."""

    distributions = [
        DependentDistribution("First", ["Shared_Dep>=1"]),
        DependentDistribution("second", ["unrelated"]),
        DependentDistribution("Third", ["SHARED-DEP", "First"]),
    ]

    dependents, unavailable = _dependent_index(distributions)  # type: ignore[arg-type]

    assert not unavailable
    assert dependents["shared-dep"] == ["First", "Third"]
    assert dependents["first"] == ["Third"]
    assert "second" not in dependents


def test_dependent_index_reads_each_distribution_once() -> None:
    """The index replaces a per-query rescan of the whole environment."""

    distributions = [
        DependentDistribution("first", ["shared"]),
        DependentDistribution("second", ["shared"]),
    ]

    _dependent_index(distributions)  # type: ignore[arg-type]

    assert [item.dependency_reads for item in distributions] == [1, 1]


def test_dependent_index_reports_unreadable_metadata() -> None:
    """One unparseable distribution makes every answer ``#N/A``, as before."""

    distributions = [
        DependentDistribution("first", ["shared"]),
        DependentDistribution("broken", ["<unreadable>"]),
    ]

    dependents, unavailable = _dependent_index(distributions)  # type: ignore[arg-type]

    assert unavailable
    assert dependents == {}


def test_unsupported_distributions_computes_tags_only_when_needed(tmp_path) -> None:  # noqa: ANN001
    from cpip.build.query import unsupported_distributions
    from cpip.core.light_metadata import LightDistributionStore
    from cpip.core.target_python import get_supported

    def wheel(name: str, *tags: str) -> None:
        info = tmp_path / f"{name}-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Name: {name}\nVersion: 1.0\n")
        (info / "WHEEL").write_text(
            "Wheel-Version: 1.0\n" + "".join(f"Tag: {tag}\n" for tag in tags)
        )

    wheel("pure", "py3-none-any")
    wheel("both", "py2-none-any", "py3-none-any")
    wheel("notags")
    calls: list[int] = []

    def supported():  # noqa: ANN202
        calls.append(1)
        return get_supported()

    distributions = LightDistributionStore(paths=[str(tmp_path)]).iter()
    assert unsupported_distributions(distributions, supported) == []
    assert calls == []

    wheel("foreign", "cp27-cp27m-manylinux1_x86_64")
    wheel(
        "native",
        *(f"{t.interpreter}-{t.abi}-{t.platform}" for t in get_supported()[:1]),
    )
    distributions = LightDistributionStore(paths=[str(tmp_path)]).iter()
    unsupported = unsupported_distributions(distributions, supported)
    assert [dist.raw_name for dist in unsupported] == ["foreign"]
    assert calls == [1]
