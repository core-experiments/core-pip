from __future__ import annotations

import pytest
from cpip.core.packaging import (
    SpecifierSet,
    Version,
    canonicalize_name,
    canonicalize_requirement,
    marker_applies,
    parse_requirement,
)


def test_canonicalize_name() -> None:
    assert canonicalize_name("Demo_Pkg.Name") == "demo-pkg-name"


def test_parse_requirement_with_extras_specifier_and_marker() -> None:
    requirement = parse_requirement('Demo-Pkg[PDF,SSL]>=1.0; python_version >= "3.11"')

    assert requirement.name == "Demo-Pkg"
    assert requirement.canonical_name == "demo-pkg"
    assert requirement.extras == {"pdf", "ssl"}
    assert requirement.marker == 'python_version >= "3.11"'
    assert requirement.is_satisfied_by("1.2")


def test_standard_requirement_skips_url_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_url_parse(value: str) -> None:
        raise AssertionError(f"parsed colon-free requirement as a URL: {value}")

    monkeypatch.setattr("cpip.core.packaging.urllib.parse.urlparse", fail_url_parse)

    requirement = parse_requirement("demo-pkg>=1")

    assert requirement.name == "demo-pkg"


def test_parse_requirement_reuses_immutable_result() -> None:
    parse_requirement.cache_clear()

    first = parse_requirement("demo-pkg>=1")
    second = parse_requirement("demo-pkg>=1")

    assert second is first
    cache = parse_requirement.cache_info()
    assert cache.misses == 1
    assert cache.hits == 1


def test_canonicalize_requirement() -> None:
    assert (
        canonicalize_requirement('Demo_Pkg[SSL,PDF] >= 1.0; python_version >= "3.11"')
        == 'demo-pkg[pdf,ssl]>=1.0; python_version >= "3.11"'
    )


def test_version_orders_prerelease_before_final() -> None:
    assert Version("1.0rc1") < Version("1.0")
    assert Version("1.0") < Version("1.0.post1")


def test_version_comparison_ignores_trailing_release_zeros() -> None:
    assert Version("1.3") == Version("1.3.0")
    assert SpecifierSet("==1.3").contains("1.3.0")


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("3.2.3-2", "3.2.3.post2"),
        ("1.0.post", "1.0.post0"),
        ("5.0.0.b1", "5.0.0b1"),
        ("6.0.0.rc1", "6.0.0rc1"),
        ("1!2.0.dev1+linux-x86_64", "1!2.0.dev1+linux.x86.64"),
    ],
)
def test_version_accepts_pep440_separator_forms(raw: str, normalized: str) -> None:
    assert str(Version(raw)) == normalized


def test_version_orders_epoch_and_dev_releases() -> None:
    assert Version("1!1.0") > Version("2.0")
    assert Version("1.0.dev1") < Version("1.0a1.dev1") < Version("1.0a1")


@pytest.mark.parametrize(
    "specifier, expected_lower, expected_upper",
    [
        (">=1,<2", (Version("1"), True), (Version("2"), False)),
        (">1,<=2", (Version("1"), False), (Version("2"), True)),
        ("==1.2", (Version("1.2"), True), (Version("1.2"), True)),
        ("~=1.2", (Version("1.2"), True), (Version("2"), False)),
        ("!=1.5", None, None),
        ("==1.*", None, None),
    ],
)
def test_specifier_set_bounds(
    specifier: str,
    expected_lower: tuple[Version, bool] | None,
    expected_upper: tuple[Version, bool] | None,
) -> None:
    assert SpecifierSet(specifier).bounds() == (expected_lower, expected_upper)


@pytest.mark.parametrize(
    "specifier, version, expected",
    [
        ("==5.0.*", "5.0.1", True),
        ("==5.0.*", "5.1", False),
        ("!=5.0.*", "5.0.1", False),
        ("!=5.0.*", "5.1", True),
    ],
)
def test_wildcard_specifier_contains(
    specifier: str, version: str, expected: bool
) -> None:
    assert SpecifierSet(specifier).contains(version) is expected


@pytest.mark.parametrize(
    "version, requires_python, expected",
    [
        ("3.6.5", "== 3.6.4", False),
        ("3.6.5", "== 3.6.5", True),
        ("3.6.5", "", True),
    ],
)
def test_requires_python_specifier_oracle(
    version: str, requires_python: str, expected: bool
) -> None:
    assert SpecifierSet(requires_python).contains(version) is expected


def test_invalid_requires_python_specifier_oracle() -> None:
    with pytest.raises(ValueError, match="invalid version specifier"):
        SpecifierSet("invalid")


def test_requirement_attribute_oracle() -> None:
    requirement = parse_requirement("affinegap==1.10")

    assert requirement.name == "affinegap"
    assert requirement.url is None
    assert requirement.extras == frozenset()
    assert str(requirement.specifier) == "==1.10"
    assert requirement.marker is None


@pytest.mark.parametrize(
    "url, name, specifier",
    [
        (
            "https://example.com/packages/INITools-0.3.tar.gz",
            "INITools",
            "==0.3",
        ),
        (
            "https://example.com/packages/demo_pkg-1.2-py3-none-any.whl",
            "demo-pkg",
            "==1.2",
        ),
    ],
)
def test_parse_bare_direct_archive_reference_infers_name_and_version(
    url: str,
    name: str,
    specifier: str,
) -> None:
    requirement = parse_requirement(url)

    assert requirement.name == name
    assert str(requirement.specifier) == specifier
    assert requirement.url == url


def test_marker_applies_respects_parenthesized_extra_marker() -> None:
    requirement = parse_requirement("backports-zstd>=1.0.0; (extra == 'zstd')")

    assert marker_applies(requirement.marker, extras=()) is False
    assert marker_applies(requirement.marker, extras=("zstd",)) is True
