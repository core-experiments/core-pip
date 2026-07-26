from __future__ import annotations

import sys
import sysconfig
from dataclasses import dataclass
from functools import lru_cache

from .wheel import TargetContext, WheelTag, supported_wheel_tags


def expand_manylinux(platform: str) -> list[str]:
    if platform.startswith("manylinux2014_"):
        suffix = platform.removeprefix("manylinux2014_")
        return [platform, f"manylinux2010_{suffix}", f"manylinux1_{suffix}"]
    if platform.startswith("manylinux2010_"):
        suffix = platform.removeprefix("manylinux2010_")
        return [platform, f"manylinux1_{suffix}"]
    return [platform]


def get_supported(
    version: str | None = None,
    platforms: list[str] | None = None,
    impl: str | None = None,
    abis: list[str] | None = None,
) -> list[WheelTag]:
    return list(
        get_supported_internal(
            version,
            tuple(platforms) if platforms is not None else None,
            impl,
            tuple(abis) if abis is not None else None,
        )
    )


@lru_cache(maxsize=64)
def get_supported_internal(
    version: str | None,
    platforms: tuple[str, ...] | None,
    impl: str | None,
    abis: tuple[str, ...] | None,
) -> tuple[WheelTag, ...]:
    expanded_platforms: list[str] | None = None
    if platforms is not None:
        expanded_platforms = []
        for platform in platforms:
            expanded_platforms.extend(expand_manylinux(platform))
    target = None
    if any(value is not None for value in (version, expanded_platforms, impl, abis)):
        target = TargetContext(
            platforms=tuple(expanded_platforms or ()),
            implementation=impl,
            python_version=version,
            abis=tuple(abis or ()),
        )
    supported = supported_wheel_tags(target)
    soabi = sysconfig.get_config_var("SOABI")
    if soabi and "-" in soabi:
        normalized: list[WheelTag] = []
        for tag in supported:
            normalized.append(
                WheelTag(
                    interpreter=tag.interpreter.replace("-", "_"),
                    abi=tag.abi.replace("-", "_"),
                    platform=tag.platform.replace("-", "_"),
                )
            )
        return tuple(normalized)
    return tuple(supported)


@dataclass
class TargetPython:
    platforms: list[str] | None = None
    py_version_info: tuple[int, ...] | None = None
    abis: list[str] | None = None
    implementation: str | None = None

    def __post_init__(self) -> None:
        self.given_py_version_info = self.py_version_info
        if self.py_version_info is None:
            self.py_version_info = (
                sys.version_info.major,
                sys.version_info.minor,
                sys.version_info.micro,
            )
        else:
            version_info = tuple(self.py_version_info)
            padded = version_info + (0,) * (3 - len(version_info))
            self.py_version_info = padded[:3]
        self.py_version = (
            ""
            if not self.py_version_info
            else ".".join(str(part) for part in self.py_version_info[:2])
        )
        self.valid_tags: list[WheelTag] | None = None
        self.valid_tags_set: set[WheelTag] | None = None

    def format_given(self) -> str:
        parts: list[str] = []
        if self.platforms:
            parts.append(f"platforms={self.platforms!r}")
        if self.given_py_version_info is not None:
            version_info = self.given_py_version_info
            version = ".".join(str(part) for part in version_info[:2])
            parts.append(f"version_info={version!r}")
        if self.abis:
            parts.append(f"abis={self.abis!r}")
        if self.implementation:
            parts.append(f"implementation={self.implementation!r}")
        return " ".join(parts)

    def get_sorted_tags(self) -> list[WheelTag]:
        if self.valid_tags is None:
            version = None
            if self.given_py_version_info is not None:
                version = "".join(str(part) for part in self.given_py_version_info[:2])
            self.valid_tags = get_supported(
                version=version,
                platforms=self.platforms,
                impl=self.implementation,
                abis=self.abis,
            )
        return self.valid_tags

    def get_unsorted_tags(self) -> set[WheelTag]:
        if self.valid_tags_set is None:
            self.valid_tags_set = set(self.get_sorted_tags())
        return self.valid_tags_set
