"""Public orchestration for the canonical resolution engine."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable

from cpip.core.errors import ResolutionError as CpipResolutionError
from cpip.index.provider import CandidateProvider
from cpip.resolution.inputs import (
    coerce_requirements,
)
from cpip.resolution.models import (
    ResolutionConfig,
    ResolutionResult,
    ResolvedRequirement,
)
from cpip.resolution.nab_provider import NabProvider
from cpip.resolution.nab_types import InstalledCandidate

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from cpip.install.requirement_set import RequirementSet
    from cpip.resolution.req_install import InstallRequirement


class ResolutionEngine:
    """Canonical resolution entry point.



    The current generic search implementation is used as the authoritative

    state machine while candidate-source and propagation boundaries are

    migrated behind this API.  Keeping orchestration here small prevents the

    public entry point from becoming another resolver context.

    """

    def __init__(
        self,
        *,
        config: ResolutionConfig | None = None,
        **kwargs: Any,
    ) -> None:
        if config is None:
            index_urls = kwargs.pop("index_urls", None)

            config = ResolutionConfig(
                find_links=tuple(kwargs.pop("find_links", ()) or ()),
                index_urls=tuple(index_urls) if index_urls is not None else None,
                no_index=kwargs.pop("no_index", False),
                no_deps=kwargs.pop("no_deps", False),
                upgrade=kwargs.pop("upgrade", False),
                ignore_installed=kwargs.pop("ignore_installed", False),
                constraints=tuple(kwargs.pop("constraints", ()) or ()),
                allow_prereleases=kwargs.pop("allow_prereleases", False),
                require_hashes=kwargs.pop("require_hashes", False),
                compute_source_hashes=kwargs.pop("compute_source_hashes", True),
                upgrade_strategy=kwargs.pop("upgrade_strategy", "only-if-needed"),
                ignore_requires_python=kwargs.pop("ignore_requires_python", False),
                python_version=kwargs.pop("python_version", None),
            )

        self.config = config

        provider = kwargs.pop("provider", None)
        if provider is None:
            provider = CandidateProvider.from_options(
                find_links=list(config.find_links),
                index_url=config.index_urls[0] if config.index_urls else None,
                extra_index_urls=config.index_urls[1:] if config.index_urls else (),
                no_index=config.no_index,
            )
        self.provider = provider

        if kwargs:
            unexpected = ", ".join(sorted(kwargs))

            raise TypeError(f"unexpected resolution options: {unexpected}")

    def resolve(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> ResolutionResult:
        from cpip._vendor.nab_resolver.resolver import Resolver

        requirements = coerce_requirements(requirements_input)
        adapter = NabProvider(self.provider, context=self.config)
        roots = adapter.add_roots(requirements)
        resolver = Resolver(adapter, root_version="0")
        try:
            selected = resolver.resolve(roots)
        except Exception as error:
            from cpip._vendor.nab_resolver.errors import (
                ResolutionError as NabResolutionError,
            )
            from cpip._vendor.nab_resolver.report import format_error

            if isinstance(error, NabResolutionError):
                message = (
                    format_error(
                        error.incompatibility,
                        narrow=adapter.narrow_for_display,
                    )
                    if error.incompatibility is not None
                    else str(error)
                )

                # The provider represents dependency ranges as the finite set
                # of available versions.  When that set is empty, the report
                # loses the user's original specifier and prints ``<empty>``.
                # Restore it for actionable CLI diagnostics.
                def restore_requirement(match: re.Match[str]) -> str:
                    name = match.group(1)
                    requirement = adapter.display_requirements.get(
                        name
                    ) or adapter.requirements.get(name)
                    if requirement is None or not str(requirement.specifier):
                        return match.group(0)
                    return f"depends on {name}{requirement.specifier}"

                message = re.sub(
                    r"depends on ([A-Za-z0-9_.-]+)(?: <empty>)?",
                    restore_requirement,
                    message,
                )
                raise CpipResolutionError(message) from error
            raise
        selected_records = tuple(
            (package, adapter.records[(package, version)])
            for package, version in selected.items()
        )
        graph: dict[str, frozenset[str]] = {}
        for package, version in selected.items():
            graph[package] = frozenset(adapter.get_dependencies(package, version))
        satisfied = tuple(
            ResolvedRequirement(adapter.requirements[package], record.distribution)
            for package, record in selected_records
            if isinstance(record, InstalledCandidate)
        )
        candidates = tuple(
            record
            for _, record in selected_records
            if not isinstance(record, InstalledCandidate)
        )
        result = ResolutionResult(
            candidates=candidates,
            graph=graph,
            satisfied=satisfied,
            metrics={
                "nab_rounds": resolver.stats.rounds,
                "nab_conflicts": resolver.stats.conflicts,
            },
        )

        if os.environ.get("CPIP_RESOLUTION_STATS") == "1":
            print(
                json.dumps({"cpip_resolution": dict(result.metrics)}, sort_keys=True),
                file=sys.stderr,
            )

        return result

    @staticmethod
    def resolve_wheelhouse(
        find_links: list[str],
        requirements: list[str],
        *,
        constraints: list[str] | None = None,
    ) -> ResolutionResult | None:
        """Resolve a local wheel directory through the normal nab path."""

        resolver = ResolutionEngine(
            find_links=tuple(find_links),
            no_index=True,
            constraints=tuple(constraints or ()),
            ignore_installed=True,
        )
        try:
            return resolver.resolve(requirements)
        finally:
            resolver.close()

    def close(self) -> None:
        self.provider.close()


__all__ = ["ResolutionConfig", "ResolutionEngine"]
