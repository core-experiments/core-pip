from __future__ import annotations

import urllib.parse
import posixpath
from collections import defaultdict
from collections.abc import Callable, Iterable

from cpip.core.errors import ResolutionError
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    canonicalize_name,
    marker_applies,
)
from cpip.core.wheel import parse_wheel_filename


ConstraintKey = tuple[str, str, frozenset[str], str | None, str | None, str]


class ConstraintStore:
    """Normalized, memoized constraint overlays for one resolution."""

    def __init__(
        self,
        constraints: Iterable[Requirement],
        *,
        direct_urls_equivalent: Callable[[str | None, str | None], bool],
    ) -> None:
        self.constraints = tuple(constraints)
        grouped: defaultdict[str, list[Requirement]] = defaultdict(list)
        for constraint in self.constraints:
            grouped[constraint.canonical_name].append(constraint)
        self.constraints_by_name = {
            name: tuple(constraints) for name, constraints in grouped.items()
        }
        self.has_constraints = bool(self.constraints)
        self.direct_urls_equivalent = direct_urls_equivalent
        self.identity_cache: dict[int, tuple[Requirement, Requirement]] = {}
        self.cache: dict[ConstraintKey, Requirement] = {}

    @staticmethod
    def key(requirement: Requirement) -> ConstraintKey:
        return (
            requirement.name,
            str(requirement.specifier),
            requirement.extras,
            requirement.url,
            requirement.marker,
            requirement.raw,
        )

    def apply(self, requirement: Requirement) -> Requirement:
        if not self.has_constraints:
            return requirement
        identity = self.identity_cache.get(id(requirement))
        if identity is not None and identity[0] is requirement:
            return identity[1]
        key = self.key(requirement)
        cached = self.cache.get(key)
        if cached is not None:
            self.identity_cache[id(requirement)] = requirement, cached
            return cached

        matching = [
            constraint
            for constraint in self.constraints_by_name.get(
                requirement.canonical_name, ()
            )
            if marker_applies(constraint.marker, extras=requirement.extras)
        ]
        if not matching:
            self.cache[key] = requirement
            self.identity_cache[id(requirement)] = requirement, requirement
            return requirement
        direct_constraints = [constraint for constraint in matching if constraint.url]
        if direct_constraints:
            if len({constraint.url for constraint in direct_constraints}) > 1:
                raise ResolutionError(
                    f"Cannot install {requirement.name} because these package versions "
                    "have conflicting dependencies."
                )
            selected = direct_constraints[-1]
            if requirement.url is not None and not self.direct_urls_equivalent(
                selected.url, requirement.url
            ):
                filename = posixpath.basename(urllib.parse.urlparse(requirement.url).path)
                parsed = parse_wheel_filename(filename)
                requested_label = (
                    f"{canonicalize_name(parsed[0])} {parsed[1]}"
                    if parsed is not None
                    else canonicalize_name(requirement.name)
                )
                raise ResolutionError(
                    f"Cannot install {requested_label} because these package versions "
                    "have conflicting dependencies."
                )
            merged_specifier = ",".join(
                part
                for part in (
                    str(requirement.specifier),
                    str(selected.specifier),
                )
                if part
            )
            result = Requirement(
                name=requirement.name,
                specifier=SpecifierSet(merged_specifier),
                extras=requirement.extras,
                url=selected.url,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        else:
            spec_parts = [
                str(requirement.specifier),
                *(str(item.specifier) for item in matching),
            ]
            merged = ",".join(part for part in spec_parts if part)
            result = Requirement(
                name=requirement.name,
                specifier=SpecifierSet(merged),
                extras=requirement.extras,
                url=requirement.url,
                marker=requirement.marker,
                raw=requirement.raw if not merged else f"{requirement.name}{merged}",
            )
        self.cache[key] = result
        self.identity_cache[id(requirement)] = requirement, result
        return result
