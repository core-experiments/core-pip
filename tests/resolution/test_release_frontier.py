from __future__ import annotations

from pathlib import Path

from cpip.core.packaging import Version, parse_requirement
from cpip.index.provider import CandidateProvider
from cpip.resolution.engine import ResolutionEngine

from .wheel_helpers import make_wheel


def test_release_frontier_reuses_catalog_and_intersects_versions(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    for version in ("1.0", "2.0", "3.0"):
        make_wheel(wheelhouse, "frontier-demo", "frontier_demo", version)

    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    resolver = ResolutionEngine(provider=provider)
    provider.available_versions(parse_requirement("frontier-demo"))

    assert resolver.release_frontier.allowed_versions(
        parse_requirement("frontier-demo>=2"),
        allow_prereleases=False,
    ) == frozenset((Version("2.0"), Version("3.0")))
    assert resolver.release_frontier.allowed_versions(
        parse_requirement("frontier-demo<2"),
        allow_prereleases=False,
    )
    assert resolver.release_frontier.metrics.catalogs_loaded == 1
    assert resolver.release_frontier.metrics.catalog_hits > 0


def test_resolution_reports_release_frontier_metrics(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(
        wheelhouse,
        "frontier-root",
        "frontier_root",
        "1.0",
        requires=["frontier-leaf"],
    )
    make_wheel(wheelhouse, "frontier-leaf", "frontier_leaf", "1.0")

    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    provider.available_versions(parse_requirement("frontier-root"))
    result = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["frontier-root"],
    )

    assert result.metrics["catalogs_loaded"] >= 1
    assert result.metrics["release_masks_built"] >= 1
    assert result.metrics["release_intersections"] >= 1
