"""Dedicated PEP 440 specifier parsing benchmarks, matching uv's cases."""

from __future__ import annotations

from cpip.core.packaging import SpecifierSet


class ParseVersionSpecifiers:
    params = (
        ">=3.8",
        ">=3.8,<4",
        ">=2.5, !=3.0.*, !=3.1.*, !=3.2.*, <4",
        "~=2.1",
        "!=2.0rc1,>=2.0",
    )
    param_names = ("specifiers",)
    number = 1000

    def time_parse_version_specifiers(self, specifiers: str) -> None:
        SpecifierSet(specifiers)
