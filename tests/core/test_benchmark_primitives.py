from __future__ import annotations

from benchmarks.pip_primitives import (
    LocalSpecifierChecks,
    NameNormalization,
    RequirementParsing,
    VersionFiltering,
    VersionParsing,
    WheelFilenameParsing,
)


def test_primitive_benchmarks_smoke() -> None:
    requirement = RequirementParsing()
    requirement.time_parse_requirement_uncached("demo>=1")
    requirement.time_parse_requirement_cached("demo>=1")

    version = VersionParsing()
    version.time_parse_version("1.2.3")

    name = NameNormalization()
    name.time_canonicalize_name("Demo_Pkg.Name")

    wheel = WheelFilenameParsing()
    wheel.time_parse_wheel_filename("demo-1.2-py3-none-any.whl")

    local = LocalSpecifierChecks()
    local.setup("range")
    local.time_contains("range")

    filtering = VersionFiltering()
    filtering.setup(">=1,<4")
    filtering.time_filter_versions(">=1,<4")
