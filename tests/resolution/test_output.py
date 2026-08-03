from __future__ import annotations

import threading
import time

from cpip.core.packaging import Version, parse_requirement
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import (
    CandidateMaterializer,
    LazyWheelCandidate,
)
from cpip.index.links import Link
from cpip.index.source_models import CandidateRecord
from cpip.install.output import (
    finalize_candidates,
    prepare_install_candidates,
)


def remote_candidate(name: str, version: str = "1.0") -> LazyWheelCandidate:
    requirement = parse_requirement(f"{name}=={version}")
    assert requirement is not None
    record = CandidateRecord(
        name=name,
        version=Version(version),
        link=Link.from_url(
            f"https://example.invalid/{name}-{version}-py3-none-any.whl",
            source_url=None,
        ),
    )
    return LazyWheelCandidate(record, requirement, CandidateMaterializer())


def test_finalize_candidates_runs_remote_wheels_concurrently_in_order() -> None:
    candidates = [remote_candidate(f"demo-{index}") for index in range(3)]
    lock = threading.Lock()
    all_started = threading.Event()
    active = 0
    peak = 0
    indexes = {id(candidate): index for index, candidate in enumerate(candidates)}

    def finalize(candidate: WheelCandidate) -> WheelCandidate:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == len(candidates):
                all_started.set()
        assert all_started.wait(timeout=5)
        # Finish in reverse order to verify ordered result assembly.
        index = indexes[id(candidate)]
        time.sleep((len(candidates) - index) * 0.005)
        with lock:
            active -= 1
        return candidate

    result = finalize_candidates(candidates, finalize)

    assert all(result is candidate for result, candidate in zip(result, candidates))
    assert peak == len(candidates)


def test_finalize_candidates_keeps_source_artifacts_on_calling_thread() -> None:
    requirement = parse_requirement("demo==1.0")
    assert requirement is not None
    source = LazyWheelCandidate(
        CandidateRecord(
            name="demo",
            version=Version("1.0"),
            link=Link.from_url(
                "https://example.invalid/demo-1.0.tar.gz",
                source_url=None,
            ),
        ),
        requirement,
        CandidateMaterializer(),
    )
    caller = threading.current_thread()
    worker: threading.Thread | None = None

    def finalize(candidate: WheelCandidate) -> WheelCandidate:
        nonlocal worker
        worker = threading.current_thread()
        return candidate

    result = finalize_candidates([source], finalize)

    assert len(result) == 1
    assert result[0] is source
    assert worker is caller


def test_prepare_install_candidates_pipelines_completed_downloads(
    tmp_path,
    monkeypatch,
) -> None:
    candidates = [remote_candidate(f"demo-{index}") for index in range(3)]
    archive_started = threading.Event()
    concrete_by_name = {
        candidate.name: WheelCandidate(
            name=candidate.name,
            version=candidate.version,
            path=str(tmp_path / f"{candidate.name}.whl"),
            dependencies=(),
            source_kind="wheel",
        )
        for candidate in candidates
    }

    def materialize(candidate: LazyWheelCandidate) -> WheelCandidate:
        if candidate.name == "demo-0":
            assert archive_started.wait(timeout=5)
        else:
            time.sleep(0.01)
        return concrete_by_name[candidate.name]

    def prepare(candidate: WheelCandidate, cache_dir: str) -> object:
        assert cache_dir == str(tmp_path / "cache")
        archive_started.set()
        return object()

    monkeypatch.setattr(LazyWheelCandidate, "materialize", materialize)
    result = prepare_install_candidates(
        candidates,
        str(tmp_path / "cache"),
        prepare,
    )

    assert [candidate.name for candidate in result] == [
        "demo-0",
        "demo-1",
        "demo-2",
    ]
    assert all(candidate.wheel_layout is not None for candidate in result)


def test_prepare_install_candidates_treats_cache_errors_as_fallback(
    tmp_path,
) -> None:
    candidate = WheelCandidate(
        name="demo",
        version=Version("1.0"),
        path=str(tmp_path / "demo.whl"),
        dependencies=(),
        source_kind="wheel",
    )

    def fail_cache(candidate: WheelCandidate, cache_dir: str) -> object:
        del candidate, cache_dir
        raise OSError("cache unavailable")

    result = prepare_install_candidates(
        [candidate],
        str(tmp_path / "cache"),
        fail_cache,
    )

    assert result == [candidate]
    assert result[0].wheel_layout is None
