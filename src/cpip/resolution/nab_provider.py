from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from cpip._vendor.nab_resolver.ranges import Range
from cpip._vendor.nab_resolver.types import Incompatibility, RangeProtocol
from cpip.core.metadata import InstalledDistribution, find_installed
from cpip.core.wheel import WheelCandidate
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.provider import CandidateProvider
from cpip.resolution.models import ResolutionConfig, canonical_url, url_name


class InstalledCandidate:
    """NAB candidate backed by an already-installed distribution."""

    source_kind = "installed"
    source_url = None
    source_vcs = None
    source_hashes = None
    path = ""
    from_cache = False
    yanked_reason = None
    requires_python = None
    provided_extras = frozenset()

    def __init__(
        self, distribution: InstalledDistribution, extras: frozenset[str]
    ) -> None:
        self.distribution = distribution
        self.name = distribution.name
        self.version = Version(distribution.version)
        self.dependencies = tuple(distribution.dependencies(extras))
        self.path = distribution.location

    @property
    def canonical_name(self) -> str:
        return self.distribution.canonical_name


def _key(requirement: Requirement) -> str:
    name = requirement.name
    if name.startswith(("file://", "http://", "https://")):
        name = urlsplit(name).path.rstrip("/").rsplit("/", 1)[-1] or name
    return canonicalize_name(name)


@dataclass
class NabProvider:
    """Native NAB provider backed by cpip candidate discovery."""

    provider: CandidateProvider
    context: ResolutionConfig

    def __post_init__(self) -> None:
        self.allow_prereleases = self.context.allow_prereleases
        self.no_deps = self.context.no_deps
        self.constraints = self.context.constraints
        self.ignore_requires_python = self.context.ignore_requires_python
        self.python_version = self.context.python_version
        self.records: dict[
            tuple[str, Version], WheelCandidate | InstalledCandidate
        ] = {}
        self.requirements: dict[str, Requirement] = {}
        self.display_requirements: dict[str, Requirement] = {}
        self._version_cache: dict[tuple[object, ...], tuple[Version, ...]] = {}
        self._installed_cache: dict[str, InstalledCandidate | None] = {}
        self._dependency_cache: dict[
            tuple[str, Version, tuple[str, ...]], Mapping[str, Range[Version]]
        ] = {}
        normalized_constraints = []
        for value in self.constraints:
            requirement = parse_requirement(value)
            if requirement.url is not None and requirement.name.startswith(
                ("file://", "http://", "https://")
            ):
                name = url_name(requirement.url)
                if name:
                    requirement = Requirement(
                        name=name,
                        specifier=requirement.specifier,
                        extras=requirement.extras,
                        url=requirement.url,
                        marker=requirement.marker,
                        raw=requirement.raw,
                    )
            normalized_constraints.append(requirement)
        self.constraint_requirements = tuple(normalized_constraints)

    def _installed_candidate(self, package: str) -> InstalledCandidate | None:
        if self.context.ignore_installed:
            return None
        if self.requirements[package].url is not None or any(
            constraint.url is not None for constraint in self._constraint_for(package)
        ):
            return None
        if package not in self._installed_cache:
            distribution = find_installed(package)
            if distribution is None:
                candidate = None
            else:
                try:
                    candidate = InstalledCandidate(
                        distribution,
                        frozenset(self.requirements[package].extras),
                    )
                except ValueError:
                    candidate = None
            self._installed_cache[package] = candidate
        return self._installed_cache[package]

    def _constraint_for(self, package: str) -> tuple[Requirement, ...]:
        name = canonicalize_name(package.split("[", 1)[0])
        return tuple(
            constraint
            for constraint in self.constraint_requirements
            if constraint is not None
            and canonicalize_name(constraint.name) == name
            and marker_applies(constraint.marker)
        )

    def _versions(self, package: str) -> tuple[Version, ...]:
        requirement = self.requirements[package]
        cache_key = (
            package,
            requirement.specifier.raw,
            tuple(sorted(requirement.extras)),
            requirement.url,
        )
        cached = self._version_cache.get(cache_key)
        if cached is not None:
            return cached
        if requirement.url is not None:
            candidates = tuple(self.provider.find_candidates(requirement))
            versions = tuple(candidate.version for candidate in candidates)
        else:
            versions = tuple(
                summary.version
                for summary in self.provider.available_versions(requirement)
            )
        installed = self._installed_candidate(package)
        if installed is not None and installed.version not in versions:
            versions += (installed.version,)
        if not versions and requirement.url is None:
            previous_allow_yanked = getattr(self.provider, "allow_yanked", None)
            if previous_allow_yanked is False:
                self.provider.allow_yanked = True
            try:
                fallback_candidates = tuple(
                    self.provider.find_candidates(parse_requirement(package))
                )
            finally:
                if previous_allow_yanked is False:
                    self.provider.allow_yanked = False
            if fallback_candidates:
                unyanked = tuple(
                    candidate
                    for candidate in fallback_candidates
                    if getattr(candidate, "yanked_reason", None) is None
                )
                versions = tuple(
                    candidate.version for candidate in (unyanked or fallback_candidates)
                )
                self._version_cache[cache_key] = versions
                return versions
        self._version_cache[cache_key] = versions
        return versions

    def _allows(self, package: str, version: Version) -> bool:
        if not version.is_prerelease or self.allow_prereleases:
            return True
        control = getattr(self.provider, "release_control", None)
        return control is None or control.allows_prereleases(package) is not False

    def _eligible_versions(self, package: str) -> tuple[Version, ...]:
        """Return versions that this provider can actually offer.

        NAB calls ``has_satisfying_version`` while constructing diagnostics.
        Keep that answer consistent with ``choose_version`` instead of
        exposing the raw index catalog.
        """
        requirement = self.requirements[package]
        constraints = self._constraint_for(package)
        versions = self._versions(package)
        return tuple(
            version
            for version in versions
            if requirement.specifier.contains(version, allow_prereleases=True)
            and self._allows(package, version)
            and all(
                constraint.specifier.contains(version, allow_prereleases=True)
                for constraint in constraints
            )
        )

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        requirement = self.requirements[package]
        constraints = self._constraint_for(package)
        url_constraints = tuple(
            constraint for constraint in constraints if constraint.url is not None
        )
        constraint_urls = tuple(
            canonical_url(url)
            for constraint in url_constraints
            if (url := constraint.url) is not None
        )
        if len(set(constraint_urls)) > 1:
            return None
        requirement_url = requirement.url
        if (
            requirement_url is not None
            and constraint_urls
            and any(canonical_url(requirement_url) != url for url in constraint_urls)
        ):
            return None
        candidate_requirement = requirement
        if len(url_constraints) == 1 and requirement.url is None:
            # A URL constraint is an artifact identity constraint, not merely
            # an empty version specifier.  Discover its version and use the
            # URL requirement when materializing the selected candidate.
            candidate_requirement = url_constraints[0]
        if requirement.url is None and (
            not CandidateEvaluator.is_exact_pin(requirement)
            or not isinstance(self.provider, CandidateProvider)
        ):
            if not isinstance(self.provider, CandidateProvider):
                candidate_requirement = parse_requirement(package)
            else:
                candidate_requirement = Requirement(
                    name=requirement.name,
                    specifier=SpecifierSet(),
                    extras=requirement.extras,
                    marker=requirement.marker,
                    raw=requirement.raw,
                )
        versions = self._versions(package)
        if len(url_constraints) == 1 and requirement.url is None:
            constrained_candidates = tuple(
                self.provider.find_candidates(url_constraints[0])
            )
            constrained_versions = {
                candidate.version for candidate in constrained_candidates
            }
            versions = tuple(sorted(set(versions) | constrained_versions))
        matching = [
            version
            for version in versions
            if version in version_range and self._allows(package, version)
        ]
        control = getattr(self.provider, "release_control", None)
        if not self.allow_prereleases and (
            control is None or control.allows_prereleases(package) is None
        ):
            stable = [version for version in matching if not version.is_prerelease]
            if stable:
                matching = stable
        if len(url_constraints) == 1 and requirement.url is None:
            matching = [
                version for version in matching if version in constrained_versions
            ]
        matching = [
            version
            for version in matching
            if all(
                constraint.specifier.contains(
                    version,
                    allow_prereleases=self.allow_prereleases,
                )
                for constraint in constraints
            )
        ]
        if not matching:
            return None
        installed = self._installed_candidate(package)
        if self.context.upgrade and installed is not None:
            indexed_matching = [
                version for version in matching if version != installed.version
            ]
            installed_is_indexed = bool(
                self.provider.find_candidates(
                    parse_requirement(package),
                    allowed_versions=frozenset({installed.version}),
                )
            )
            if indexed_matching and not installed_is_indexed:
                matching = indexed_matching
        if (
            installed is not None
            and installed.version in matching
            and not self.context.upgrade
        ):
            selected = installed.version
        else:
            selected = max(matching)
        if installed is not None and selected == installed.version:
            self.records[(package, selected)] = installed
            return selected
        candidates = tuple(
            self.provider.find_candidates(
                candidate_requirement, allowed_versions=frozenset({selected})
            )
        )
        if not candidates and requirement.url is None:
            candidates = tuple(
                self.provider.find_candidates(
                    parse_requirement(package),
                    allowed_versions=frozenset({selected}),
                ),
            )
        if not candidates and getattr(self.provider, "allow_yanked", None) is False:
            self.provider.allow_yanked = True
            try:
                fallback = tuple(
                    self.provider.find_candidates(parse_requirement(package))
                )
                usable = [
                    item
                    for item in fallback
                    if item.version in version_range
                    and item.version in matching
                    and all(
                        constraint.specifier.contains(
                            item.version,
                            allow_prereleases=True,
                        )
                        for constraint in constraints
                    )
                    and getattr(item, "yanked_reason", None) is None
                ]
                if usable:
                    candidate = max(usable, key=lambda item: item.version)
                    selected = candidate.version
                    candidates = (candidate,)
                else:
                    candidates = tuple(
                        self.provider.find_candidates(
                            parse_requirement(package),
                            allowed_versions=frozenset({selected}),
                        ),
                    )
            finally:
                self.provider.allow_yanked = False
        if not candidates:
            return None
        candidate = candidates[0]
        candidate_requires_python = getattr(candidate, "requires_python", None)
        if (
            not self.ignore_requires_python
            and self.python_version is None
            and candidate_requires_python
            and not CandidateEvaluator.requires_python_matches(
                candidate_requires_python
            )
        ):
            for fallback in sorted(matching, reverse=True):
                if fallback == selected:
                    continue
                alternatives = tuple(
                    self.provider.find_candidates(
                        candidate_requirement, allowed_versions=frozenset({fallback})
                    )
                )
                if alternatives and (
                    self.ignore_requires_python
                    or self.python_version is not None
                    or not getattr(alternatives[0], "requires_python", None)
                    or CandidateEvaluator.requires_python_matches(
                        alternatives[0].requires_python
                    )
                ):
                    selected, candidate = fallback, alternatives[0]
                    break
            else:
                return None
        self.records[(package, selected)] = candidate
        return selected

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        return any(
            version in version_range for version in self._eligible_versions(package)
        )

    def get_dependencies(
        self, package: str, version: Version
    ) -> Mapping[str, Range[Version]]:
        if self.no_deps:
            return {}
        cache_key = (package, version, tuple(sorted(self.requirements[package].extras)))
        cached = self._dependency_cache.get(cache_key)
        if cached is not None:
            return cached
        record = self.records.get((package, version))
        if record is None:
            raise RuntimeError(
                f"NAB requested dependencies for unselected candidate {package}=={version}"
            )
        normalized_dependencies = []
        for dependency in record.dependencies:
            if not marker_applies(
                dependency.marker,
                extras=self.requirements[package].extras,
            ):
                continue
            if dependency.name.startswith(("file://", "http://", "https://")):
                name = urlsplit(dependency.name).path.rstrip("/").rsplit("/", 1)[-1]
                dependency = Requirement(
                    name=name or dependency.name,
                    specifier=dependency.specifier,
                    extras=dependency.extras,
                    url=dependency.url or dependency.name,
                    marker=dependency.marker,
                    raw=dependency.raw,
                )
            normalized_dependencies.append(dependency)
        dependencies_records = tuple(normalized_dependencies)
        self_dependencies = [d for d in dependencies_records if _key(d) == package]
        if self_dependencies:
            merged_extras = frozenset(
                self.requirements[package].extras
                | frozenset(
                    extra
                    for dependency in self_dependencies
                    for extra in dependency.extras
                )
            )
            current = self.requirements[package]
            self.requirements[package] = Requirement(
                name=current.name,
                specifier=current.specifier,
                extras=merged_extras,
                url=current.url,
                marker=current.marker,
                raw=current.raw,
            )
            self.choose_version(package, Range.singleton(version))
            record = self.records[(package, version)]
        dependencies: dict[str, Range[Version]] = {}
        for dependency in dependencies_records:
            if _key(dependency) == package:
                continue
            if dependency.url is None and dependency.name.startswith(
                ("file://", "http://", "https://")
            ):
                dependency = Requirement(
                    name=dependency.name,
                    specifier=dependency.specifier,
                    extras=dependency.extras,
                    url=dependency.name,
                    marker=dependency.marker,
                    raw=dependency.raw,
                )
            dependency_key = _key(dependency)
            self.display_requirements.setdefault(dependency_key, dependency)
            if dependency.url is not None and dependency.name.startswith(
                ("file://", "http://", "https://")
            ):
                name = url_name(dependency.url)
                if name is None and dependency.raw and "@" in dependency.raw:
                    raw_name = dependency.raw.split("@", 1)[0].strip()
                    if raw_name and not raw_name.startswith(
                        ("file://", "http://", "https://")
                    ):
                        name = raw_name
                if name is None:
                    name = dependency.url.rstrip("/").rsplit("/", 1)[-1]
                if name is None:
                    try:
                        candidates = tuple(
                            self.provider.find_candidates(
                                dependency,
                                allowed_versions=None,
                            ),
                        )
                    except (AttributeError, TypeError):
                        candidates = ()
                    if candidates:
                        name = candidates[0].name
                if name:
                    dependency = Requirement(
                        name=name,
                        specifier=dependency.specifier,
                        extras=dependency.extras,
                        url=dependency.url,
                        marker=dependency.marker,
                        raw=dependency.raw,
                    )
                    dependency_key = _key(dependency)
            dependency_constraints = self._constraint_for(dependency_key)
            dependency_url = next(
                (
                    constraint.url
                    for constraint in dependency_constraints
                    if constraint.url is not None
                ),
                dependency.url,
            )
            if dependency_url != dependency.url:
                dependency = Requirement(
                    name=dependency.name,
                    specifier=dependency.specifier,
                    extras=dependency.extras,
                    url=dependency_url,
                    marker=dependency.marker,
                    raw=dependency.raw,
                )
            existing_requirement = self.requirements.get(dependency_key)
            if (
                existing_requirement is not None
                and existing_requirement.url is not None
                and dependency.url is not None
                and canonical_url(existing_requirement.url)
                != canonical_url(dependency.url)
            ):
                dependencies[dependency_key] = Range.empty()
                continue
            if existing_requirement is None:
                self.requirements[dependency_key] = dependency
            elif dependency.extras - existing_requirement.extras:
                self.requirements[dependency_key] = Requirement(
                    name=existing_requirement.name,
                    specifier=existing_requirement.specifier,
                    extras=frozenset(existing_requirement.extras | dependency.extras),
                    url=existing_requirement.url,
                    marker=existing_requirement.marker,
                    raw=existing_requirement.raw,
                )
                selected_dependency_version = next(
                    (
                        candidate_version
                        for (candidate_name, candidate_version) in self.records
                        if candidate_name == dependency_key
                    ),
                    None,
                )
                if selected_dependency_version is not None:
                    self.choose_version(
                        dependency_key,
                        Range.singleton(selected_dependency_version),
                    )
                    record = self.records.get(
                        (dependency_key, selected_dependency_version), record
                    )
            allowed = self._versions(dependency_key)
            selected = [
                candidate
                for candidate in allowed
                if dependency.specifier.contains(candidate, allow_prereleases=True)
                and all(
                    constraint.specifier.contains(candidate, allow_prereleases=True)
                    for constraint in self._constraint_for(dependency_key)
                )
            ]
            # Keep exact dependency constraints in diagnostics even when no
            # matching artifact exists; a finite available-version range
            # would otherwise collapse to ``<empty>`` and hide ``==N``.
            specifier_text = str(dependency.specifier)
            if specifier_text.startswith("==") and "," not in specifier_text:
                try:
                    dependencies[dependency_key] = Range.singleton(
                        Version(specifier_text[2:]),
                    )
                except ValueError:
                    dependencies[dependency_key] = self._finite_range(selected)
            else:
                dependencies[dependency_key] = self._finite_range(selected)
        result = dict(dependencies)
        self._dependency_cache[cache_key] = result
        return result

    @staticmethod
    def _finite_range(versions: tuple[Version, ...] | list[Version]) -> Range[Version]:
        result = Range.empty()
        for version in versions:
            result = result | Range.singleton(version)
        return result

    def begin_decision_scan(self) -> None:
        return None

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int, str]:
        return (len(self._versions(package)), -conflict_counts.get(package, 0), package)

    def is_ready(self, package: str) -> bool:
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        return None

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        return []

    def widen_decision(self, package: str, version: Version) -> Range[Version] | None:
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[Version]
    ) -> RangeProtocol[Version]:
        return constraint

    def add_root(self, requirement: Requirement) -> tuple[str, Range[Version]]:
        if requirement.name.startswith(("file://", "http://", "https://")):
            name = urlsplit(requirement.name).path.rstrip("/").rsplit("/", 1)[-1]
            requirement = Requirement(
                name=name or requirement.name,
                specifier=requirement.specifier,
                extras=requirement.extras,
                url=requirement.url or requirement.name,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        package = _key(requirement)
        previous = self.requirements.get(package)
        if previous is not None and previous.extras != requirement.extras:
            requirement = Requirement(
                name=requirement.name,
                specifier=requirement.specifier,
                extras=frozenset(previous.extras | requirement.extras),
                url=requirement.url or previous.url,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        elif (
            previous is not None
            and previous.url is not None
            and requirement.url is None
        ):
            requirement = Requirement(
                name=requirement.name,
                specifier=requirement.specifier,
                extras=frozenset(previous.extras | requirement.extras),
                url=previous.url,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        self.requirements[package] = requirement
        versions = self._eligible_versions(package)
        return package, self._finite_range(versions)

    def add_roots(self, requirements: list[Requirement]) -> dict[str, Range[Version]]:
        """Register roots with extras merged before NAB builds its graph."""
        root_names = {requirement.canonical_name for requirement in requirements}
        merged = list(requirements)
        for requirement in tuple(merged):
            # Extras merging only needs the best available candidate. Walking
            # every source candidate here eagerly builds older sdists, which
            # is both expensive and incorrect for offline resolution when an
            # older source distribution is intentionally broken.
            candidate = next(
                iter(self.provider.find_candidates(requirement, allowed_versions=None)),
                None,
            )
            if candidate is None:
                continue
            for dependency in getattr(candidate, "dependencies", ()):
                if dependency.canonical_name not in root_names or not dependency.extras:
                    continue
                for index, root in enumerate(merged):
                    if root.canonical_name != dependency.canonical_name:
                        continue
                    extras = frozenset(root.extras | dependency.extras)
                    if extras != root.extras:
                        merged[index] = Requirement(
                            name=root.name,
                            specifier=root.specifier,
                            extras=extras,
                            url=root.url,
                            marker=root.marker,
                            raw=root.raw,
                        )
        roots: dict[str, Range[Version]] = {}
        for requirement in merged:
            package, requirement_range = self.add_root(requirement)
            roots[package] = roots.get(package, requirement_range) & requirement_range
        return roots
