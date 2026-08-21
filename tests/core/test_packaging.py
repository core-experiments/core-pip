from __future__ import annotations

import pytest
from cpip.core.packaging import (
    InvalidVersion,
    Requirement,
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


class TestVersionComparisonDispatch:
    """Version's comparators coerce strings and version-shaped objects
    (anything whose str() parses -- notably another packaging library's
    Version), while types whose instances fail coercion are remembered so
    later comparisons skip straight to reflected dispatch. The resolver's
    range-arithmetic infinity sentinels previously paid a str() + full
    regex parse attempt + raised-and-caught InvalidVersion per bound
    comparison before reaching that same dispatch.
    """

    def test_sentinel_comparisons_defer_to_the_sentinel(self) -> None:
        from cpip._vendor.nab_resolver.ranges import (
            NEGATIVE_INFINITY,
            POSITIVE_INFINITY,
        )

        version = Version("1.2.3")

        assert (version == NEGATIVE_INFINITY) is False
        assert (version != POSITIVE_INFINITY) is True
        # Ordering against a sentinel on the right previously *raised*
        # InvalidVersion (the coercion in the ordering methods was
        # uncaught); reflected dispatch makes it just work.
        assert (version < POSITIVE_INFINITY) is True
        assert (version > NEGATIVE_INFINITY) is True
        assert (version <= POSITIVE_INFINITY) is True
        assert (version >= NEGATIVE_INFINITY) is True
        assert (NEGATIVE_INFINITY < version) is True
        assert (POSITIVE_INFINITY > version) is True

    def test_string_coercion_still_works(self) -> None:
        version = Version("1.2.3")

        assert version == "1.2.3"
        assert version == "1.2.3.0"
        assert version != "1.2.4"
        assert version < "2.0"
        assert version > "1.0"
        assert version <= "1.2.3"
        assert version >= "1.2.3"

    def test_invalid_string_equality_is_false_not_an_error(self) -> None:
        assert (Version("1.2.3") == "not a version") is False

    def test_invalid_string_ordering_still_raises(self) -> None:
        with pytest.raises(InvalidVersion):
            Version("1.2.3") < "not a version"  # noqa: B015

    def test_unrelated_types_are_unequal(self) -> None:
        assert (Version("1.2.3") == object()) is False

        assert (Version("1.2.3") == None) is False  # noqa: E711

    def test_cross_library_version_objects_compare_by_value(self) -> None:
        """Another packaging library's Version (whose str() is a valid
        version) must keep comparing by value -- the regression CI caught
        when coercion was briefly restricted to plain strings: the
        lazy-wheel path compares a real `packaging` Version against ours.
        """
        from packaging.version import Version as PackagingVersion

        ours = Version("0.782")

        theirs = PackagingVersion("0.782")

        assert (ours == theirs) is True
        assert (ours != PackagingVersion("0.783")) is True
        assert (ours < PackagingVersion("1.0")) is True
        assert (ours >= PackagingVersion("0.5")) is True

    def test_failed_coercion_cache_does_not_leak(self) -> None:
        """A cached failed coercion must keep giving the same answers on
        repeat and must not affect coercible operands."""
        from cpip._vendor.nab_resolver.ranges import NEGATIVE_INFINITY

        version = Version("1.2.3")

        # Prime the cache with the sentinel's string form, twice.
        assert (version == NEGATIVE_INFINITY) is False
        assert (version == NEGATIVE_INFINITY) is False

        # Coercible operands are unaffected.
        from packaging.version import Version as PackagingVersion

        assert (version == PackagingVersion("1.2.3")) is True
        assert version == "1.2.3"

    def test_mixed_parseability_type_is_judged_per_instance(self) -> None:
        """A type whose instances only sometimes stringify to a version
        must be judged per instance: one unparseable instance must not
        poison a later parseable one (the failure cache is keyed on the
        string form, not the operand's type).
        """

        class Moody:
            def __init__(self, text: str) -> None:
                self.text = text

            def __str__(self) -> str:
                return self.text

        version = Version("1.2.3")

        assert (version == Moody("not a version")) is False
        assert (version == Moody("1.2.3")) is True
        assert (version == Moody("not a version")) is False
        assert (version == Moody("1.2.4")) is False
        assert (version < Moody("2.0")) is True


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


def test_parse_requirement_shares_specifier_sets_by_text() -> None:
    from cpip.core import packaging as packaging_module

    packaging_module._specifier_sets.clear()
    parse_requirement.cache_clear()
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
    from cpip.core import packaging as packaging_module

    packaging_module._specifier_sets.clear()
    parse_requirement.cache_clear()
    limit = packaging_module._SPECIFIER_SET_CACHE_SIZE
    for index in range(limit + 5):
        parse_requirement(f"pkg-{index}>={index}")
    assert len(packaging_module._specifier_sets) <= limit
    # Entries evicted by the sweep are simply rebuilt; requirements already
    # parsed keep the instances they were given.
    assert parse_requirement("pkg-0>=0").specifier == SpecifierSet(">=0")


def test_shared_specifier_set_contains_cache_is_bounded() -> None:
    from cpip.core import packaging as packaging_module

    packaging_module._specifier_sets.clear()
    parse_requirement.cache_clear()
    shared = parse_requirement("pkg-a>=1").specifier
    assert shared is parse_requirement("pkg-b>=1").specifier
    limit = packaging_module._CONTAINS_CACHE_SIZE
    for index in range(limit + 50):
        assert shared.contains(Version(f"1.{index}"))
        assert not shared.contains(Version(f"0.{index}"))
    assert len(shared._contains_cache) <= limit
    # Answers survive the sweep.
    assert shared.contains(Version("1.0"))
    assert not shared.contains(Version("0.1"))


def test_specifier_clauses_share_one_version_per_text() -> None:
    from cpip.core import packaging as packaging_module

    packaging_module._versions_by_text.clear()
    packaging_module._specifier_sets.clear()
    parse_requirement.cache_clear()
    pinned = parse_requirement("a==1.1.0").specifier.specifiers[0]
    floor = parse_requirement("b>=1.1.0").specifier.specifiers[0]
    assert pinned._parsed_version is floor._parsed_version
    assert pinned._parsed_version == Version("1.1.0")
    # Wildcards validate the prefix but keep no parsed version, as before.
    wildcard = parse_requirement("c==1.1.*").specifier.specifiers[0]
    assert wildcard._parsed_version is None
    with pytest.raises(InvalidVersion, match="not-a-version"):
        parse_requirement("d==not-a-version")
    assert "not-a-version" not in packaging_module._versions_by_text
    limit = packaging_module._VERSION_CACHE_SIZE
    for index in range(limit + 5):
        parse_requirement(f"pkg-{index}>={index}.0")
    assert len(packaging_module._versions_by_text) <= limit
