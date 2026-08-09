from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from cpip._vendor.nab_resolver.ranges import Range
from cpip._vendor.nab_resolver.types import Incompatibility, RangeProtocol
from cpip.core.metadata import InstalledDistribution, find_installed
from cpip.core.wheel import WheelCandidate
from cpip.core.packaging import (
    InvalidVersion,
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

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


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


# Fewer than two exact pins cannot intersect to nothing.
_MIN_PINS_TO_DISAGREE = 2


def _dependencies_or_none(candidate: object) -> tuple[Requirement, ...] | None:
    """A candidate's dependencies, or None when its metadata will not load.

    Reading ``dependencies`` is what pulls metadata, which for a source
    artifact means a build and for a remote one a request.  The forward check
    asks this of releases the resolver may never select, so a release with
    broken or unreachable metadata must read as undecidable rather than take
    the whole resolution down with it -- a different release may well resolve.
    """
    try:
        return tuple(getattr(candidate, "dependencies", ()))
    except Exception:
        return None


def _implied_range(specifier: SpecifierSet) -> Range[Version]:
    """Widen a specifier to the interval that contains everything it admits.

    ``bounds`` drops ``!=``, ``===`` and ``==X.*`` and reads ``~=`` as its
    half-open interval, and it is blind to pre-release rules.  Every one of
    those is a widening, which is the direction a rejection needs: an empty
    intersection of intervals that each contain *more* than their specifier is
    empty for the specifiers too.

    Working in intervals rather than over the catalog also keeps the answer
    independent of which releases the active policy happens to admit -- a
    yanked-only release cannot turn a possible fan-out into a rejected one.
    """
    lower, upper = specifier.bounds()
    result: Range[Version] = Range.full()

    if lower is not None:
        version, inclusive = lower
        result = result & (
            Range.at_least(version) if inclusive else Range.greater_than(version)
        )

    if upper is not None:
        version, inclusive = upper
        result = result & (
            Range.at_most(version) if inclusive else Range.less_than(version)
        )

    return result


def _exact_pin(requirement: Requirement) -> Version | None:
    """The single release a ``==`` requirement names, or None.

    Anything else -- a range, several clauses, a wildcard, an unparseable
    version -- has no unique release and so cannot narrow a domain to a point.
    """
    clauses = requirement.specifier.specifiers
    if len(clauses) != 1:
        return None

    clause = clauses[0]
    if clause.operator != "==" or clause.version.endswith("*"):
        return None

    try:
        return Version(clause.version)
    except InvalidVersion:
        return None


def _key(requirement: Requirement) -> str:
    name = requirement.name
    if name.startswith(("file://", "http://", "https://")):
        name = urlsplit(name).path.rstrip("/").rsplit("/", 1)[-1] or name
    return canonicalize_name(name)


class _RecordingRequirements(dict):
    """The package -> requirement map, recording every package it replaces.

    ``prioritize`` answers from ``len(_versions(package))``, which follows
    the package's requirement, and the resolver caches sort keys between
    decision scans. Recording in ``__setitem__`` rather than at the handful
    of assignment sites is what keeps that record complete as merging grows
    new ones.
    """

    __slots__ = ("touched",)

    def __init__(self) -> None:
        super().__init__()
        self.touched: set[str] = set()

    def __setitem__(self, key: str, value: Requirement) -> None:
        self.touched.add(key)
        super().__setitem__(key, value)


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
        self.requirements: dict[str, Requirement] = _RecordingRequirements()
        self.display_requirements: dict[str, Requirement] = {}
        self._version_cache: dict[tuple[object, ...], tuple[Version, ...]] = {}
        # Fast paths in front of ``_version_cache``, whose key costs more to
        # build than the lookup it guards. A package's entry in
        # ``self.requirements`` is replaced, never mutated, so an identity
        # check is enough to notice that the answer may have moved; a miss
        # just falls through to the content-keyed cache below.
        self._version_memo: dict[str, tuple[Requirement, tuple[Version, ...]]] = {}
        self._priority_memo: dict[
            str, tuple[Requirement, int, tuple[int, int, str]]
        ] = {}
        self._installed_cache: dict[str, InstalledCandidate | None] = {}
        # Forward-check memos. The catalog ones are keyed on facts that do not
        # move during a resolution. The verdict is not: it depends on the
        # package's active extras, which widen as extras are merged, so those
        # are part of its key.
        self._preflight_cache: dict[tuple[str, Version, tuple[str, ...]], bool] = {}
        self._catalog_candidate_cache: dict[str, dict[Version, object | None]] = {}
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
        memo = self._version_memo.get(package)
        if memo is not None and memo[0] is requirement:
            return memo[1]

        versions = self._versions_uncached(package, requirement)
        self._version_memo[package] = (requirement, versions)
        return versions

    def _versions_uncached(
        self,
        package: str,
        requirement: Requirement,
    ) -> tuple[Version, ...]:
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
            # Nothing matched under the active policy. Look again with yanked
            # releases admitted so the package is at least known to exist.
            with self._yanked_allowed():
                fallback_candidates = tuple(
                    self.provider.find_candidates(parse_requirement(package))
                )
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

    def _yanked_allowed(self) -> AbstractContextManager[None]:
        """Scope a yanked-release fallback, when the provider supports one.

        Stand-in providers used in tests implement only the query methods, so
        fall back to leaving policy alone rather than probing for attributes
        at each call site.
        """
        if isinstance(self.provider, CandidateProvider):
            return self.provider.yanked_allowed()
        return nullcontext()

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
            selected = self._newest_viable(package, matching)
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
        if not candidates:
            retried = self._retry_including_yanked(
                package,
                selected,
                matching=matching,
                constraints=constraints,
                version_range=version_range,
            )
            if retried is not None:
                selected, candidates = retried
        if not candidates:
            return None
        candidate = candidates[0]

        if self._requires_python_rejects(candidate):
            alternative = self._alternative_for_requires_python(
                candidate_requirement,
                selected,
                matching=matching,
            )
            if alternative is None:
                return None
            selected, candidate = alternative

        self.records[(package, selected)] = candidate
        return selected

    def _newest_viable(self, package: str, matching: list[Version]) -> Version:
        """Pick the newest version whose exact pins are not already impossible.

        The resolver has no lookahead: it decides a version, decides its
        dependencies, and only then discovers that two of them pin the same
        package to different releases.  Each such candidate costs a decision
        per dependency plus a conflict, and every conflict leaves behind an
        incompatibility that all later propagation re-scans.  On a wheelhouse
        whose releases disagree pairwise that is quadratic, and the resolver
        spends it before reaching the one release that works.

        Looking one level past the pins is enough to rule those out up front.
        Restores the behavior of ``preflight_exact_dependencies``, which the
        deleted local-wheelhouse kernel ran for exactly this reason.

        Rejecting a satisfiable version would silently return an older
        solution, so this defers to the resolver on anything it cannot decide
        exactly -- and if it rejects *every* candidate it defers as well,
        rather than claiming a graph is unsolvable on the strength of a
        conservative check.
        """
        if len(matching) == 1:
            # Nothing to choose between, so looking ahead cannot change the
            # answer -- and the metadata it would read is not free.
            return matching[0]

        newest_first = sorted(matching, reverse=True)
        for version in newest_first:
            if not self._pins_are_impossible(package, version):
                return version
        return newest_first[0]

    def _pins_are_impossible(self, package: str, version: Version) -> bool:
        """Whether this version's ``==`` pins force an empty version domain.

        Conservative by construction: every branch that cannot be decided
        exactly answers ``False`` so the resolver stays authoritative.  Only a
        provable emptiness -- two pins whose dependency requirements share a
        package but no version -- answers ``True``.
        """
        # Extras gate which dependencies apply, and merging one in widens the
        # set. A verdict reached under narrower extras must not be reused
        # after they grow, or a viable version gets skipped.
        extras = tuple(sorted(self.requirements[package].extras))
        cache_key = (package, version, extras)
        cached = self._preflight_cache.get(cache_key)
        if cached is not None:
            return cached

        verdict = self._compute_pins_are_impossible(package, version, extras)
        self._preflight_cache[cache_key] = verdict
        return verdict

    def _compute_pins_are_impossible(
        self,
        package: str,
        version: Version,
        extras: tuple[str, ...],
    ) -> bool:
        if not isinstance(self.provider, CandidateProvider):
            return False

        candidate = self._catalog_candidate(package, version)
        if candidate is None:
            return False

        # Two pins are the minimum that can disagree, so count them before
        # reading any child metadata. Requirements are already parsed, making
        # this the cheap half of the check and the common exit.
        dependencies = _dependencies_or_none(candidate)
        if dependencies is None:
            return False

        pins: list[tuple[Requirement, Version]] = []
        for dependency in dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            if dependency.url is not None:
                # A direct URL is an artifact identity, not a version domain.
                return False
            pinned = _exact_pin(dependency)
            if pinned is None:
                return False
            pins.append((dependency, pinned))

        if len(pins) < _MIN_PINS_TO_DISAGREE:
            return False

        domains: dict[str, Range[Version]] = {}

        for dependency, pinned in pins:
            child = self._catalog_candidate(_key(dependency), pinned)
            if child is None:
                # The pin names a release the catalog does not offer. The
                # resolver reports that far better than a silent skip would.
                return False

            grandchildren = _dependencies_or_none(child)
            if grandchildren is None:
                return False

            for grandchild in grandchildren:
                if not marker_applies(grandchild.marker, extras=dependency.extras):
                    continue
                if grandchild.url is not None:
                    continue

                name = _key(grandchild)
                implied = _implied_range(grandchild.specifier)
                narrowed = domains.get(name)
                narrowed = implied if narrowed is None else narrowed & implied
                if narrowed.is_empty:
                    return True
                domains[name] = narrowed

        return False

    def _catalog_candidate(self, package: str, version: Version) -> object | None:
        """The single catalog entry for one release, or None if not unique.

        Ambiguity is not a rejection: more than one artifact for a release
        means the choice belongs to the resolver's own evaluation.
        """
        return self._catalog_by_version(package).get(version)

    def _catalog_by_version(self, package: str) -> dict[Version, object | None]:
        """Index a package's catalog entries by release, in one query.

        Asking per release would run the whole candidate pipeline once per
        version, which is the same quadratic shape the forward check exists to
        avoid.  Candidate objects are cheap -- only reading ``dependencies``
        loads metadata -- so building the whole index costs one query.
        """
        cached = self._catalog_candidate_cache.get(package)
        if cached is not None:
            return cached

        try:
            found = tuple(self.provider.find_candidates(parse_requirement(package)))
        except Exception:
            # Metadata that will not load is the resolver's problem to report.
            found = ()

        index: dict[Version, object | None] = {}
        for candidate in found:
            version = candidate.version
            # A release with more than one artifact is ambiguous here.
            index[version] = None if version in index else candidate

        self._catalog_candidate_cache[package] = index
        return index

    def _retry_including_yanked(
        self,
        package: str,
        selected: Version,
        *,
        matching: list[Version],
        constraints: tuple[Requirement, ...],
        version_range: RangeProtocol[Version],
    ) -> tuple[Version, tuple[WheelCandidate, ...]] | None:
        """Look again with yanked releases admitted, or ``None`` to give up.

        Reached only when the active policy offered no artifact for a version
        the resolver already selected.  A release can be absent because every
        artifact for it is yanked, and admitting those makes the package
        resolvable again; an unyanked release found this way is preferred.

        There is nothing to relax when the provider already admits yanked
        releases, so that case declines rather than widening the search.
        """
        if not isinstance(self.provider, CandidateProvider):
            return None
        if self.provider.allow_yanked:
            return None

        with self.provider.yanked_allowed():
            fallback = tuple(self.provider.find_candidates(parse_requirement(package)))
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
                and item.yanked_reason is None
            ]
            if usable:
                candidate = max(usable, key=lambda item: item.version)
                return candidate.version, (candidate,)

            return selected, tuple(
                self.provider.find_candidates(
                    parse_requirement(package),
                    allowed_versions=frozenset({selected}),
                ),
            )

    def _requires_python_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether this interpreter falls outside the candidate's Requires-Python.

        Skipped when the caller targets another interpreter, since the
        declaration then says nothing about the target.
        """
        requires_python = getattr(candidate, "requires_python", None)
        if (
            self.ignore_requires_python
            or self.python_version is not None
            or not requires_python
        ):
            return False

        try:
            return not CandidateEvaluator.requires_python_matches(requires_python)
        except ValueError:
            # An unparseable declaration is a rejection, not a crash --
            # matching how available_versions treats the same metadata.
            return True

    def _alternative_for_requires_python(
        self,
        candidate_requirement: Requirement,
        selected: Version,
        *,
        matching: list[Version],
    ) -> tuple[Version, WheelCandidate] | None:
        """Walk back to the newest release this interpreter can install."""
        for fallback in sorted(matching, reverse=True):
            if fallback == selected:
                continue
            alternatives = tuple(
                self.provider.find_candidates(
                    candidate_requirement,
                    allowed_versions=frozenset({fallback}),
                )
            )
            if alternatives and not self._requires_python_rejects(alternatives[0]):
                return fallback, alternatives[0]
        return None

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
        """A range holding exactly ``versions``, built in one pass.

        Unioning singletons one at a time re-sorts and re-merges the whole
        interval list on every step, so a package with many releases pays
        O(n^2 log n) to describe its own catalog -- and this runs for every
        dependency edge.  Distinct versions give disjoint, non-touching
        singletons, so sorting once produces exactly what ``Range`` wants.
        """
        ordered = sorted(set(versions))
        return Range(tuple((version, True, version, True) for version in ordered))

    def begin_decision_scan(self) -> None:
        return None

    def consume_priority_invalidations(self) -> list[str]:
        """Report packages whose priority may have moved, and reset.

        ``is_ready`` is constant here, so a package's priority moves only
        with its requirement -- which is replaced, never mutated in place.
        """
        touched = self.requirements.touched
        if not touched:
            return []
        self.requirements.touched = set()
        return list(touched)

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int, str]:
        # ``choose_package`` runs this for every undecided package on every
        # decision, so the scan is the hot caller. Only the conflict count
        # moves between decisions; the version count follows the requirement.
        conflicts = conflict_counts.get(package, 0)
        requirement = self.requirements[package]
        memo = self._priority_memo.get(package)
        if memo is not None and memo[0] is requirement and memo[1] == conflicts:
            return memo[2]

        priority = (len(self._versions(package)), -conflicts, package)
        self._priority_memo[package] = (requirement, conflicts, priority)
        return priority

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
