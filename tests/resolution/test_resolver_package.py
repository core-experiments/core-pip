from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from cpip.core.errors import (
    DirectoryUrlHashUnsupported,
    DistributionNotFound,
    HashMismatch,
    HashMissing,
    HashUnpinned,
    ResolutionError,
    VcsHashUnsupported,
)
from cpip.core.format_control import FormatControl
from cpip.core.packaging import Requirement, Version, parse_requirement
from cpip.core.wheel import TargetContext, WheelCandidate
from cpip.index.cache import wheel_cache_path
from cpip.index.candidate_materialization import CandidateStream, LazyWheelCandidate
from cpip.index.provider import CandidateProvider
from cpip.index.source_models import CandidateSummary
from cpip.resolution.engine import ResolutionEngine
from cpip.resolution.engine.algorithms import is_pypi_hosted_url
from cpip.resolution.engine.input.requirements import (
    install_req_from_editable,
    install_req_from_line,
)
from cpip.resolution.engine.state.agenda import PendingAgenda
from cpip.resolution.engine.state.domains import (
    LearnedIncompatibility,
    PackageDomain,
)
from cpip.resolution.engine.state.requests import (
    SearchFrame,
    SearchRequest,
)
from cpip.resolution.engine.state.requirement_set import RequirementSet
from cpip.resolution.req_install import file_hashes

from .wheel_helpers import make_sdist, make_wheel


class CountingFailedResolver(ResolutionEngine):
    def __init__(self) -> None:
        super().__init__(no_index=True)
        self.uncached_searches = 0

    def search_uncached(  # type: ignore[override]
        self,
        *args: object,
        **kwargs: object,
    ) -> SearchFrame:
        self.uncached_searches += 1
        if False:
            yield
        return False


def test_pending_agenda_rolls_back_nested_mutations() -> None:
    original = [parse_requirement(name) for name in ("first", "second", "third")]
    agenda = PendingAgenda(original)
    original_state = agenda.state_key()
    root = agenda.checkpoint()
    entries = list(agenda.iter_entries())
    agenda.remove(entries[1][0])
    agenda.prepend(parse_requirement(name) for name in ("before-a", "before-b"))
    branch = agenda.checkpoint()
    agenda.remove(agenda.first()[0])
    agenda.prepend((parse_requirement("nested"),))

    agenda.rollback(branch)
    assert [item.name for item in agenda] == [
        "before-a",
        "before-b",
        "first",
        "third",
    ]
    assert len(agenda.state_key()) == 4

    agenda.rollback(root)
    assert list(agenda) == original
    assert agenda.state_key() == original_state
    assert len(agenda.entries_internal) == len(original)
    assert set(agenda.by_name) == {"first", "second", "third"}


def test_finite_domain_kernel_matches_generic_on_wide_roots(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "kernel-packages"
    wheelhouse.mkdir()
    for index in range(32):
        make_wheel(
            wheelhouse,
            f"kernel-{index}",
            f"kernel_{index}",
            "1.0",
        )

    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
        compute_source_hashes=False,
    )
    plan = resolver.resolve([f"kernel-{index}" for index in range(32)])

    assert len(plan.candidates) == 32
    baseline_resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
        compute_source_hashes=False,
    )
    baseline_resolver.kernel_enabled = False
    baseline = baseline_resolver.resolve([f"kernel-{index}" for index in range(32)])
    assert {
        (candidate.canonical_name, str(candidate.version))
        for candidate in plan.candidates
    } == {
        (candidate.canonical_name, str(candidate.version))
        for candidate in baseline.candidates
    }


def test_finite_domain_kernel_backtracks_and_matches_generic(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "kernel-backtracking"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "shared", "1.0")
    make_wheel(wheelhouse, "shared", "shared", "1.1")
    make_wheel(wheelhouse, "left", "left", "1.0", requires=["shared==1.0"])
    make_wheel(wheelhouse, "left", "left", "2.0", requires=["shared==1.1"])
    make_wheel(wheelhouse, "right", "right", "1.0", requires=["shared==1.1"])
    make_wheel(wheelhouse, "right", "right", "2.0", requires=["shared==1.0"])
    make_wheel(
        wheelhouse,
        "kernel-root",
        "kernel_root",
        "1.0",
        requires=["left>=1", "right>=1"],
    )
    for index in range(31):
        make_wheel(wheelhouse, f"leaf-{index}", f"leaf_{index}", "1.0")

    requirements = ["kernel-root", *(f"leaf-{index}" for index in range(31))]
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    kernel_plan = resolver.resolve(requirements)

    baseline_resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    baseline_resolver.kernel_enabled = False
    baseline_plan = baseline_resolver.resolve(requirements)

    def validate(plan: object) -> set[str]:
        candidates = plan.candidates  # type: ignore[attr-defined]
        selected = {candidate.canonical_name: candidate for candidate in candidates}
        for candidate in candidates:
            for dependency in candidate.dependencies:
                dependency_candidate = selected[dependency.canonical_name]
                assert dependency.is_satisfied_by(
                    dependency_candidate.version,
                    allow_prereleases=True,
                )
        return set(selected)

    assert validate(kernel_plan) == validate(baseline_plan)


def test_finite_domain_kernel_supports_environment_markers(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "kernel-markers"
    wheelhouse.mkdir()
    make_wheel(
        wheelhouse,
        "marker-root",
        "marker_root",
        "1.0",
        requires=["marker-leaf>=1; python_version >= '3.0'"],
    )
    make_wheel(wheelhouse, "marker-leaf", "marker_leaf", "1.0")
    for index in range(31):
        make_wheel(wheelhouse, f"marker-independent-{index}", "marker", "1.0")

    requirements = [
        "marker-root",
        *(f"marker-independent-{index}" for index in range(31)),
    ]
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    kernel_plan = resolver.resolve(requirements)

    baseline_resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    baseline_resolver.kernel_enabled = False
    baseline_plan = baseline_resolver.resolve(requirements)

    assert {candidate.canonical_name for candidate in kernel_plan.candidates} == {
        candidate.canonical_name for candidate in baseline_plan.candidates
    }


def test_finite_domain_kernel_supports_root_extras(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "kernel-extras"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "extra-all", "extra_all", "1.0")
    make_wheel(wheelhouse, "extra-dev", "extra_dev", "1.0")
    make_wheel(
        wheelhouse,
        "extras-root",
        "extras_root",
        "1.0",
        requires=[
            "extra-all>=1; extra == 'all'",
            "extra-dev>=1; extra == 'dev'",
        ],
        provides_extra=["all", "dev"],
    )
    for index in range(31):
        make_wheel(wheelhouse, f"extra-independent-{index}", "extra", "1.0")

    requirements = [
        "extras-root[all,dev]",
        *(f"extra-independent-{index}" for index in range(31)),
    ]
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    kernel_plan = resolver.resolve(requirements)

    baseline_resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    baseline_resolver.kernel_enabled = False
    baseline_plan = baseline_resolver.resolve(requirements)

    kernel_names = {candidate.canonical_name for candidate in kernel_plan.candidates}
    baseline_names = {
        candidate.canonical_name for candidate in baseline_plan.candidates
    }
    assert kernel_names == baseline_names
    assert "extra-all" in kernel_names
    assert "extra-dev" in kernel_names


def test_finite_domain_kernel_resolves_lazy_index_domains(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    root_archives = []
    for version in range(1, 101):
        root_archives.append(
            make_wheel(
                packages,
                "remote-root",
                "remote_root",
                f"{version}.0",
                requires=["remote-child[feature]>=1"] if version == 100 else [],
            ),
        )
    child = make_wheel(
        packages,
        "remote-child",
        "remote_child",
        "1.0",
        requires=["remote-extra>=1; extra == 'feature'"],
        provides_extra=["feature"],
    )
    extra = make_wheel(packages, "remote-extra", "remote_extra", "1.0")
    independent = make_wheel(
        packages,
        "remote-independent",
        "remote_independent",
        "1.0",
    )
    write_simple_project_archive_index(index, "remote-root", root_archives)
    write_simple_project_archive_index(index, "remote-child", [child])
    write_simple_project_archive_index(index, "remote-extra", [extra])
    write_simple_project_archive_index(
        index,
        "remote-independent",
        [independent],
    )

    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(index_url=index.as_uri()),
        ignore_installed=True,
    )
    plan = resolver.resolve(["remote-root", "remote-independent"])

    selected = {
        candidate.canonical_name: str(candidate.version)
        for candidate in plan.candidates
    }
    assert selected == {
        "remote-root": "100.0",
        "remote-child": "1.0",
        "remote-extra": "1.0",
        "remote-independent": "1.0",
    }
    assert plan.metrics["search_frames"] == 0
    assert plan.metrics["metadata_loads"] <= 4


def test_finite_domain_kernel_matches_generic_across_targets(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "kernel-targets"
    wheelhouse.mkdir()
    for index in range(32):
        make_wheel(
            wheelhouse,
            f"target-independent-{index}",
            "target_independent",
            "1.0",
        )
    requirements = [f"target-independent-{index}" for index in range(32)]

    for target in (
        TargetContext(platforms=("linux_x86_64",), python_version="3.11"),
        TargetContext(platforms=("win_amd64",), python_version="3.12"),
        TargetContext(platforms=("macosx_11_0_arm64",), python_version="3.12"),
    ):
        resolver = ResolutionEngine(
            provider=CandidateProvider.from_options(
                find_links=[str(wheelhouse)],
                no_index=True,
                target=target,
            ),
            ignore_installed=True,
        )
        kernel_plan = resolver.resolve(requirements)
        baseline_resolver = ResolutionEngine(
            provider=CandidateProvider.from_options(
                find_links=[str(wheelhouse)],
                no_index=True,
                target=target,
            ),
            ignore_installed=True,
        )
        baseline_resolver.kernel_enabled = False
        baseline_plan = baseline_resolver.resolve(requirements)
        assert {candidate.canonical_name for candidate in kernel_plan.candidates} == {
            candidate.canonical_name for candidate in baseline_plan.candidates
        }


def test_finite_domain_kernel_repeats_conflicts_across_root_releases(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "kernel-repeated-conflicts"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "shared", "1.0")
    make_wheel(wheelhouse, "shared", "shared", "1.1")
    make_wheel(wheelhouse, "left", "left", "1.0", requires=["shared==1.0"])
    make_wheel(wheelhouse, "left", "left", "2.0", requires=["shared==1.1"])
    make_wheel(wheelhouse, "right", "right", "1.0", requires=["shared==1.0"])
    make_wheel(wheelhouse, "right", "right", "2.0", requires=["shared==1.0"])
    make_wheel(
        wheelhouse,
        "repeated-root",
        "repeated_root",
        "1.0",
        requires=["left==1", "right>=1"],
    )
    make_wheel(
        wheelhouse,
        "repeated-root",
        "repeated_root",
        "2.0",
        requires=["left==2", "right>=1"],
    )
    for index in range(31):
        make_wheel(wheelhouse, f"repeated-leaf-{index}", "repeated_leaf", "1.0")

    requirements = [
        "repeated-root",
        *(f"repeated-leaf-{index}" for index in range(31)),
    ]
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    kernel_plan = resolver.resolve(requirements)

    baseline_resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    baseline_resolver.kernel_enabled = False
    baseline_plan = baseline_resolver.resolve(requirements)

    assert {candidate.canonical_name for candidate in kernel_plan.candidates} == {
        candidate.canonical_name for candidate in baseline_plan.candidates
    }


def test_finite_domain_kernel_performs_nonchronological_backjump(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "kernel-backjump"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "shared", "1.0")
    make_wheel(wheelhouse, "shared", "shared", "1.1")
    make_wheel(wheelhouse, "noise", "noise", "1.0")
    make_wheel(wheelhouse, "noise", "noise", "2.0")
    make_wheel(wheelhouse, "right", "right", "1.0", requires=["shared==1.0"])
    make_wheel(wheelhouse, "right", "right", "2.0", requires=["shared==1.0"])
    make_wheel(
        wheelhouse,
        "left",
        "left",
        "1.0",
        requires=["shared==1.0", "noise>=1"],
    )
    make_wheel(
        wheelhouse,
        "left",
        "left",
        "2.0",
        requires=["shared==1.1", "noise>=1"],
    )
    make_wheel(
        wheelhouse,
        "backjump-root",
        "backjump_root",
        "1.0",
        requires=["left>=1", "right>=1"],
    )
    for index in range(31):
        make_wheel(wheelhouse, f"backjump-leaf-{index}", "backjump_leaf", "1.0")

    requirements = [
        "backjump-root",
        *(f"backjump-leaf-{index}" for index in range(31)),
    ]
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    kernel_plan = resolver.resolve(requirements)

    baseline_resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    baseline_resolver.kernel_enabled = False
    baseline_plan = baseline_resolver.resolve(requirements)

    assert {candidate.canonical_name for candidate in kernel_plan.candidates} == {
        candidate.canonical_name for candidate in baseline_plan.candidates
    }


def test_finite_domain_kernel_resolves_exhausted_singleton_causes(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "kernel-singleton-resolution"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "shared", "1.0")
    make_wheel(wheelhouse, "shared", "shared", "2.0")
    make_wheel(
        wheelhouse,
        "anchor",
        "anchor",
        "1.0",
        requires=["shared==1.0"],
    )
    for version in ("1.0", "2.0"):
        make_wheel(
            wheelhouse,
            "trigger",
            "trigger",
            version,
            requires=["shared==2.0"],
        )
    noise = [f"branch-noise-{index}" for index in range(8)]
    for name in noise:
        make_wheel(wheelhouse, name, "branch_noise", "1.0")
        make_wheel(wheelhouse, name, "branch_noise", "2.0")
    make_wheel(wheelhouse, "gate", "gate", "1.0")
    make_wheel(
        wheelhouse,
        "gate",
        "gate",
        "2.0",
        requires=["trigger>=1", *(f"{name}>=1" for name in noise)],
    )
    make_wheel(
        wheelhouse,
        "singleton-root",
        "singleton_root",
        "1.0",
        requires=["gate==1.0"],
    )
    make_wheel(
        wheelhouse,
        "singleton-root",
        "singleton_root",
        "2.0",
        requires=["gate==2.0"],
    )
    for index in range(30):
        make_wheel(wheelhouse, f"singleton-leaf-{index}", "leaf", "1.0")

    requirements = [
        "singleton-root",
        "anchor",
        *(f"singleton-leaf-{index}" for index in range(30)),
    ]
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    plan = resolver.resolve(requirements)
    selected = {candidate.canonical_name: candidate for candidate in plan.candidates}

    assert selected["singleton-root"].version == Version("1.0")
    assert selected["gate"].version == Version("1.0")
    assert selected["shared"].version == Version("1.0")
    assert resolver.metrics.backjumps >= 1
    assert resolver.metrics.conflicts < 16


def test_pending_agenda_maintains_wide_state_key_incrementally() -> None:
    requirements = [parse_requirement(f"package-{index}") for index in range(20)]
    agenda = PendingAgenda(requirements)
    original = agenda.state_key()
    checkpoint = agenda.checkpoint()
    agenda.remove(list(agenda.iter_entries())[10][0])
    agenda.prepend((parse_requirement("replacement"),))

    assert len(agenda.state_key()) == 20

    agenda.rollback(checkpoint)
    assert agenda.state_key() == original


def test_failed_search_frame_restores_pending_agenda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    requirement = parse_requirement("demo")
    agenda = PendingAgenda((requirement,))

    def fail_search(*args: object, **kwargs: object) -> SearchFrame:
        pending = args[0]
        pending.remove(pending.first()[0])  # type: ignore[attr-defined]
        if False:
            yield
        return False

    monkeypatch.setattr(resolver, "search_uncached", fail_search)
    request = SearchRequest(agenda, {}, {}, {}, {"<root>": set()}, {}, {})
    frame = resolver.search_frame_internal(request)

    with pytest.raises(StopIteration) as completed:
        next(frame)

    assert completed.value.value is False
    assert list(agenda) == [requirement]


def test_resolver_search_driver_is_iterative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)

    def search_frame(request: SearchRequest) -> SearchFrame:
        if request.pending:
            entry_id, _ = request.pending.first()
            checkpoint = request.pending.checkpoint()
            request.pending.remove(entry_id)
            result = yield type(request)(
                request.pending,
                request.selected,
                request.selected_extras,
                request.satisfied,
                request.graph,
                request.source_requirements,
                request.source_requirements_by_url,
                checkpoint=checkpoint,
            )
            return result
        return True

    monkeypatch.setattr(resolver, "search_frame_internal", search_frame)
    pending = [parse_requirement(f"package-{index}") for index in range(2_000)]

    assert resolver.search_internal(
        pending,
        {},
        {},
        {},
        {"<root>": set()},
        source_requirements={},
        source_requirements_by_url={},
    )


def test_non_http_source_skips_pypi_host_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_url_parse(url: str) -> None:
        raise AssertionError(f"parsed non-HTTP package URL: {url}")

    monkeypatch.setattr(
        "cpip.resolution.engine.runtime.urllib.parse.urlparse",
        fail_url_parse,
    )

    assert not is_pypi_hosted_url("file:///packages/demo-1.0.whl")


def test_resolver_caches_prerelease_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    requirement = parse_requirement("demo-pkg>=1")

    assert not resolver.allow_prereleases_internal(requirement)

    def fail_direct_check(requirement: Requirement) -> bool:
        raise AssertionError(f"recomputed prerelease policy: {requirement}")

    monkeypatch.setattr(
        "cpip.resolution.engine.runtime.is_direct_requirement",
        fail_direct_check,
    )

    assert not resolver.allow_prereleases_internal(parse_requirement("demo-pkg>=1"))


def test_resolver_indexes_installed_distributions_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def installed_distributions() -> list[object]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(
        "cpip.resolution.engine.selection.iter_installed_distributions",
        installed_distributions,
    )
    resolver = ResolutionEngine(no_index=True)

    assert resolver.find_installed_internal("first") is None
    assert resolver.find_installed_internal("second") is None
    assert calls == 1


def test_file_hashes_streams_file_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.whl"
    payload = b"a" * (2 * 1024 * 1024 + 1)
    artifact.write_bytes(payload)

    def fail_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"read entire artifact into memory: {path}")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    assert file_hashes(artifact)["sha256"] == hashlib.sha256(payload).hexdigest()


def test_resolver_memoizes_equivalent_failed_search_states() -> None:
    resolver = CountingFailedResolver()
    pending = [resolver.apply_constraints(parse_requirement("demo-pkg>=1"))]
    search_args = (pending, {}, {}, {}, {"<root>": set()})
    search_kwargs = {
        "source_requirements": {},
        "source_requirements_by_url": {},
    }

    assert not resolver.search_internal(*search_args, **search_kwargs)
    assert not resolver.search_internal(*search_args, **search_kwargs)

    assert resolver.uncached_searches == 1


def test_failed_search_state_is_independent_of_graph_history() -> None:
    resolver = CountingFailedResolver()
    pending = [resolver.apply_constraints(parse_requirement("demo-pkg>=1"))]
    search_kwargs = {
        "source_requirements": {},
        "source_requirements_by_url": {},
    }

    assert not resolver.search_internal(
        pending,
        {},
        {},
        {},
        {"<root>": {"demo-pkg"}},
        **search_kwargs,
    )
    assert not resolver.search_internal(
        pending,
        {},
        {},
        {},
        {"<root>": {"parent"}, "parent": {"demo-pkg"}},
        **search_kwargs,
    )

    assert resolver.uncached_searches == 1


def test_resolver_requirement_ordering_does_not_probe_package_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)

    def fail_exists(path: Path) -> bool:
        raise AssertionError(f"probed package requirement as a path: {path}")

    monkeypatch.setattr(Path, "exists", fail_exists)

    _, chosen = resolver.choose_requirement(
        PendingAgenda([parse_requirement("wide>=1"), parse_requirement("narrow==1")]),
        {},
    )

    assert chosen.canonical_name == "wide"


def test_resolver_caches_candidate_counts_per_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    calls = 0
    matching_versions = resolver.provider.matching_versions

    def counted_matching_versions(
        requirement: Requirement,
        *,
        allow_prereleases: bool,
    ) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        return matching_versions(requirement, allow_prereleases=allow_prereleases)

    monkeypatch.setattr(
        resolver.provider,
        "matching_versions",
        counted_matching_versions,
    )
    pending = PendingAgenda(
        [parse_requirement("first>=1"), parse_requirement("second>=1")],
    )

    resolver.choose_requirement(pending, {})
    resolver.choose_requirement(pending, {})

    assert calls == 2


def test_resolver_prefers_last_successful_candidate_seed() -> None:
    resolver = ResolutionEngine(no_index=True)
    resolver.resolution_seed["demo"] = ("2", "https://example.test/demo-2.whl")
    first = WheelCandidate(
        name="demo",
        version=Version("1"),
        path=Path("demo-1.whl"),
        dependencies=(),
        source_url="https://example.test/demo-1.whl",
    )
    second = WheelCandidate(
        name="demo",
        version=Version("2"),
        path=Path("demo-2.whl"),
        dependencies=(),
        source_url="https://example.test/demo-2.whl",
    )

    preferred = CandidateStream(iter((first, second))).prefer(
        lambda candidate: resolver.candidate_matches_seed(
            candidate,
            resolver.resolution_seed["demo"],
        ),
    )

    assert preferred[0] is second


def test_resolver_retains_successful_candidate_seed(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo", "demo", "1.0")
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )

    resolver.resolve(["demo"])

    assert resolver.resolution_seed["demo"][0] == "1.0"
    assert resolver.resolution_seed["demo"][1].startswith("file:")


def test_resolver_interns_canonical_package_ids() -> None:
    resolver = ResolutionEngine(no_index=True)

    assert resolver.package_id_internal("Demo_Pkg") == resolver.package_id_internal(
        "demo-pkg",
    )
    assert resolver.package_names_internal == ["demo-pkg"]


def test_resolver_uses_conflict_activity_to_break_domain_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    first = parse_requirement("first>=1")
    active = parse_requirement("active>=1")
    monkeypatch.setattr(resolver, "decision_candidate_count", lambda item: 2)
    resolver.bump_conflict_activity("active")

    _, chosen = resolver.choose_requirement(PendingAgenda([first, active]), {})
    assert chosen is active


def test_resolver_conflict_activity_is_monotonic() -> None:
    resolver = ResolutionEngine(no_index=True)
    resolver.bump_conflict_activity("active")
    resolver.bump_conflict_activity("active")

    assert resolver.conflict_activity[resolver.package_id_internal("active")] == 2


def test_resolver_compacts_learned_incompatibilities() -> None:
    resolver = ResolutionEngine(no_index=True)
    resolver.learned_clause_limit = 1
    first = LearnedIncompatibility(
        frozenset(
            (
                (resolver.package_id_internal("first"), 1, frozenset()),
                (resolver.package_id_internal("fourth"), 1, frozenset()),
                (resolver.package_id_internal("fifth"), 1, frozenset()),
            ),
        ),
        (resolver.package_id_internal("first"),) * 2,
    )
    second = LearnedIncompatibility(
        frozenset(
            (
                (resolver.package_id_internal("second"), 1, frozenset()),
                (resolver.package_id_internal("third"), 1, frozenset()),
                (resolver.package_id_internal("sixth"), 1, frozenset()),
            ),
        ),
        (
            resolver.package_id_internal("second"),
            resolver.package_id_internal("third"),
        ),
    )
    resolver.record_learned_incompatibility(first)
    resolver.record_learned_incompatibility(second)

    assert len(resolver.learned_incompatibilities) == 1


def test_resolver_ranks_each_pending_package_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    requirements = [
        parse_requirement("demo>=1"),
        parse_requirement("demo<3"),
        parse_requirement("other"),
    ]
    ranked: list[str] = []

    def candidate_count(requirement: Requirement) -> int:
        ranked.append(requirement.canonical_name)
        return 1

    monkeypatch.setattr(resolver, "decision_candidate_count", candidate_count)

    _, chosen = resolver.choose_requirement(PendingAgenda(requirements), {})
    assert chosen is requirements[0]
    assert ranked == ["demo", "other"]


def test_resolver_reuses_version_masks_for_reversible_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    calls = 0

    def available_versions(
        requirement_internal: Requirement,
    ) -> tuple[CandidateSummary, ...]:
        nonlocal calls
        calls += 1
        return tuple(
            CandidateSummary(Version(str(version)), False, None)
            for version in (1, 2, 3)
        )

    monkeypatch.setattr(resolver.provider, "available_versions", available_versions)
    broad = parse_requirement("demo>=1,<4")
    exclusion = parse_requirement("demo!=2")
    narrowed = parse_requirement("demo<2")

    original = resolver.requirements_version_mask((broad, exclusion))
    assert original is not None
    assert original.bit_count() == 2
    assert resolver.requirements_version_mask((broad, exclusion, narrowed)) == 1
    assert resolver.requirements_version_mask((broad, exclusion)) == original
    assert calls == 1


def test_resolver_restores_domain_mask_after_dependency_backtrack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolutionEngine(no_index=True)
    monkeypatch.setattr(
        resolver.provider,
        "available_versions",
        lambda requirement: tuple(
            CandidateSummary(Version(str(version)), False, None)
            for version in (1, 2, 3)
        ),
    )
    domain = PackageDomain(roots=[parse_requirement("demo>=1,<4")])

    broad_mask = resolver.domain_version_mask(domain)
    domain.set_incoming("parent", (parse_requirement("demo<3"),))
    narrowed_mask = resolver.domain_version_mask(domain)
    domain.remove_incoming("parent")

    assert broad_mask is not None
    assert broad_mask.bit_count() == 3
    assert narrowed_mask is not None
    assert narrowed_mask.bit_count() == 2
    assert resolver.domain_version_mask(domain) == broad_mask


def test_resolver_reuses_watched_incompatibility_for_exact_assignments() -> None:
    resolver = ResolutionEngine(no_index=True)
    parent = WheelCandidate(
        name="parent",
        version=Version("1"),
        path=Path("parent-1-py3-none-any.whl"),
        dependencies=(),
    )
    child = WheelCandidate(
        name="child",
        version=Version("1"),
        path=Path("child-1-py3-none-any.whl"),
        dependencies=(),
    )
    other_parent = WheelCandidate(
        name="parent",
        version=Version("2"),
        path=Path("parent-2-py3-none-any.whl"),
        dependencies=(),
    )
    other_source = WheelCandidate(
        name="parent",
        version=Version("1"),
        path=Path("other-index/parent-1-py3-none-any.whl"),
        dependencies=(),
    )
    resolver.learn_watched_incompatibility(
        child,
        frozenset(),
        parse_requirement("shared>=2"),
        PackageDomain(incoming={"parent": (parse_requirement("shared<2"),)}),
        {"parent": parent},
        {},
    )

    assert resolver.violates_watched_incompatibility(
        child,
        frozenset(),
        {"parent": parent},
        {},
    )
    assert not resolver.violates_watched_incompatibility(
        child,
        frozenset(),
        {"parent": other_parent},
        {},
    )
    assert not resolver.violates_watched_incompatibility(
        child,
        frozenset(),
        {"parent": other_source},
        {},
    )


def test_resolver_minimizes_watched_incompatibility_sources() -> None:
    resolver = ResolutionEngine(no_index=True)
    parent = WheelCandidate(
        name="parent",
        version=Version("1"),
        path=Path("parent-1-py3-none-any.whl"),
        dependencies=(),
    )
    irrelevant = WheelCandidate(
        name="irrelevant",
        version=Version("1"),
        path=Path("irrelevant-1-py3-none-any.whl"),
        dependencies=(),
    )
    child = WheelCandidate(
        name="child",
        version=Version("1"),
        path=Path("child-1-py3-none-any.whl"),
        dependencies=(),
    )
    resolver.learn_watched_incompatibility(
        child,
        frozenset(),
        parse_requirement("shared>=2"),
        PackageDomain(
            incoming={
                "parent": (parse_requirement("shared<2"),),
                "irrelevant": (parse_requirement("shared==3"),),
            },
        ),
        {"parent": parent, "irrelevant": irrelevant},
        {},
    )

    assert len(resolver.learned_incompatibilities[0].terms) == 2
    assert resolver.violates_watched_incompatibility(
        child,
        frozenset(),
        {"parent": parent},
        {},
    )


def test_resolver_learns_direct_selected_candidate_conflicts() -> None:
    resolver = ResolutionEngine(no_index=True)
    child = WheelCandidate(
        name="child",
        version=Version("1"),
        path=Path("child-1-py3-none-any.whl"),
        dependencies=(parse_requirement("shared>=2"),),
    )
    selected = WheelCandidate(
        name="shared",
        version=Version("1"),
        path=Path("shared-1-py3-none-any.whl"),
        dependencies=(),
    )

    assert (
        resolver.candidate_dependencies_conflict(
            child,
            extras=frozenset(),
            selected={"shared": selected},
            selected_extras={},
        )
        is False
    )
    assert len(resolver.learned_incompatibilities) == 1
    assert {
        level for _, level in resolver.learned_incompatibilities[0].decision_levels
    } == {
        0,
        1,
    }
    assert resolver.violates_watched_incompatibility(
        child,
        frozenset(),
        {"shared": selected},
        {},
    )


def test_resolver_failure_result_carries_second_highest_level() -> None:
    resolver = ResolutionEngine(no_index=True)
    first = (0, 0, frozenset())
    second = (1, 0, frozenset())
    conflict = LearnedIncompatibility(
        frozenset((first, second)),
        (0, 1),
        ((first, 1), (second, 4)),
    )
    resolver.backjump_conflict = conflict

    failure = resolver.should_backjump_after_failure(0, 4)

    assert failure is not None
    assert failure.conflict is conflict
    assert failure.target_level == 1


def test_resolver_minimizes_repeated_exact_conflict_sources() -> None:
    dependency = parse_requirement("shared==8")
    requirements = {
        "first": (parse_requirement("shared==7"),),
        "second": (parse_requirement("shared<8"),),
        "irrelevant": (parse_requirement("shared>=1"),),
    }

    assert ResolutionEngine.minimal_exact_conflict_sources(
        dependency,
        (),
        requirements,
        ["first", "second", "irrelevant"],
    ) == ["second"]


def test_resolver_skips_memoization_for_linear_search_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    for index in range(4):
        requires = [] if index == 3 else [f"chain-{index + 1}==1.0"]
        make_wheel(
            wheelhouse,
            f"chain-{index}",
            f"chain_{index}",
            "1.0",
            requires=requires,
        )
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    search_state_key = resolver.search_state_key_internal
    calls = 0

    def counting_search_state_key(
        *args: object,
        **kwargs: object,
    ) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        return search_state_key(*args, **kwargs)

    monkeypatch.setattr(
        resolver,
        "search_state_key_internal",
        counting_search_state_key,
    )

    def fail_candidate_count(requirement: Requirement) -> int:
        raise AssertionError(f"counted sole unresolved requirement: {requirement}")

    monkeypatch.setattr(resolver, "candidate_count_internal", fail_candidate_count)

    plan = resolver.resolve(["chain-0"])

    assert len(plan.candidates) == 4
    assert calls == 0


def test_search_state_key_does_not_materialize_url_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[wheelhouse.as_posix()],
            no_index=True,
        ),
        ignore_installed=True,
    )
    candidate = next(
        iter(resolver.provider.find_candidates(parse_requirement("demo-pkg"))),
    )

    def fail_materialize(self: LazyWheelCandidate) -> WheelCandidate:
        raise AssertionError("search state key materialized a lazy candidate")

    monkeypatch.setattr(LazyWheelCandidate, "materialize", fail_materialize)

    resolver.search_state_key_internal(
        PendingAgenda(()),
        {"demo-pkg": candidate},
        {},
        {},
        {},
    )


def test_resolver_caches_viable_candidate_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    old = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    new = make_wheel(packages, "demo-pkg", "demo_pkg", "2.0")
    write_simple_project_archive_index(index, "demo-pkg", [old, new])
    provider = CandidateProvider.from_options(index_url=index.as_uri())
    available_versions = provider.available_versions
    calls = 0

    def counting_available_versions(
        requirement: Requirement,
    ) -> tuple[CandidateSummary, ...]:
        nonlocal calls
        calls += 1
        return available_versions(requirement)

    monkeypatch.setattr(provider, "available_versions", counting_available_versions)
    resolver = ResolutionEngine(provider=provider)

    assert resolver.candidate_count_internal(parse_requirement("demo-pkg>=2")) == 1
    assert resolver.candidate_count_internal(parse_requirement("demo-pkg>=2")) == 1
    assert resolver.candidate_count_internal(parse_requirement("demo-pkg<2")) == 1
    assert calls == 2


def test_resolver_caches_candidate_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    old = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    new = make_wheel(packages, "demo-pkg", "demo_pkg", "2.0")
    write_simple_project_archive_index(index, "demo-pkg", [old, new])
    provider = CandidateProvider.from_options(index_url=index.as_uri())
    find_candidates = provider.find_candidates
    calls = 0

    def counting_find_candidates(
        requirement: Requirement,
    ) -> CandidateStream:
        nonlocal calls
        calls += 1
        return find_candidates(requirement)

    monkeypatch.setattr(provider, "find_candidates", counting_find_candidates)
    resolver = ResolutionEngine(provider=provider)
    requirement = parse_requirement("demo-pkg")

    first = resolver.find_candidates_internal(requirement)
    second = resolver.find_candidates_internal(requirement)

    assert [candidate.version for candidate in first] == [
        Version("2.0"),
        Version("1.0"),
    ]
    assert list(second) == list(first)
    assert calls == 1


def test_resolver_defers_local_wheel_hashing_until_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    selected_wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "2.0")

    finalize_hashes = file_hashes
    hashed_paths: list[str] = []

    def counting_hashes(path: str) -> dict[str, str]:
        hashed_paths.append(path)
        return finalize_hashes(path)

    monkeypatch.setattr("cpip.resolution.engine.output.file_hashes", counting_hashes)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [
        os.fspath(selected_wheel),
    ]
    assert hashed_paths == [os.fspath(selected_wheel)]
    assert plan.candidates[0].source_hashes == file_hashes(selected_wheel)


def test_resolver_can_skip_source_hashing_for_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    selected_wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    def fail_hashing(path: Path) -> dict[str, str]:
        raise AssertionError(f"hashed install candidate: {path}")

    monkeypatch.setattr("cpip.resolution.engine.output.file_hashes", fail_hashing)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(
        provider=provider,
        ignore_installed=True,
        compute_source_hashes=False,
    ).resolve(["demo-pkg"])

    assert [candidate.path for candidate in plan.candidates] == [
        os.fspath(selected_wheel),
    ]


def test_resolver_reuses_cataloged_wheel_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    def fail_filename_parse(path: object) -> None:
        raise AssertionError(f"reparsed cataloged wheel filename: {path}")

    monkeypatch.setattr(
        "cpip.core.wheel.parse_wheel_filename",
        fail_filename_parse,
    )
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [os.fspath(wheel)]


def test_resolver_reuses_cataloged_wheel_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    requirement = parse_requirement("demo-pkg")
    provider.available_versions(requirement)

    def fail_version_parse(self: Version, value: str) -> None:
        raise AssertionError(f"reparsed cataloged wheel version: {value}")

    monkeypatch.setattr(Version, "__init__", fail_version_parse)

    candidates = list(provider.find_candidates(requirement))

    assert [candidate.path for candidate in candidates] == [os.fspath(wheel)]


def test_resolver_reuses_link_vcs_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    def fail_vcs_parse(url: str) -> None:
        raise AssertionError(f"reparsed classified wheel URL for VCS: {url}")

    monkeypatch.setattr("cpip.index.vcs.vcs_scheme", fail_vcs_parse)
    monkeypatch.setattr("cpip.index.artifacts.vcs_scheme", fail_vcs_parse)
    monkeypatch.setattr(
        "cpip.index.candidate_materialization.vcs_scheme",
        fail_vcs_parse,
    )
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [os.fspath(wheel)]


def test_resolver_reuses_link_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    def fail_url_parse(url: str) -> None:
        raise AssertionError(f"reparsed classified local URL: {url}")

    monkeypatch.setattr(
        "cpip.index.artifacts.ArtifactLocator.local_path",
        fail_url_parse,
    )
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [os.fspath(wheel)]


def test_resolver_reuses_validated_wheel_dist_info_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    def fail_namelist(archive: zipfile.ZipFile) -> list[str]:
        raise AssertionError(f"allocated archive name list: {archive.filename}")

    monkeypatch.setattr(zipfile.ZipFile, "namelist", fail_namelist)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [os.fspath(wheel)]


def test_resolver_reads_only_required_core_metadata_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(
        wheelhouse,
        "demo-pkg",
        "demo_pkg",
        "1.0",
        requires=["dependency>=1"],
    )
    make_wheel(wheelhouse, "dependency", "dependency", "1.0")

    def fail_parser() -> None:
        raise AssertionError("constructed a general-purpose email parser")

    monkeypatch.setattr("cpip.core.wheel.Parser", fail_parser)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [
        candidate.path
        for candidate in plan.candidates
        if candidate.canonical_name == "demo-pkg"
    ] == [os.fspath(wheel)]


def test_resolver_ignores_metadata_body_headers(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    with zipfile.ZipFile(wheel, "a") as archive:
        metadata_path = "demo_pkg-1.0.dist-info/METADATA"
        metadata = archive.read(metadata_path).decode()
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(
                metadata_path,
                metadata
                + "\nDescription body\nRequires-Dist: nonexistent-package\n" * 1000,
            )
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [os.fspath(wheel)]


def test_resolver_ignores_unneeded_core_metadata_headers(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    with zipfile.ZipFile(wheel, "a") as archive:
        metadata_path = "demo_pkg-1.0.dist-info/METADATA"
        metadata = archive.read(metadata_path).decode()
        extra_headers = "".join(
            f"Project-URL: Documentation {index}, https://example.invalid/{index}\n"
            for index in range(1000)
        )
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(metadata_path, metadata + extra_headers)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(
        ["demo-pkg"],
    )

    assert [candidate.path for candidate in plan.candidates] == [os.fspath(wheel)]


def test_resolver_propagates_contradictory_exact_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    for index in range(1, 5):
        version = f"{index}.0"
        a_requires = [f"B=={version}"]
        if index > 1:
            a_requires.append(f"C=={index - 1}.0")
        make_wheel(
            wheelhouse,
            "A",
            "a",
            version,
            requires=a_requires,
        )
        make_wheel(
            wheelhouse,
            "B",
            "b",
            version,
            requires=[f"C=={version}"],
        )
        make_wheel(wheelhouse, "C", "c", version)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    find_candidates = provider.find_candidates
    c_searches = 0

    def counting_find_candidates(requirement: Requirement) -> CandidateStream:
        nonlocal c_searches
        if requirement.canonical_name == "c":
            c_searches += 1
        return find_candidates(requirement)

    monkeypatch.setattr(provider, "find_candidates", counting_find_candidates)

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(["A"])

    assert {candidate.canonical_name for candidate in plan.candidates} == {
        "a",
        "b",
        "c",
    }
    assert c_searches == 1


def test_resolver_propagates_disjoint_dependency_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    for index in range(1, 5):
        version = f"{index}.0"
        a_requires = [f"B>={index},<{index + 1}"]
        if index > 1:
            a_requires.append(f"C>={index - 1},<{index}")
        make_wheel(
            wheelhouse,
            "A",
            "a",
            version,
            requires=a_requires,
        )
        make_wheel(
            wheelhouse,
            "B",
            "b",
            version,
            requires=[f"C>={index},<{index + 1}"],
        )
        make_wheel(wheelhouse, "C", "c", version)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    find_candidates = provider.find_candidates
    c_searches = 0

    def counting_find_candidates(requirement: Requirement) -> CandidateStream:
        nonlocal c_searches
        if requirement.canonical_name == "c":
            c_searches += 1
        return find_candidates(requirement)

    monkeypatch.setattr(provider, "find_candidates", counting_find_candidates)

    plan = ResolutionEngine(provider=provider, ignore_installed=True).resolve(["A"])

    assert {candidate.canonical_name for candidate in plan.candidates} == {
        "a",
        "b",
        "c",
    }
    assert c_searches == 1


def test_resolver_propagates_consumed_root_constraints(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    for index in range(1, 5):
        version = f"{index}.0"
        make_wheel(wheelhouse, "shared", "shared", version)
        make_wheel(
            wheelhouse,
            "child",
            "child",
            version,
            requires=[f"shared=={version}"],
        )
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )

    plan = resolver.resolve(["shared<4", "child"])

    selected = {
        candidate.canonical_name: str(candidate.version)
        for candidate in plan.candidates
    }
    assert selected == {"child": "3.0", "shared": "3.0"}
    assert resolver.backtrack_count == 1


def test_resolver_propagates_root_incompatibilities(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "shared", "1.0")
    make_wheel(
        wheelhouse,
        "child",
        "child",
        "1.0",
        requires=["shared>=2"],
    )
    for index in range(1, 6):
        make_wheel(
            wheelhouse,
            "parent",
            "parent",
            f"{index}.0",
            requires=["child"],
        )
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )

    with pytest.raises(ResolutionError):
        resolver.resolve(["shared<2", "parent"])

    assert len(resolver.root_incompatibilities) == 5
    assert len(resolver.root_unsatisfiable_domains) == 1


def test_root_incompatibility_does_not_escape_requirement_domain(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "shared", "1.0")
    make_wheel(
        wheelhouse,
        "child",
        "child",
        "1.0",
        requires=["shared>=2"],
    )
    make_wheel(
        wheelhouse,
        "child",
        "child",
        "2.0",
        requires=["shared<2"],
    )
    make_wheel(
        wheelhouse,
        "parent",
        "parent",
        "2.0",
        requires=["child<2"],
    )
    make_wheel(
        wheelhouse,
        "parent",
        "parent",
        "1.0",
        requires=["child>=2"],
    )
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )

    plan = resolver.resolve(["shared<2", "parent"])

    selected = {
        candidate.canonical_name: str(candidate.version)
        for candidate in plan.candidates
    }
    assert selected == {"child": "2.0", "parent": "1.0", "shared": "1.0"}


def test_resolver_prefers_stable_release_by_default(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    stable = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    prerelease = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0b1")
    write_simple_project_archive_index(index, "demo-pkg", [stable, prerelease])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = ResolutionEngine(provider=provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["1.0"]


def test_resolver_allows_prerelease_with_pre_flag(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    stable = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    prerelease = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0b1")
    write_simple_project_archive_index(index, "demo-pkg", [stable, prerelease])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = ResolutionEngine(provider=provider, allow_prereleases=True).resolve(
        ["demo-pkg"],
    )

    assert [str(candidate.version) for candidate in plan.candidates] == ["2.0b1"]


def test_resolver_allows_prerelease_when_specifier_mentions_one(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    stable = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    prerelease = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0b1")
    write_simple_project_archive_index(index, "demo-pkg", [stable, prerelease])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = ResolutionEngine(provider=provider).resolve(["demo-pkg>=0.0.dev0"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["2.0b1"]


def test_resolver_only_binary_rejects_source_only_project(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [sdist])
    format_control = FormatControl()
    format_control.apply("only-binary", ":all:")

    provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        format_control=format_control,
    )

    with pytest.raises(DistributionNotFound, match="No matching distribution found"):
        ResolutionEngine(provider=provider).resolve(["demo-pkg"])


def test_resolver_no_binary_allows_source_only_selection(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    write_simple_project_archive_index(index, "demo-pkg", [wheel, sdist])
    format_control = FormatControl()
    format_control.apply("no-binary", ":all:")

    provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        format_control=format_control,
    )
    plan = ResolutionEngine(provider=provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["2.0"]


def test_resolver_prefer_binary_prefers_older_wheel_over_newer_source(
    tmp_path: Path,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    write_simple_project_archive_index(index, "demo-pkg", [sdist, wheel])

    default_provider = CandidateProvider.from_options(index_url=index.as_uri())
    preferred_provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        prefer_binary=True,
    )

    default_plan = ResolutionEngine(provider=default_provider).resolve(["demo-pkg"])
    preferred_plan = ResolutionEngine(provider=preferred_provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in default_plan.candidates] == ["2.0"]
    assert preferred_plan.candidates[0].path == os.fspath(wheel)


def test_resolver_applies_version_constraints(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    old = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    new = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    write_simple_project_archive_index(index, "demo-pkg", [old, new])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = ResolutionEngine(provider=provider, constraints=["demo-pkg<2"]).resolve(
        ["demo-pkg"],
    )

    assert [str(candidate.version) for candidate in plan.candidates] == ["1.0"]


def test_resolver_indexes_constraints_by_canonical_name() -> None:
    resolver = ResolutionEngine(
        no_index=True,
        constraints=["Demo_Pkg>=1", "demo-pkg<3", "other-pkg==2"],
    )

    constrained = resolver.apply_constraints(parse_requirement("DEMO.pkg"))

    assert set(resolver.constraints_by_name) == {"demo-pkg", "other-pkg"}
    assert constrained.is_satisfied_by("1")
    assert constrained.is_satisfied_by("2")
    assert not constrained.is_satisfied_by("3")


def test_resolver_maintains_reverse_dependency_index(tmp_path: Path) -> None:
    resolver = ResolutionEngine(no_index=True)
    candidate = WheelCandidate(
        name="parent",
        version=Version("1"),
        path=tmp_path / "parent.whl",
        dependencies=(
            parse_requirement("child>=1"),
            parse_requirement("child<3"),
            parse_requirement("other"),
        ),
    )
    sibling = WheelCandidate(
        name="sibling",
        version=Version("1"),
        path=tmp_path / "sibling.whl",
        dependencies=(parse_requirement("child>=2"),),
    )

    resolver.add_candidate_dependencies("parent", candidate)
    resolver.add_candidate_dependencies("sibling", sibling)
    initial_domain = resolver.domains_internal["child"].requirements()
    active = resolver.active_requirements_for(
        "child",
        parse_requirement("child!=2"),
        [parse_requirement("deferred")],
    )

    assert [item.raw for item in active] == [
        "child!=2",
        "child>=1",
        "child<3",
        "child>=2",
        "deferred",
    ]

    resolver.remove_candidate_dependencies("parent", candidate)
    assert tuple(resolver.incoming_requirements["child"]) == ("sibling",)
    assert resolver.domains_internal["child"].requirements() != initial_domain
    assert [item.raw for item in resolver.domains_internal["child"].requirements()] == [
        "child>=2",
    ]

    resolver.remove_candidate_dependencies("sibling", sibling)
    assert resolver.incoming_requirements == {}
    assert "child" not in resolver.domains_internal


def test_resolver_only_materializes_top_matching_wheels(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    newest = make_wheel(packages, "demo-pkg", "demo_pkg", "3.0")
    older = make_wheel(packages, "demo-pkg", "demo_pkg", "2.0")
    prerelease = make_wheel(packages, "demo-pkg", "demo_pkg", "4.0b1")
    write_simple_project_archive_index(index, "demo-pkg", [prerelease, newest, older])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = ResolutionEngine(provider=provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["3.0"]


def test_resolver_applies_direct_url_constraint(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    direct = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(no_index=True)
    plan = ResolutionEngine(
        provider=provider,
        constraints=[f"demo-pkg @ {direct.as_uri()}"],
    ).resolve(["demo-pkg"])

    assert len(plan.candidates) == 1
    assert plan.candidates[0].path == os.fspath(direct)


def test_resolver_prefers_direct_requirement_over_index_candidates(
    tmp_path: Path,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    indexed = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    direct = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [indexed])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = ResolutionEngine(provider=provider).resolve(
        ["demo-pkg", f"demo-pkg @ {direct.as_uri()}"],
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].path == os.fspath(direct)


def test_resolver_accepts_requirement_set_input(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [sdist])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    reqset = RequirementSet()
    reqset.add_named_requirement(install_req_from_line("demo-pkg"))

    plan = ResolutionEngine(provider=provider).resolve(reqset)

    assert [str(candidate.version) for candidate in plan.candidates] == ["1.0"]


def test_resolver_can_return_requirement_set(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [wheel])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    resolved = ResolutionEngine(provider=provider).resolve_requirement_set(["demo-pkg"])

    assert resolved.has_requirement("demo-pkg")
    assert len(resolved.all_requirements) == 1
    req = resolved.get_requirement("demo-pkg")
    assert req.req is not None
    assert str(req.req) == "demo-pkg==1.0"
    assert req.link is not None
    assert req.link.filename == wheel.name


def test_resolved_requirement_set_includes_download_info_for_local_wheel(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")

    resolved = ResolutionEngine().resolve_requirement_set([wheel.as_posix()])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == file_hashes(wheel)


def test_resolved_requirement_set_includes_download_info_for_local_dir(
    tmp_path: Path,
) -> None:
    source = tmp_path / "demo-pkg"
    package = source / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    source.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ],
        ),
        encoding="utf-8",
    )

    resolved = ResolutionEngine().resolve_requirement_set([source.as_posix()])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.dir_info is not None
    assert req.download_info.dir_info.editable is False


def test_resolved_requirement_set_includes_download_info_for_find_links(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )
    resolved = ResolutionEngine(provider=provider).resolve_requirement_set(["demo-pkg"])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == file_hashes(wheel)


def test_resolved_requirement_set_preserves_direct_archive_url(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()

    resolved = ResolutionEngine().resolve_requirement_set([f"demo-pkg @ {archive_url}"])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url == archive_url
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == file_hashes(archive)


def test_resolved_requirement_set_marks_local_editable_dir(
    tmp_path: Path,
) -> None:
    source = tmp_path / "editable-demo"
    package = source / "editable_demo"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text(
        "NAME = 'editable-demo'\n",
        encoding="utf-8",
    )
    source.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "editable-demo"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ],
        ),
        encoding="utf-8",
    )

    resolved = ResolutionEngine().resolve_requirement_set(
        [install_req_from_editable(source.as_posix())],
    )

    req = resolved.all_requirements[0]
    assert req.editable is True
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.dir_info is not None
    assert req.download_info.dir_info.editable is True


def test_resolved_requirement_set_includes_vcs_info_for_git_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    repo.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ],
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    resolved = ResolutionEngine().resolve_requirement_set(
        [f"demo-pkg @ git+{repo.as_uri()}"],
    )

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.vcs_info is not None
    assert req.download_info.vcs_info.vcs == "git"
    assert req.download_info.url == repo.as_uri()


def test_resolved_requirement_set_marks_cached_wheel_for_direct_archive(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()
    cache_dir = tmp_path / "cache"
    entry_dir = wheel_cache_path(cache_dir, archive_url)
    entry_dir_path = Path(entry_dir)
    entry_dir_path.mkdir(parents=True)
    cached_wheel = make_wheel(entry_dir_path, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(no_index=True, wheel_cache_dir=cache_dir)
    resolved = ResolutionEngine(provider=provider).resolve_requirement_set(
        [f"demo-pkg @ {archive_url}"],
    )

    req = resolved.all_requirements[0]
    assert req.is_wheel_from_cache is True
    assert req.cached_wheel_source_link is not None
    assert req.cached_wheel_source_link.url == archive_url
    assert req.download_info is not None
    assert req.download_info.url == archive_url
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == {}
    assert req.link is not None
    assert req.link.filename == cached_wheel.name


def test_resolved_requirement_set_reads_origin_hashes_from_cache(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()
    cache_dir = tmp_path / "cache"
    entry_dir = wheel_cache_path(cache_dir, archive_url)
    entry_dir_path = Path(entry_dir)
    entry_dir_path.mkdir(parents=True)
    make_wheel(entry_dir_path, "demo-pkg", "demo_pkg", "1.0")
    entry_dir_path.joinpath("origin.json").write_text(
        f'{{"url": "{archive_url}", '
        '"archive_info": {"hashes": {"sha256": "abc123"}}}',
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True, wheel_cache_dir=cache_dir)
    resolved = ResolutionEngine(provider=provider).resolve_requirement_set(
        [f"demo-pkg @ {archive_url}"],
    )

    req = resolved.all_requirements[0]
    assert req.is_wheel_from_cache is True
    assert req.download_info is not None
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == {"sha256": "abc123"}


def test_invalid_cache_origin_file_is_ignored(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()
    cache_dir = tmp_path / "cache"
    entry_dir = wheel_cache_path(cache_dir, archive_url)
    entry_dir_path = Path(entry_dir)
    entry_dir_path.mkdir(parents=True)
    make_wheel(entry_dir_path, "demo-pkg", "demo_pkg", "1.0")
    entry_dir_path.joinpath("origin.json").write_text("{", encoding="utf-8")

    provider = CandidateProvider.from_options(no_index=True, wheel_cache_dir=cache_dir)
    resolved = ResolutionEngine(provider=provider).resolve_requirement_set(
        [f"demo-pkg @ {archive_url}"],
    )

    req = resolved.all_requirements[0]
    assert req.is_wheel_from_cache is True
    assert req.download_info is not None
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == {}
    assert any(
        "Ignoring invalid cache entry origin file" in message
        for message in caplog.messages
    )


def test_require_hashes_rejects_missing_hash_for_pinned_requirement(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )
    reqset = RequirementSet()
    reqset.add_named_requirement(install_req_from_line("demo-pkg==1.0"))

    with pytest.raises(
        HashMissing,
        match="Missing hash for:\n    demo-pkg==1.0 --hash=sha256:",
    ) as exc_info:
        ResolutionEngine(provider=provider, require_hashes=True).resolve(reqset)
    assert file_hashes(wheel)["sha256"] in str(exc_info.value)


def test_require_hashes_rejects_vcs_requirements(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    repo.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ],
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    req = install_req_from_line(f"demo-pkg @ git+{repo.as_uri()}")
    req.hash_options = {"sha256": ["badbad"]}

    with pytest.raises(VcsHashUnsupported, match="hash version control repositories"):
        ResolutionEngine(require_hashes=True).resolve([req])


def test_require_hashes_rejects_directory_urls(tmp_path: Path) -> None:
    source = tmp_path / "demo-pkg"
    package = source / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    source.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ],
        ),
        encoding="utf-8",
    )

    req = install_req_from_line(source.as_posix())

    with pytest.raises(DirectoryUrlHashUnsupported, match="they point to directories"):
        ResolutionEngine(require_hashes=True).resolve([req])


def test_require_hashes_rejects_hash_mismatch(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    req = install_req_from_line(f"demo-pkg @ {archive.as_uri()}")
    req.hash_options = {"sha256": ["badbad"]}

    with pytest.raises(HashMismatch, match="Expected sha256 badbad"):
        ResolutionEngine(require_hashes=True).resolve([req])


def test_require_hashes_lazily_selects_matching_artifact(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [wheel, archive])
    req = install_req_from_line("demo-pkg==1.0")
    req.hash_options = {"sha256": [file_hashes(archive)["sha256"]]}
    provider = CandidateProvider.from_options(index_url=index.as_uri())

    plan = ResolutionEngine(provider=provider, require_hashes=True).resolve([req])

    assert len(plan.candidates) == 1
    assert plan.candidates[0].source_kind == "sdist"


def test_require_hashes_rejects_unpinned_transitive_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(
        wheelhouse,
        "toporequires",
        "toporequires",
        "0.0.1",
    )
    make_wheel(
        wheelhouse,
        "toporequires2",
        "toporequires2",
        "0.0.1",
        requires=["toporequires"],
    )
    req = install_req_from_line("toporequires2==0.0.1")
    req.hash_options = {
        "sha256": [
            file_hashes(wheelhouse / "toporequires2-0.0.1-py3-none-any.whl")["sha256"],
        ],
    }
    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )

    with pytest.raises(HashUnpinned, match="Unpinned requirement:\n    toporequires"):
        ResolutionEngine(provider=provider, require_hashes=True).resolve([req])


def test_require_hashes_allows_hashed_transitive_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    dep_wheel = make_wheel(
        wheelhouse,
        "toporequires",
        "toporequires",
        "0.0.1",
    )
    parent_wheel = make_wheel(
        wheelhouse,
        "toporequires2",
        "toporequires2",
        "0.0.1",
        requires=["toporequires==0.0.1"],
    )
    dep_req = install_req_from_line("toporequires==0.0.1")
    dep_req.hash_options = {"sha256": [file_hashes(dep_wheel)["sha256"]]}
    parent_req = install_req_from_line("toporequires2==0.0.1")
    parent_req.hash_options = {"sha256": [file_hashes(parent_wheel)["sha256"]]}
    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )

    plan = ResolutionEngine(provider=provider, require_hashes=True).resolve(
        [parent_req, dep_req],
    )

    assert [candidate.name for candidate in plan.candidates] == [
        "toporequires",
        "toporequires2",
    ]


def write_simple_project_archive_index(
    index: Path,
    project: str,
    archives: list[Path],
) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    links = []
    for archive in archives:
        href = os.path.relpath(archive, project_dir).replace(os.sep, "/")
        links.append(f'<a href="{href}">{archive.name}</a>')
    (project_dir / "index.html").write_text("\n".join(links) + "\n", encoding="utf-8")
