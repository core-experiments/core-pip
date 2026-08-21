from __future__ import annotations

import pytest
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    canonicalize_name,
    canonicalize_requirement,
    marker_applies,
    parse_requirement,
)
from cpip.core.versions import InvalidVersion, Version


def test_canonicalize_name() -> None:
    assert canonicalize_name("Demo_Pkg.Name") == "demo-pkg-name"


def test_parse_requirement_with_extras_specifier_and_marker() -> None:
    requirement = parse_requirement('Demo-Pkg[PDF,SSL]>=1.0; python_version >= "3.11"')

    assert requirement.name == "Demo-Pkg"
    assert requirement.canonical_name == "demo-pkg"
    assert requirement.extras == {"pdf", "ssl"}
    assert requirement.marker == 'python_version >= "3.11"'
    assert requirement.is_satisfied_by("1.2")


def test_unconstrained_requirement_preserves_prerelease_filtering() -> None:
    requirement = parse_requirement("demo-pkg")

    assert requirement.is_satisfied_by("1.0rc1")
    assert not requirement.is_satisfied_by("1.0rc1", allow_prereleases=False)


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


def test_requirement_cache_state_roundtrip() -> None:
    original = parse_requirement(
        'Demo-Pkg[PDF,SSL]~=1.2,!=1.5; python_version >= "3.11"',
    )
    state = original.cache_state_internal()

    restored = Requirement.from_cache_state(state)
    restored_again = Requirement.from_cache_state(state)

    assert restored_again is restored
    assert restored == original
    assert restored.canonical_name == "demo-pkg"
    assert restored.is_satisfied_by("1.4")
    assert not restored.is_satisfied_by("1.5")


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
    "raw",
    ["1.2.3", "1!2.0rc1.post2.dev3+linux-x86_64", "0.0.0"],
)
def test_version_cache_state_roundtrip(raw: str) -> None:
    original = Version(raw)
    restored = Version.from_cache_state(original.cache_state_internal())
    restored_again = Version.from_cache_state(original.cache_state_internal())

    assert restored == original
    assert restored_again is restored
    assert hash(restored) == hash(original)
    assert str(restored) == str(original)
    assert restored.release == original.release
    assert restored.is_prerelease == original.is_prerelease


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


def test_specifier_set_bounds_are_memoized() -> None:
    specifier = SpecifierSet(">=1,<2")
    first = specifier.bounds()
    assert specifier.bounds() is first


def test_empty_specifier_set_preserves_prerelease_filtering() -> None:
    specifier = SpecifierSet()

    assert specifier.contains("1.0")
    assert not specifier.contains("1.0rc1")
    assert specifier.contains("1.0rc1", allow_prereleases=True)


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
    specifier: str,
    version: str,
    expected: bool,
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
    version: str,
    requires_python: str,
    expected: bool,
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


class TestVersionIsItsOwnKey:
    """Version is a tuple whose elements are its PEP 440 ordering key: it
    compares only with other Versions, in C, and is immutable and interned.
    """

    def test_sentinel_comparisons_defer_to_the_sentinel(self) -> None:
        from cpip._vendor.nab_resolver.ranges import (
            NEGATIVE_INFINITY,
            POSITIVE_INFINITY,
        )

        version = Version("1.2.3")

        assert (version == NEGATIVE_INFINITY) is False
        assert (version != POSITIVE_INFINITY) is True
        assert (version < POSITIVE_INFINITY) is True
        assert (version > NEGATIVE_INFINITY) is True
        assert (version <= POSITIVE_INFINITY) is True
        assert (version >= NEGATIVE_INFINITY) is True
        assert (NEGATIVE_INFINITY < version) is True
        assert (POSITIVE_INFINITY > version) is True

    def test_never_compares_with_text(self) -> None:
        version = Version("1.2.3")

        assert (version == "1.2.3") is False
        assert (version != "1.2.3") is True
        with pytest.raises(TypeError):
            version < "2.0"  # noqa: B015
        with pytest.raises(TypeError):
            version >= "1.0"  # noqa: B015

    def test_unrelated_types_are_unequal(self) -> None:
        assert (Version("1.2.3") == object()) is False
        assert (Version("1.2.3") == None) is False  # noqa: E711

    def test_is_frozen(self) -> None:
        version = Version("1.2.3")

        with pytest.raises(AttributeError):
            version.public = "9"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            del version.release
        with pytest.raises(AttributeError):
            version.anything = 1  # type: ignore[attr-defined]

    def test_is_interned_by_text(self) -> None:
        assert Version("1.2.3") is Version("1.2.3")
        # Equal texts that differ in spelling are equal, not identical.
        assert Version("1.2.3") == Version("1.2.3.0")
        assert Version("1.2.3") is not Version("1.2.3.0")

    def test_equal_versions_share_dict_slots(self) -> None:
        table = {Version("1.0"): "a"}
        assert table[Version("1.0.0")] == "a"
        assert table[Version("1")] == "a"
        assert len({Version("1.0"), Version("1.0.0"), Version("1.0+x")}) == 2

    def test_copies_and_pickles_are_the_same_value(self) -> None:
        import copy
        import pickle

        version = Version("1!2.0rc1.post2.dev3+linux-x86_64")

        assert copy.copy(version) is version
        assert copy.deepcopy(version) is version
        restored = pickle.loads(pickle.dumps(version))
        assert restored == version
        assert str(restored) == str(version)

    def test_zero_version_is_the_shared_sentinel(self) -> None:
        from cpip.core.versions import ZERO_VERSION

        assert ZERO_VERSION == Version("0")
        assert ZERO_VERSION < Version("0.0.1")
        assert not ZERO_VERSION.is_prerelease

    def test_derived_fields(self) -> None:
        version = Version("1!2.0rc1.post2.dev3+Linux_x86-64")

        assert version.epoch == 1
        assert version.release == (2, 0)
        assert version.local == "linux.x86.64"
        assert version.public == "1!2.0rc1.post2.dev3+linux.x86.64"
        assert version.base_version == "1!2.0"
        assert version.is_prerelease
        assert Version("1.0.post1").is_prerelease is False
        assert Version("1.0.post1.dev0").is_prerelease is True
        assert Version("1.0").local is None
        assert Version("1.0").base_version == "1.0"

    def test_bare_dev_and_pre_segments_mean_zero(self) -> None:
        assert str(Version("1.0.dev")) == "1.0.dev0"
        assert Version("1.0.dev") == Version("1.0.dev0")
        assert Version("1.0.dev").is_prerelease
        assert Version("1.0-dev") < Version("1.0a0")
        assert str(Version("1.0a")) == "1.0a0"
        assert str(Version("1.0.post")) == "1.0.post0"

    def test_key_internal_keeps_the_three_element_form_without_local(self) -> None:
        assert Version("1.2").key_internal() == (0, (1, 2), (3, 0, 0, 0, 1, 0))
        assert Version("1.2+a").key_internal() == (
            0,
            (1, 2),
            (3, 0, 0, 0, 1, 0),
            ((0, "a"),),
        )
        assert type(Version("1.2").key_internal()) is tuple


class TestSplitMarker:
    """split_marker's fast paths (no semicolon; no quote before the first
    semicolon) must agree exactly with the quote-aware character walk.
    """

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("botocore==1.28.50", ("botocore==1.28.50", None)),
            ("pkg[extra]>=1.0", ("pkg[extra]>=1.0", None)),
            ("", ("", None)),
            ("pkg ; python_version >= '3.8'", ("pkg", "python_version >= '3.8'")),
            ('pkg ; python_version >= "3.8"', ("pkg", 'python_version >= "3.8"')),
            ("pkg;extra == 'feature'", ("pkg", "extra == 'feature'")),
            # A quoted ";" before the real separator must not split early --
            # the quote-aware walk's whole reason to exist.
            (
                "pkg @ https://x/y?q=';' ; python_version >= '3.8'",
                ("pkg @ https://x/y?q=';'", "python_version >= '3.8'"),
            ),
            # A ";" inside quotes with no separator after it: no split.
            ("pkg=='a;b'", ("pkg=='a;b'", None)),
        ],
    )
    def test_split(self, value: str, expected: tuple[str, str | None]) -> None:
        from cpip.core.packaging import split_marker

        assert split_marker(value) == expected


# The intern tables behind parse_requirement are an implementation detail;
# the tests below observe them only through these two helpers so that a
# relocation of the tables touches one place.


def _table_sizes() -> dict[str, int]:
    from cpip.core import packaging as packaging_module
    from cpip.core import versions as versions_module

    return {
        "specifier_sets": len(packaging_module._specifier_sets),
        "versions": len(versions_module._versions),
    }


def _table_limits() -> dict[str, int]:
    from cpip.core import packaging as packaging_module
    from cpip.core import versions as versions_module

    return {
        "specifier_sets": packaging_module._SPECIFIER_SET_CACHE_SIZE,
        "versions": versions_module._VERSIONS_LIMIT,
        "contains": packaging_module._CONTAINS_CACHE_SIZE,
    }


def _contains_cache_size(specifier: SpecifierSet) -> int:
    return len(specifier._contains_cache)


def test_parse_requirement_shares_specifier_sets_by_text() -> None:
    first = parse_requirement("leaf-0>=1.1.0,<2")
    second = parse_requirement("leaf-1>=1.1.0,<2")
    third = parse_requirement("leaf-2>=1.1.0")
    assert first.specifier is second.specifier
    assert first.specifier is not third.specifier
    assert first.specifier == SpecifierSet(">=1.1.0,<2")
    assert (
        parse_requirement("bare-a").specifier is parse_requirement("bare-b").specifier
    )
    assert not parse_requirement("bare-a").specifier.specifiers
    # Sharing changes nothing about the answers.
    assert first.specifier.contains(Version("1.5"))
    assert not second.specifier.contains(Version("2.0"))
    assert str(first.specifier) == str(second.specifier)


def test_specifier_set_intern_table_is_bounded() -> None:
    limit = _table_limits()["specifier_sets"]
    for index in range(limit + 5):
        parse_requirement(f"pkg-{index}>={index}")
    assert _table_sizes()["specifier_sets"] <= limit
    # Entries evicted by the sweep are simply rebuilt; requirements already
    # parsed keep the instances they were given.
    assert parse_requirement("pkg-0>=0").specifier == SpecifierSet(">=0")


def test_shared_specifier_set_contains_cache_is_bounded() -> None:
    shared = parse_requirement("pkg-a>=1").specifier
    assert shared is parse_requirement("pkg-b>=1").specifier
    limit = _table_limits()["contains"]
    for index in range(limit + 50):
        assert shared.contains(Version(f"1.{index}"))
        assert not shared.contains(Version(f"0.{index}"))
    assert _contains_cache_size(shared) <= limit
    # Answers survive the sweep.
    assert shared.contains(Version("1.0"))
    assert not shared.contains(Version("0.1"))


def test_specifier_clauses_share_one_version_per_text() -> None:
    pinned = parse_requirement("a==1.1.0").specifier.specifiers[0]
    floor = parse_requirement("b>=1.1.0").specifier.specifiers[0]
    assert pinned.parsed_version is floor.parsed_version
    assert pinned.parsed_version == Version("1.1.0")
    # A wildcard validates its prefix and answers by prefix.
    wildcard = parse_requirement("c==1.1.*").specifier
    assert wildcard.contains(Version("1.1.5"))
    assert not wildcard.contains(Version("1.2"))
    with pytest.raises(InvalidVersion, match="not-a-version"):
        parse_requirement("d==not-a-version")
    limit = _table_limits()["versions"]
    for index in range(limit + 5):
        parse_requirement(f"pkg-{index}>={index}.0")
    assert _table_sizes()["versions"] <= limit
