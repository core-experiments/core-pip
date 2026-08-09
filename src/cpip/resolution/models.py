"""Consolidated resolution configurations, models, and URL identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.core.packaging import Requirement


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


class RequirementInput(Protocol):
    """Installer-provided requirement data consumed by resolution.

    The concrete installer requirement also carries build and preparation
    state.  Resolution depends only on this smaller structural contract.
    """

    req: Any
    link: Any
    hash_options: dict[str, list[str]]
    constraint: bool
    satisfied_by: Any
    editable: bool
    user_supplied: bool

    @property
    def name(self) -> str | None: ...

    @property
    def extras(self) -> set[str]: ...

    @property
    def markers(self) -> str | None: ...

    def is_satisfied_by(self, candidate: object) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResolvedRequirement:
    """A requirement satisfied by an already-installed distribution."""

    requirement: Requirement
    distribution: InstalledDistribution


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """The single result shape returned by the canonical engine."""

    candidates: tuple[Any, ...]
    graph: Mapping[str, frozenset[str]]
    conflicts: tuple[str, ...] = ()
    satisfied: tuple[ResolvedRequirement, ...] = ()
    metrics: Mapping[str, int | float] = MappingProxyType({})


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    netloc = (
        "" if parts.scheme == "file" and parts.netloc == "localhost" else parts.netloc
    )
    fragment = tuple(
        item
        for item in parse_qsl(parts.fragment, keep_blank_values=True)
        if item[0].lower() != "egg"
    )
    query = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query), urlencode(fragment))
    )


def url_name(url: str) -> str | None:
    values = parse_qs(urlsplit(url).fragment).get("egg")
    return values[0] if values else None
