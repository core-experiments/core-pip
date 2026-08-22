from __future__ import annotations

import sys
from collections.abc import Mapping
from contextlib import nullcontext
from urllib.parse import urlsplit

from cpip._vendor.nab_resolver.ranges import Range
from cpip._vendor.nab_resolver.types import Incompatibility, RangeProtocol
from cpip.core.metadata import find_installed
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.core.versions import Version, ZERO_VERSION
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.provider import CandidateProvider
from cpip.resolution.models import ResolutionConfig, canonical_url, url_name
from cpip.resolution.nab_types import (
    _MIN_PINS_TO_DISAGREE,
    InstalledCandidate,
    _dependencies_or_none,
    _implied_range,
    _key,
    _RecordingRequirements,
)

TYPE_CHECKING = False

_MISSING = object()

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


# Sorting Versions through their comparison keys gives the order
# Version.__lt__ defines, with the tuple comparison done in C instead of a
# Python-level dunder per comparison.

# SpecifierSet's mutable state is all memo caches, so one empty instance can
# stand in for every "any version" requirement.


class NabProvider:
    """Native NAB provider backed by cpip candidate discovery."""

    def __init__(self, provider: CandidateProvider, context: ResolutionConfig) -> None:
        self.provider = provider
        self.context = context
        self.__post_init__()

    def __post_init__(self) -> None:
        self.allow_prereleases = self.context.allow_prereleases
        self.no_deps = self.context.no_deps
        self.constraints = self.context.constraints
        self.ignore_requires_python = self.context.ignore_requires_python
        self.python_version = self.context.python_version
        self.records: dict[
            tuple[str, Version], WheelCandidate | InstalledCandidate
        ] = {}
        self.requirements: _RecordingRequirements = _RecordingRequirements()
        self.display_requirements: dict[str, Requirement] = {}
        self._unpinned_requirements: dict[str, tuple[Requirement, Requirement]] = {}
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
        self._catalog_candidate_cache: dict[tuple[str, Version], object | None] = {}
        self._catalog_by_version_cache: dict[str, dict[Version, object | None]] = {}
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
            requirement.specifier.text,
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
            not requirement.specifier.is_pinned
            or not isinstance(self.provider, CandidateProvider)
        ):
            if not isinstance(self.provider, CandidateProvider):
                candidate_requirement = parse_requirement(package)
            else:
                # The same name-only requirement is needed on every decision
                # for this package; a package's entry in self.requirements
                # is replaced, never mutated, so identity tells when the
                # memoized one no longer matches.
                memo = self._unpinned_requirements.get(package)
                if memo is None or memo[0] is not requirement:
                    memo = (
                        requirement,
                        Requirement(
                            name=requirement.name,
                            specifier=SpecifierSet(),
                            extras=requirement.extras,
                            marker=requirement.marker,
                            raw=requirement.raw,
                        ),
                    )
                    self._unpinned_requirements[package] = memo
                candidate_requirement = memo[1]
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

        if self._invalid_metadata_rejects(candidate):
            print(
                f"WARNING: Ignoring version {candidate.version} of "
                f"{candidate.name} since it has invalid metadata",
                file=sys.stderr,
            )

            alternative = self._alternative_for_invalid_metadata(
                candidate_requirement,
                selected,
                matching=matching,
            )
            if alternative is None:
                return None
            selected, candidate = alternative

        if self._requires_python_rejects(candidate):
            alternative = self._alternative_for_requires_python(
                candidate_requirement,
                selected,
                matching=matching,
            )
            if alternative is None:
                return None
            selected, candidate = alternative

        if self._inconsistent_metadata_rejects(candidate):
            metadata_version = getattr(candidate, "metadata_version", None)

            print(
                f"WARNING: {candidate.name} has an inconsistent version: "
                f"expected '{candidate.version}', but metadata has "
                f"'{metadata_version}'",
            )

            if requirement.extras:
                print(
                    f"Requested {requirement.raw or requirement.name}, "
                    f"but installing version {metadata_version}",
                )

            alternative = self._alternative_for_inconsistent_metadata(
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
            pinned = dependency.specifier.exact_version
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

        One release at a time: the provider reads the release's artifacts
        out of the package catalog, and only the one release inspected is
        materialized. Querying the package by name instead re-evaluated
        every link and materialized every release a second time, beside the
        evaluation the resolver's own requirement already paid for.
        """
        key = (package, version)
        cached = self._catalog_candidate_cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached

        requirement = parse_requirement(package)
        try:
            records = self.provider.release_candidates(requirement, version)
        except Exception:
            # Metadata that will not load is the resolver's problem to report.
            records = ()

        if records is None:
            candidate = self._catalog_by_version(package).get(version)
        elif len(records) != 1:
            candidate = None
        else:
            try:
                candidate = self.provider.get_materializer_internal().materialize_one(
                    requirement,
                    records[0],
                )
            except Exception:
                candidate = None

        self._catalog_candidate_cache[key] = candidate
        return candidate

    def _catalog_by_version(self, package: str) -> dict[Version, object | None]:
        """Index every release of a package the provider has no catalog for.

        The fallback behind :meth:`_catalog_candidate`: one full query per
        package, materializing every release, for the cases the per-release
        read declines (a direct URL, an upload cutoff, required hashes).
        """
        cached = self._catalog_by_version_cache.get(package)
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

        self._catalog_by_version_cache[package] = index
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

    def _invalid_metadata_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether the candidate's metadata fails to load at all.

        A malformed artifact -- e.g. one of its own dependencies declaring an
        unparseable version -- surfaces as an exception the first time
        metadata is read (accessing any of ``dependencies``,
        ``requires_python``, or ``provided_extras`` triggers the same lazy
        load). Treat that the same way materialization already does: skip
        the candidate and let the resolver fall back to the next release.
        """
        try:
            getattr(candidate, "dependencies", None)
        except (OSError, ValueError):
            return True
        return False

    def _alternative_for_invalid_metadata(
        self,
        candidate_requirement: Requirement,
        selected: Version,
        *,
        matching: list[Version],
    ) -> tuple[Version, WheelCandidate] | None:
        """Walk back to the newest release whose metadata actually loads."""
        for fallback in sorted(matching, reverse=True):
            if fallback == selected:
                continue
            alternatives = tuple(
                self.provider.find_candidates(
                    candidate_requirement,
                    allowed_versions=frozenset({fallback}),
                )
            )
            if alternatives and not self._invalid_metadata_rejects(alternatives[0]):
                return fallback, alternatives[0]
        return None

    def _requires_python_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether this interpreter falls outside the candidate's Requires-Python.

        Skipped when the caller targets another interpreter, since the
        declaration then says nothing about the target.
        """
        try:
            requires_python = getattr(candidate, "requires_python", None)
        except (OSError, ValueError):
            # Unreadable metadata is a rejection here too -- callers that
            # need the real reason check _invalid_metadata_rejects first.
            return True

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

    def _inconsistent_metadata_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether the candidate's own metadata contradicts its declared version.

        A filename can claim one release while the wheel's METADATA declares
        another (a malformed or mislabeled artifact). Installing it would
        silently give the user something other than what they asked for, so
        treat it the same as an incompatible interpreter: reject it and let
        the resolver fall back to the next candidate.
        """
        declared_version = getattr(candidate, "version", None)
        if declared_version is None or declared_version == ZERO_VERSION:
            return False

        metadata_version = getattr(candidate, "metadata_version", None)
        if metadata_version is None:
            return False

        return metadata_version != declared_version

    def _alternative_for_inconsistent_metadata(
        self,
        candidate_requirement: Requirement,
        selected: Version,
        *,
        matching: list[Version],
    ) -> tuple[Version, WheelCandidate] | None:
        """Walk back to the newest release whose metadata matches its filename."""
        for fallback in sorted(matching, reverse=True):
            if fallback == selected:
                continue
            alternatives = tuple(
                self.provider.find_candidates(
                    candidate_requirement,
                    allowed_versions=frozenset({fallback}),
                )
            )
            if alternatives and not self._inconsistent_metadata_rejects(
                alternatives[0],
            ):
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
            pinned = dependency.specifier.exact_version
            if pinned is not None:
                dependencies[dependency_key] = Range.singleton(pinned)
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
            try:
                dependencies = getattr(candidate, "dependencies", ())
            except (OSError, ValueError):
                # Malformed metadata belongs to real resolution to diagnose;
                # this pre-scan is a best-effort extras merge and skips it.
                continue
            for dependency in dependencies:
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
