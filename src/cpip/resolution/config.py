"""Immutable configuration for one NAB resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolutionConfig:
    """Complete policy and source configuration for one resolution."""

    find_links: tuple[str, ...] = ()
    index_urls: tuple[str, ...] | None = None
    no_index: bool = False

    allow_prereleases: bool = False
    no_deps: bool = False
    constraints: tuple[str, ...] = ()
    ignore_requires_python: bool = False
    python_version: str | None = None
    ignore_installed: bool = False
    upgrade: bool = False
    require_hashes: bool = False
    compute_source_hashes: bool = True
    upgrade_strategy: str = "only-if-needed"
