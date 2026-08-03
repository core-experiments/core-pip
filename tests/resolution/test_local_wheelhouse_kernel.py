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
    assert all(
        isinstance(dependency, Requirement) for dependency in application.dependencies
    )
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


def test_local_wheelhouse_kernel_shares_metadata_with_generic_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "leaf", "leaf", "2.0")
    make_wheel(
        wheelhouse,
        "application",
        "application",
        "1.0",
        requires=["leaf==1.0"],
    )

    from cpip.core import wheel as wheel_module
    from cpip.resolution.engine import propagation
    from cpip.resolution.engine.sources.wheelhouse import engine as wheelhouse_engine

    wheel_module.wheel_metadata_cache.clear()
    wheel_module.preloaded_wheel_metadata_cache.clear()
    monkeypatch.setattr(propagation, "_kernel_failure_cache", {})
    metadata_reads: list[str] = []
    original_read = wheel_module.read_core_metadata_headers
    original_resolve = wheelhouse_engine.resolve
    fast_resolves = 0

    def tracked_read(archive, path, dist_info_dir):
        metadata_reads.append(path)
        return original_read(archive, path, dist_info_dir)

    def tracked_resolve(*args, **kwargs):
        nonlocal fast_resolves
        fast_resolves += 1
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(wheel_module, "read_core_metadata_headers", tracked_read)
    monkeypatch.setattr(wheelhouse_engine, "resolve", tracked_resolve)

    messages = []
    for _ in range(2):
        with pytest.raises(ResolutionError) as error:
            ResolutionEngine(
                provider=CandidateProvider.from_options(
                    find_links=[str(wheelhouse)],
                    no_index=True,
                ),
                ignore_installed=True,
            ).resolve(["application"])
        messages.append(str(error.value))

    assert metadata_reads == []
    assert fast_resolves == 1
    assert messages[0] == messages[1]

    make_wheel(wheelhouse, "leaf", "leaf", "1.0")
    result = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    ).resolve(["application"])
    assert fast_resolves == 2
    assert {candidate.canonical_name for candidate in result.candidates} == {
        "application",
        "leaf",
    }


def test_conflicting_root_bounds_skip_candidate_discovery(monkeypatch) -> None:
    provider = CandidateProvider.from_options(no_index=True)

    def unexpected_find_candidates(*args, **kwargs):
        raise AssertionError("candidate discovery should not run")

    monkeypatch.setattr(provider, "find_candidates", unexpected_find_candidates)
    with pytest.raises(ResolutionError, match="conflicting dependencies"):
        ResolutionEngine(provider=provider, ignore_installed=True).resolve(
            ["demo==1", "demo==2"],
        )
