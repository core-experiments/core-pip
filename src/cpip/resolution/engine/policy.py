"""Candidate policy checks and resolver diagnostics."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from cpip.core.errors import InstallationError, ResolutionError
from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    canonicalize_name,
    marker_applies,
)
from cpip.core.wheel import WheelCandidate
from cpip.resolution.engine.algorithms import is_pypi_hosted_url
from cpip.resolution.engine.validation import ValidationOperations

SOURCE_KINDS = frozenset(("source-tree", "sdist", "vcs"))

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.resolution.engine.context import ConfigurationContext


class PolicyOperations:
    """Candidate policy and resolver diagnostic operations."""

    def upgrade_allowed_for(self: ConfigurationContext, name: str) -> bool:
        if not self.upgrade:
            return False
        if self.upgrade_strategy == "eager":
            return True
        return name in self.root_requirement_names

    def validate_candidate_policy(
        self: ConfigurationContext,
        candidate: WheelCandidate,
    ) -> None:
        self.validate_requires_python(candidate)
        self.validate_external_url_dependencies(candidate)
        if candidate.yanked_reason is not None:
            reason = candidate.yanked_reason or "<none given>"
            print(
                f"WARNING: The candidate selected is a yanked version: {candidate.name}=={candidate.version}",
                file=sys.stderr,
            )
            print(f"Reason for being yanked: {reason}", file=sys.stderr)

    def validate_requires_python(
        self: ConfigurationContext,
        candidate: WheelCandidate,
    ) -> None:
        if self.ignore_requires_python:
            return
        if not candidate.requires_python:
            return
        python_version = self.python_version
        try:
            matches = SpecifierSet(candidate.requires_python).contains(python_version)
        except ValueError:
            return
        if matches:
            return
        raise InstallationError(
            f"Package '{candidate.name}' requires a different Python: "
            f"{python_version} not in '{candidate.requires_python}'",
        )

    def validate_external_url_dependencies(
        self: ConfigurationContext,
        candidate: WheelCandidate,
    ) -> None:
        if not is_pypi_hosted_url(candidate.source_url):
            return
        for dependency in candidate.dependencies:
            if dependency.url is None or is_pypi_hosted_url(dependency.url):
                continue
            raise InstallationError(
                "Packages installed from PyPI cannot depend on packages "
                "which are not also hosted on PyPI.\n"
                f"{candidate.name} depends on {dependency}",
            )

    def validate_candidate_constraints(
        self: ConfigurationContext,
        candidate: WheelCandidate,
    ) -> None:
        matching = [
            constraint
            for constraint in self.constraints_by_name.get(candidate.canonical_name, ())
            if marker_applies(constraint.marker, extras=())
        ]
        for constraint in matching:
            if not constraint.is_satisfied_by(
                candidate.version,
                allow_prereleases=True,
            ):
                if candidate.source_kind in SOURCE_KINDS:
                    raise ResolutionError(
                        f"Cannot install {candidate.name} {candidate.version} "
                        "because it conflicts with a constraint.",
                    )
                raise ResolutionError(
                    f"Cannot install {candidate.name} {candidate.version} because these "
                    "package versions have conflicting dependencies.",
                )

    def warn_missing_candidate_extras(
        self: ConfigurationContext,
        requirement: Requirement,
        candidate: WheelCandidate,
    ) -> None:
        if requirement.url is not None and requirement.name.startswith("file://"):
            return
        self.warn_missing_extras(
            candidate.name,
            requirement.extras,
            candidate.provided_extras,
            version=str(candidate.version),
        )

    def warn_missing_installed_extras(
        self: ConfigurationContext,
        requirement: Requirement,
        installed: InstalledDistribution,
    ) -> None:
        provided = frozenset(
            canonicalize_name(value.strip())
            for value in installed.raw.metadata.get_all("Provides-Extra", [])
            if value.strip()
        )
        self.warn_missing_extras(
            requirement.name,
            requirement.extras,
            provided,
            version=installed.version,
        )

    def warn_missing_extras(
        self: ConfigurationContext,
        project_name: str,
        requested: frozenset[str],
        provided: frozenset[str],
        *,
        version: str | None = None,
    ) -> None:
        if not requested:
            return
        normalized_provided = {canonicalize_name(extra) for extra in provided}
        for extra in sorted(requested):
            normalized = canonicalize_name(extra)
            key = (canonicalize_name(project_name), normalized)
            if normalized in normalized_provided or key in self.warned_missing_extras:
                continue
            version_text = f" {version}" if version is not None else ""
            print(
                f"WARNING: {project_name}{version_text} "
                f"{self.does_not_provide_extra_text(extra)}",
                file=sys.stderr,
            )
            self.warned_missing_extras.add(key)

    @staticmethod
    def does_not_provide_extra_text(extra: str) -> str:
        return f"does not provide the extra '{extra}'"

    def no_matching_distribution_message(
        self: ConfigurationContext,
        requirement: Requirement,
    ) -> str:
        summaries = self.provider.available_versions(requirement)
        final_only = (
            self.provider.release_control is not None
            and self.provider.release_control.allows_prereleases(requirement.name)
            is False
        )
        non_yanked_versions = sorted(
            {
                str(summary.version): summary.version
                for summary in summaries
                if not summary.is_yanked
            }.values(),
        )
        yanked_versions = sorted(
            {
                str(summary.version): summary.version
                for summary in summaries
                if summary.is_yanked
            }.values(),
        )
        if not non_yanked_versions:
            return (
                f"Could not find a version that satisfies the requirement "
                f"{requirement.raw or requirement.name} (from versions: none)\n"
                f"No matching distribution found for {requirement.raw or requirement.name}"
            )
        version_label = "a final version" if final_only else "a version"
        message = (
            f"Could not find {version_label} that satisfies the requirement "
            f"{requirement.raw or requirement.name} (from versions: "
            + ", ".join(str(version) for version in non_yanked_versions)
            + ")"
        )
        if yanked_versions:
            message += "\nIgnored the following yanked versions: " + ", ".join(
                str(version) for version in yanked_versions
            )
        return (
            message
            + f"\nNo matching distribution found for {requirement.raw or requirement.name}"
        )


class PolicyChecks(PolicyOperations, ValidationOperations):
    """Complete candidate checks: policy, compatibility, and hashes."""
