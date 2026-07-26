from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from pip.core.errors import InstallationError
from pip.core.wheel import (
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
        )
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
    runtime: str, wheel: str, expected: bool
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


def test_wheel_candidate_rejects_invalid_filename_oracle(tmp_path: Path) -> None:
    wheel = tmp_path / "invalid.whl"
    wheel.write_bytes(b"not a wheel")

    with pytest.raises(InstallationError, match="Invalid wheel filename"):
        wheel_candidate(wheel)


def test_wheel_candidate_reuses_metadata_across_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                )
            ),
        )
    base = wheel_candidate(wheel)
    monkeypatch.setattr(
        "pip.core.wheel.read_wheel_metadata_internal",
        lambda *args_internal, **kwargs_internal: pytest.fail("metadata was reparsed"),
    )

    feature = wheel_candidate(wheel, {"feature"})

    assert [item.name for item in base.dependencies] == ["base"]
    assert [item.name for item in feature.dependencies] == ["base", "optional"]
