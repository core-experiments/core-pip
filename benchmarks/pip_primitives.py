"""Small deterministic benchmarks for pip's resolver hot primitives."""

from __future__ import annotations

from pip.core.packaging import (
    SpecifierSet,
    Version,
    canonicalize_name,
    parse_requirement,
)
from pip.core.wheel import parse_wheel_filename
from pip.resolution.fast_local_wheelhouse import (
    LocalWheelSpecifier,
    LocalWheelVersion,
)


REQUIREMENTS = (
    "demo>=1.2,<3",
    "Demo_Pkg[PDF,SSL]>=1.0; python_version >= '3.11'",
    "demo!=1.0.*,>=0.5,<3; sys_platform == 'darwin'",
    "demo @ https://example.invalid/packages/demo-1.2-py3-none-any.whl",
)

VERSIONS = (
    "1",
    "1.2.3",
    "2.0rc1",
    "3.0.post1",
    "4.0.dev2+local.1",
)

WHEEL_FILENAMES = (
    "demo-1.2-py3-none-any.whl",
    "demo_pkg-2024.1-cp311-abi3-manylinux_2_17_x86_64.whl",
    "example-2.0.0-py2.py3-none-any.whl",
    "long_project_name-1.0.0-1-cp312-cp312-macosx_14_0_arm64.whl",
)

LOCAL_SPECIFIERS = {
    "range": (
        (">=", LocalWheelVersion((1, 0), "1.0")),
        ("<", LocalWheelVersion((4, 0), "4.0")),
    ),
    "exact": (("==", LocalWheelVersion((2, 4), "2.4")),),
    "exclusions": (
        (">=", LocalWheelVersion((1, 0), "1.0")),
        ("!=", LocalWheelVersion((2, 0), "2.0")),
        ("!=", LocalWheelVersion((3, 0), "3.0")),
    ),
}


class RequirementParsing:
    """Measure PEP 508 parsing with and without pip's parse cache."""

    params = REQUIREMENTS
    param_names = ("requirement",)
    number = 1000

    def time_parse_requirement_uncached(self, requirement: str) -> None:
        parse_requirement.__wrapped__(requirement)

    def time_parse_requirement_cached(self, requirement: str) -> None:
        parse_requirement(requirement)


class VersionParsing:
    params = VERSIONS
    param_names = ("version",)
    number = 1000

    def time_parse_version(self, version: str) -> None:
        Version(version)


class NameNormalization:
    params = (
        "Demo_Pkg.Name",
        "many---separators___here",
        "simple-name",
        "project.with.mixed_separators",
    )
    param_names = ("name",)
    number = 1000

    def time_canonicalize_name(self, name: str) -> None:
        canonicalize_name(name)


class WheelFilenameParsing:
    params = WHEEL_FILENAMES
    param_names = ("filename",)
    number = 1000

    def time_parse_wheel_filename(self, filename: str) -> None:
        parse_wheel_filename(filename)


class LocalSpecifierChecks:
    params = tuple(LOCAL_SPECIFIERS)
    param_names = ("specifier",)
    number = 1000

    def setup(self, specifier: str) -> None:
        self.specifier = LocalWheelSpecifier(LOCAL_SPECIFIERS[specifier])
        self.versions = tuple(
            LocalWheelVersion((index, 0), f"{index}.0") for index in range(1, 6)
        )

    def time_contains(self, specifier: str) -> None:
        for version in self.versions:
            self.specifier.contains(version)


class VersionFiltering:
    params = (">=1,<4", ">=2.0,!=3.0", "~=2.1")
    param_names = ("specifier",)
    number = 1000

    def setup(self, specifier: str) -> None:
        self.parsed = SpecifierSet(specifier)
        self.versions = tuple(Version(f"{major}.{minor}") for major in range(1, 6) for minor in range(10))

    def time_filter_versions(self, specifier: str) -> None:
        tuple(version for version in self.versions if self.parsed.contains(version))
