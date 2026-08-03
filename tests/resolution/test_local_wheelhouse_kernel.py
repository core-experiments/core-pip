from __future__ import annotations

from pathlib import Path

import pytest
from cpip.core.errors import ResolutionError
from cpip.core.packaging import Requirement
from cpip.core.wheel import WheelCandidate
from cpip.index.provider import CandidateProvider
from cpip.resolution.engine import ResolutionEngine
from cpip.resolution.req_install import file_hashes

from .wheel_helpers import make_wheel


def test_local_wheelhouse_kernel_returns_canonical_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "leaf", "leaf", "1.0")
    make_wheel(
        wheelhouse,
        "application",
        "application",
        "1.0",
        requires=["leaf>=1"],
    )

    from cpip.resolution.engine.sources.wheelhouse import engine as wheelhouse_engine

    original_resolve = wheelhouse_engine.resolve
    calls = 0

    def tracked_resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(wheelhouse_engine, "resolve", tracked_resolve)
    result = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
        compute_source_hashes=False,
    ).resolve(["application"])

    assert calls == 1
    assert all(isinstance(candidate, WheelCandidate) for candidate in result.candidates)
    assert all(candidate.source_hashes is None for candidate in result.candidates)
    application = next(
        candidate
        for candidate in result.candidates
        if candidate.canonical_name == "application"
    )
    assert all(isinstance(dependency, Requirement) for dependency in application.dependencies)
    assert result.graph == {
        "<root>": frozenset({"application"}),
        "application": frozenset({"leaf"}),
        "leaf": frozenset(),
    }


def test_local_wheelhouse_kernel_applies_constraint_semantics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo", "demo", "1.0")
    make_wheel(wheelhouse, "demo", "demo", "2.0")

    from cpip.resolution.engine.sources.wheelhouse import engine as wheelhouse_engine

    original_resolve = wheelhouse_engine.resolve
    calls = 0

    def tracked_resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(wheelhouse_engine, "resolve", tracked_resolve)
    result = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
        constraints=["demo<2"],
        compute_source_hashes=False,
    ).resolve(["demo"])

    assert calls == 1
    assert [str(candidate.version) for candidate in result.candidates] == ["1.0"]


def test_local_wheelhouse_kernel_reuses_metadata_read_for_source_hash(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo", "demo", "1.0")

    result = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    ).resolve(["demo"])

    assert result.candidates[0].source_hashes == file_hashes(wheel)


def test_conflicting_root_bounds_skip_candidate_discovery(monkeypatch) -> None:
    provider = CandidateProvider.from_options(no_index=True)

    def unexpected_find_candidates(*args, **kwargs):
        raise AssertionError("candidate discovery should not run")

    monkeypatch.setattr(provider, "find_candidates", unexpected_find_candidates)
    with pytest.raises(ResolutionError, match="conflicting dependencies"):
        ResolutionEngine(provider=provider, ignore_installed=True).resolve(
            ["demo==1", "demo==2"],
        )
