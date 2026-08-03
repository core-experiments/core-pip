from __future__ import annotations

import os
import zipfile
from typing import Any

from cpip.core.packaging import Requirement, SpecifierSet, Version
from cpip.core.wheel import WheelCandidate, parse_wheel, wheel_candidate
from cpip.index.source_locations import resolve_source_location
from cpip.install.wheel_archive_cache import exact_install_plan_key
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.model import ResolutionResult


def cached_remote_plan_key(
    options: Any,
    bundle: Any,
    requirements: list[Any],
    target: Any,
) -> str | None:
    """Key the narrow, repeatable warm-install shape used by pinned locks."""

    if (
        options.no_cache_dir
        or options.target is None
        or not options.ignore_installed
        or not options.no_compile
        or options.dry_run
        or options.report
        or options.user
        or options.root is not None
        or options.prefix is not None
        or options.no_deps
        or options.upgrade
        or options.pre
        or options.require_hashes
        or options.ignore_requires_python
        or options.platform
        or options.implementation
        or options.python_version
        or options.abi
        or options.uploaded_prior_to
        or options.groups
        or options.requirements_from_scripts
        or options.constraint_files
        or options.build_constraint_files
        or options.config_settings
        or options.no_binary
        or options.only_binary
        or options.all_releases
        or options.only_final
        or bundle.no_index
        or bundle.find_links
        or bundle.extra_index_urls
        or bundle.constraints
        or bundle.editables
        or bundle.require_hashes
        or bundle.locked_links
        or bundle.requirement_hashes
        or bundle.constraint_hashes
        or bundle.format_control.no_binary
        or bundle.format_control.only_binary
        or bundle.release_control.all_releases
        or bundle.release_control.only_final
    ):
        return None

    context = (
        "remote-exact-v1",
        bundle.index_url,
        tuple(bundle.extra_index_urls),
        tuple(target.platforms),
        target.implementation,
        target.python_version,
        tuple(target.abis),
        options.upgrade_strategy,
        bool(options.force_reinstall),
    )

    return exact_install_plan_key(tuple(requirements), context)


def try_local_wheelhouse_plan(
    options: Any,
    bundle: Any,
    requirements: list[Any],
    *,
    cache_dir: str | None,
) -> ResolutionResult | None:
    """Reuse the local resolver for the narrow pure-wheel install shape."""

    if (
        not bundle.no_index
        or not bundle.find_links
        or bundle.extra_index_urls
        or bundle.constraints
        or bundle.require_hashes
        or bundle.format_control.no_binary
        or bundle.format_control.only_binary
        or bundle.release_control.all_releases
        or bundle.release_control.only_final
        or bundle.editables
        or options.groups
        or options.constraint_files
        or options.no_deps
        or options.pre
        or options.all_releases
        or options.only_final
        or options.no_binary
        or options.only_binary
        or options.platform
        or options.implementation
        or options.abi
        or options.dry_run
        or options.report
        or options.require_hashes
        or options.ignore_requires_python
        or options.user
        or options.root
        or options.prefix
        or options.target is None
        or (not options.ignore_installed and not options.upgrade)
        or not options.no_compile
    ):
        return None

    if any(resolve_source_location(value)[1] is None for value in bundle.find_links):
        return None

    values: list[str] = []

    for requirement in requirements:
        if (
            requirement.req is None
            or requirement.req.url is not None
            or requirement.link is not None
            or requirement.hash_options
            or requirement.config_settings
        ):
            return None

        values.append(requirement.req.raw)

        if options.upgrade:
            specifiers = requirement.req.specifier.specifiers

            if (
                len(specifiers) != 1
                or specifiers[0].operator != "=="
                or specifiers[0].version.endswith(".*")
            ):
                return None

    local_plan = ResolutionEngine.resolve_wheelhouse(
        bundle.find_links,
        values,
        cache_dir=cache_dir,
    )

    if local_plan is None:
        return None

    candidates = []

    graph: dict[str, set[str]] = {}

    try:
        for local_candidate in local_plan.candidates:
            dependencies = []

            for dependency in local_candidate.dependencies:
                specifier = ",".join(
                    operator + str(getattr(expected, "text", expected))
                    for operator, expected in dependency.specifier.values
                )

                extras = f"[{','.join(sorted(dependency.extras))}]" if dependency.extras else ""

                marker = ""

                if dependency.marker is not None:
                    operator, value = dependency.marker

                    marker = f"; extra {operator} '{value}'"

                raw = f"{dependency.name}{extras}{specifier}{marker}"

                parsed = Requirement(
                    name=dependency.name,
                    specifier=SpecifierSet(specifier),
                    extras=frozenset(dependency.extras),
                    marker=marker.removeprefix("; ") or None,
                    raw=raw,
                )

                dependencies.append(parsed)

            candidate = WheelCandidate(
                name=local_candidate.name,
                version=Version(str(local_candidate.version)),
                path=local_candidate.path,
                dependencies=tuple(dependencies),
                provided_extras=local_candidate.provided_extras,
                requires_python=local_candidate.requires_python,
                source_kind="wheel",
            )

            candidates.append(candidate)

            graph[candidate.canonical_name] = {
                dependency.canonical_name for dependency in candidate.dependencies
            }

    except (OSError, TypeError, ValueError):
        return None

    if options.upgrade and any(candidate.dependencies for candidate in candidates):
        # The ordinary resolver must decide whether already-satisfied

        # dependencies should move under the selected upgrade strategy.

        return None

    if sum(os.stat(candidate.path).st_size for candidate in candidates) > 4 * 1024 * 1024:
        for index, candidate in enumerate(candidates):
            with zipfile.ZipFile(candidate.path) as archive:
                dist_info, _ = parse_wheel(
                    archive,
                    os.path.basename(candidate.path)[:-4].split("-", 1)[0],
                )

                layout = wheel_candidate(
                    candidate.path,
                    archive=archive,
                    dist_info_dir=dist_info,
                ).wheel_layout

            candidates[index] = candidate.copy_with(wheel_layout=layout)

    return ResolutionResult(
        candidates=tuple(candidates),
        graph={name: frozenset(children) for name, children in graph.items()},
    )
