from __future__ import annotations

import sysconfig
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
        ),
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
                ),
            )
        return tuple(normalized)
    return tuple(supported)
