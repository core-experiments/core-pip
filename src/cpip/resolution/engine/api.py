"""Public orchestration for the canonical resolution engine."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cpip.core.packaging import Requirement
from cpip.resolution.engine.model import ResolutionResult
from cpip.resolution.engine.runtime import ResolutionRuntime

if TYPE_CHECKING:
    from cpip.resolution.engine.state.requirement_set import RequirementSet
    from cpip.resolution.req_install import InstallRequirement


@dataclass(frozen=True, slots=True)
class ResolutionConfig:
    """Immutable resolver configuration passed to one engine invocation."""

    find_links: tuple[str, ...] = ()
    index_urls: tuple[str, ...] | None = None
    no_index: bool = False
    no_deps: bool = False
    upgrade: bool = False
    ignore_installed: bool = False
    constraints: tuple[str, ...] = ()
    allow_prereleases: bool = False
    require_hashes: bool = False
    compute_source_hashes: bool = True
    upgrade_strategy: str = "only-if-needed"
    ignore_requires_python: bool = False
    python_version: str | None = None


class ResolutionEngine(ResolutionRuntime):
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
        super().__init__(
            provider=kwargs.pop("provider", None),
            find_links=list(config.find_links),
            index_urls=list(config.index_urls)
            if config.index_urls is not None
            else None,
            no_index=config.no_index,
            no_deps=config.no_deps,
            upgrade=config.upgrade,
            ignore_installed=config.ignore_installed,
            constraints=list(config.constraints),
            allow_prereleases=config.allow_prereleases,
            require_hashes=config.require_hashes,
            compute_source_hashes=config.compute_source_hashes,
            upgrade_strategy=config.upgrade_strategy,
            ignore_requires_python=config.ignore_requires_python,
            python_version=config.python_version,
        )
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"unexpected resolution options: {unexpected}")

    def resolve(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> ResolutionResult:
        result = ResolutionResult.from_plan(super().resolve_plan(requirements_input))
        if os.environ.get("CPIP_RESOLUTION_STATS") == "1":
            print(
                json.dumps({"cpip_resolution": dict(result.metrics)}, sort_keys=True),
                file=sys.stderr,
            )
        return result

    def resolve_requirement_set(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> RequirementSet[InstallRequirement]:
        return super().resolve_requirement_set(requirements_input)

    @staticmethod
    def resolve_wheelhouse(
        find_links: list[str],
        requirements: list[str],
        *,
        cache_dir: str | None = None,
        constraints: list[str] | None = None,
    ) -> ResolutionResult | None:
        """Resolve a pure-wheel directory through the same result boundary."""
        from cpip.resolution.engine.sources.wheelhouse.engine import resolve

        candidates = resolve(
            find_links,
            requirements,
            cache_dir=cache_dir,
            constraints=constraints,
        )
        return (
            None if candidates is None else ResolutionResult.from_candidates(candidates)
        )

    def coerce_requirements(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> list[Requirement]:
        return super().coerce_requirements(requirements_input)

    def close(self) -> None:
        self.provider.close()


__all__ = ["ResolutionConfig", "ResolutionEngine"]
