from __future__ import annotations

import platform
import sys
import zipfile
from pathlib import Path

import pytest
from cpip.core.errors import InstallationError
from cpip.core.utils import CURRENT_PYTHON_VERSION_DIGITS
from cpip.core.wheel import (
    TargetContext,
    WheelTag,
    parse_wheel_file,
    parse_wheel_filename,
    supported_wheel_tags,
    wheel_candidate,
    wheel_tag_rank,
)


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("simple-1.1.1-py2-none-any.whl", ("simple", "1.1.1")),
        ("simple-1.1-py2.py3-abi1.abi2-any.whl", ("simple", "1.1")),
        ("simple-1.1-4-py2-none-any.whl", ("simple", "1.1")),
        ("simple-1-py2-none-any.whl", ("simple", "1")),
        ("complex_dist-0.1-py2.py3-none-any.whl", ("complex-dist", "0.1")),
    ],
)
def test_parse_wheel_filename_oracle(filename: str, expected: tuple[str, str]) -> None:
    assert parse_wheel_filename(filename) == expected


def test_parse_wheel_file_multi_tag_oracle() -> None:
    wheel = parse_wheel_file("simple-1.1-py2.py3-abi1.abi2-any.whl")

    assert wheel is not None
    assert wheel.name == "simple"
    assert str(wheel.version) == "1.1"
    assert wheel.build_tag is None
    assert set(wheel.tags) == {
        WheelTag("py2", "abi1", "any"),
        WheelTag("py2", "abi2", "any"),
        WheelTag("py3", "abi1", "any"),
        WheelTag("py3", "abi2", "any"),
    }


def test_parse_wheel_file_interns_versions_and_tags() -> None:
    first = parse_wheel_file("first-1.0-py3-none-any.whl")
    second = parse_wheel_file("second-1.0-py3-none-any.whl")

    assert first is not None
    assert second is not None
    assert first.version is second.version
    assert first.tags is second.tags


def test_parse_wheel_file_build_tag_oracle() -> None:
    wheel = parse_wheel_file("simple-1.1-4-py2-none-any.whl")

    assert wheel is not None
    assert wheel.build_tag == "4"
    assert wheel.tags == (WheelTag("py2", "none", "any"),)


def test_wheel_tag_rank_oracle() -> None:
    supported = (
        WheelTag("py2", "none", "TEST"),
        WheelTag("py2", "TEST", "any"),
        WheelTag("py2", "none", "any"),
    )
    any_wheel = parse_wheel_file("simple-0.1-py2-none-any.whl")
    test_wheel = parse_wheel_file("simple-0.1-py2-none-TEST.whl")

    assert any_wheel is not None
    assert test_wheel is not None
    assert wheel_tag_rank(any_wheel.tags, supported) == 2
    assert wheel_tag_rank(test_wheel.tags, supported) == 0
    assert wheel_tag_rank(any_wheel.tags, ()) is None


def test_wheel_tag_rank_reuses_compatibility_result() -> None:
    candidate = (WheelTag("py3", "none", "any"),)
    supported = (WheelTag("py3", "none", "any"),)
    wheel_tag_rank.cache_clear()

    assert wheel_tag_rank(candidate, supported) == 0
    assert wheel_tag_rank(candidate, supported) == 0

    cache = wheel_tag_rank.cache_info()
    assert cache.misses == 1
    assert cache.hits == 1


def test_supported_wheel_tags_target_context_oracle() -> None:
    tags = supported_wheel_tags(
        TargetContext(
            platforms=("linux_x86_64",),
            implementation="cp",
            python_version="3.11",
            abis=("cp311",),
        ),
    )

    assert WheelTag("cp311", "cp311", "linux_x86_64") in tags
    assert WheelTag("py3", "cp311", "any") in tags


@pytest.mark.parametrize(
    "runtime,wheel,expected",
    [
        ("macosx_13_0_arm64", "macosx_11_0_arm64", True),
        ("macosx_13_0_arm64", "macosx_13_0_universal2", True),
        ("macosx_13_0_arm64", "macosx_14_0_arm64", False),
        ("macosx_13_0_x86_64", "macosx_13_0_universal2", False),
    ],
)
def test_wheel_tag_rank_macos_platform_oracle(
    runtime: str,
    wheel: str,
    expected: bool,
) -> None:
    supported = (WheelTag("cp311", "cp311", runtime),)
    candidate = (WheelTag("cp311", "cp311", wheel),)

    assert (wheel_tag_rank(candidate, supported) is not None) is expected


@pytest.mark.parametrize(
    "filename",
    [
        "simple-_invalid_-py2-none-any.whl",
        "Cython-cp27-none-linux_x86_64.whl",
        "invalid.whl",
        "simple-0.1_1-py2-none-any.whl",
        "six-1.16.0_build1-py3-none-any.whl",
    ],
)
def test_parse_wheel_filename_rejects_invalid_oracle(filename: str) -> None:
    assert parse_wheel_filename(filename) is None


def test_current_macos_accepts_newer_arm64_wheel() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("macOS arm64 wheel compatibility test")
    wheel = parse_wheel_file(
        "demo-1.0-cp"
        f"{CURRENT_PYTHON_VERSION_DIGITS}-cp{CURRENT_PYTHON_VERSION_DIGITS}"
        "-macosx_12_0_arm64.whl",
    )
    assert wheel is not None
    assert wheel_tag_rank(wheel.tags) is not None


def test_wheel_candidate_rejects_invalid_filename_oracle(tmp_path: Path) -> None:
    wheel = tmp_path / "invalid.whl"
    wheel.write_bytes(b"not a wheel")

    with pytest.raises(InstallationError, match="Invalid wheel filename"):
        wheel_candidate(wheel)


def test_wheel_candidate_reuses_metadata_across_extras(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "\n".join(
                (
                    "Name: demo",
                    "Version: 1.0",
                    "Requires-Dist: base",
                    "Requires-Dist: optional; extra == 'feature'",
                    "Provides-Extra: feature",
                    "",
                ),
            ),
        )
    base = wheel_candidate(wheel)
    monkeypatch.setattr(
        "cpip.core.wheel.read_metadata_message_internal",
        lambda *args_internal, **kwargs_internal: pytest.fail("metadata was reparsed"),
    )

    feature = wheel_candidate(wheel, {"feature"})

    assert [item.name for item in base.dependencies] == ["base"]
    assert [item.name for item in feature.dependencies] == ["base", "optional"]


def test_wheel_tag_refuses_mutation() -> None:
    """The cached hash and lowercase forms only hold if a tag cannot change.

    A tag lives in sets and dictionary keys -- ``supported_wheel_tags`` and
    ``wheel_tag_rank``'s cache both depend on it -- so a rewritten field would
    leave the hash pointing at the old value and silently corrupt lookups.
    """
    tag = WheelTag("py3", "none", "any")

    for attribute in ("interpreter", "abi", "platform", "_hash"):
        with pytest.raises(AttributeError, match="immutable"):
            setattr(tag, attribute, "changed")

    with pytest.raises(AttributeError, match="immutable"):
        del tag.interpreter

    assert tag == WheelTag("py3", "none", "any")
    assert hash(tag) == hash(WheelTag("py3", "none", "any"))


def test_wheel_tag_hash_matches_equality() -> None:
    """Equal tags must be interchangeable as dictionary keys."""
    first = WheelTag("cp312", "cp312", "macosx_11_0_arm64")
    second = WheelTag("cp312", "cp312", "macosx_11_0_arm64")
    other = WheelTag("cp312", "cp312", "manylinux_2_17_x86_64")

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second, other}) == 2
    assert {first: "value"}[second] == "value"


def test_target_context_refuses_mutation() -> None:
    """The cached hash only holds if a target cannot change.

    A target is looked up through ``supported_wheel_tags``'s unbounded
    cache, so a rewritten field would leave the hash pointing at the old
    value while ``__eq__`` reflected the new one, corrupting the cache.
    """
    target = TargetContext(
        platforms=("linux_x86_64",),
        implementation="cp",
        python_version="3.11",
        abis=("cp311",),
    )

    for attribute in ("platforms", "implementation", "python_version", "abis", "_hash"):
        with pytest.raises(AttributeError, match="immutable"):
            setattr(target, attribute, "changed")

    with pytest.raises(AttributeError, match="immutable"):
        del target.platforms

    assert target == TargetContext(
        platforms=("linux_x86_64",),
        implementation="cp",
        python_version="3.11",
        abis=("cp311",),
    )


def test_target_context_hash_matches_equality() -> None:
    """Equal targets must be interchangeable as dictionary/cache keys."""
    first = TargetContext(platforms=("linux_x86_64",), implementation="cp")
    second = TargetContext(platforms=("linux_x86_64",), implementation="cp")
    other = TargetContext(platforms=("macosx_11_0_arm64",), implementation="cp")

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second, other}) == 2
    assert {first: "value"}[second] == "value"


def test_target_context_cached_tag_lookup_is_stable() -> None:
    """Equal-but-distinct targets must not fragment the tag cache."""
    first = TargetContext(platforms=("linux_x86_64",), implementation="cp")
    second = TargetContext(platforms=("linux_x86_64",), implementation="cp")

    assert supported_wheel_tags(first) is supported_wheel_tags(second)


def test_parse_wheel_file_bare_name_matches_path(tmp_path: Path) -> None:
    name = "simple-1.1-4-py2.py3-abi1.abi2-any.whl"
    bare = parse_wheel_file(name)
    assert bare is not None
    assert parse_wheel_file(str(tmp_path / name)) == bare
    assert parse_wheel_file(f"nested/dir/{name}") == bare
    assert parse_wheel_file("nested/dir/") is None


def test_project_wheel_dependencies_marker_filtering() -> None:
    from cpip.core.packaging import parse_requirement
    from cpip.core.versions import Version
    from cpip.core.wheel import WheelResolutionMetadata, project_wheel_dependencies

    plain = WheelResolutionMetadata(
        name="pkg",
        version=Version("1.0"),
        dependencies=tuple(map(parse_requirement, ("a>=1", "b", "c[x]==2"))),
        provided_extras=frozenset(),
        requires_python=None,
    )
    # No markers: every dependency applies for any extras set, and the
    # metadata's own tuple is handed back rather than a filtered copy.
    assert project_wheel_dependencies(plain, None, frozenset()) is plain.dependencies
    assert (
        project_wheel_dependencies(plain, None, frozenset({"x"})) is plain.dependencies
    )

    marked = WheelResolutionMetadata(
        name="pkg",
        version=Version("1.0"),
        dependencies=tuple(
            map(
                parse_requirement,
                ("a", 'b; extra == "fast"', 'c; python_version < "2.0"', "d"),
            ),
        ),
        provided_extras=frozenset({"fast"}),
        requires_python=None,
    )
    assert [r.name for r in project_wheel_dependencies(marked, None, frozenset())] == [
        "a",
        "d",
    ]
    assert [
        r.name for r in project_wheel_dependencies(marked, None, frozenset({"fast"}))
    ] == ["a", "b", "d"]


def test_lazy_wheel_layout_is_read_once_and_shared_by_copies() -> None:
    from cpip.core.versions import Version
    from cpip.core.wheel import LazyWheelLayout, WheelCandidate

    reads: list[None] = []

    def compute() -> tuple[str, ...]:
        reads.append(None)
        return ("demo-1.0.dist-info", (), True)

    candidate = WheelCandidate(
        name="demo",
        version=Version("1.0"),
        path="/wheels/demo-1.0-py3-none-any.whl",
        dependencies=(),
        wheel_layout=LazyWheelLayout(compute),
    )
    copy = candidate.copy_with(source_kind="wheel")

    assert candidate.wheel_layout_if_loaded is None
    assert reads == []
    assert copy.wheel_layout == ("demo-1.0.dist-info", (), True)
    assert candidate.wheel_layout == ("demo-1.0.dist-info", (), True)
    assert reads == [None]
    assert candidate.wheel_layout_if_loaded == ("demo-1.0.dist-info", (), True)

    candidate.wheel_layout = None
    assert candidate.wheel_layout is None
