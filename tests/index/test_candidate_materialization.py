from collections.abc import Iterator
from pathlib import Path

import pytest
from cpip.core.packaging import Version
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import CandidateStream


def make_candidate(version: str) -> WheelCandidate:
    return WheelCandidate(
        name="demo-pkg",
        version=Version(version),
        path=Path(f"demo_pkg-{version}-py3-none-any.whl"),
        dependencies=(),
    )


def test_candidate_stream_materializes_on_demand_and_replays() -> None:
    produced: list[str] = []

    def generate() -> Iterator[WheelCandidate]:
        for version in ("3", "2", "1"):
            produced.append(version)
            yield make_candidate(version)

    stream = CandidateStream(generate())

    assert produced == []
    assert stream
    assert produced == ["3"]
    assert stream[1].version == Version("2")
    assert produced == ["3", "2"]
    assert [candidate.version for candidate in stream] == [
        Version("3"),
        Version("2"),
        Version("1"),
    ]
    assert produced == ["3", "2", "1"]
    assert list(stream) == list(stream)
    assert produced == ["3", "2", "1"]


def test_candidate_stream_replays_terminal_error() -> None:
    error = RuntimeError("materialization failed")

    def generate() -> Iterator[WheelCandidate]:
        yield make_candidate("2")
        raise error

    stream = CandidateStream(generate())

    assert stream[0].version == Version("2")
    with pytest.raises(RuntimeError, match="materialization failed") as first:
        list(stream)
    with pytest.raises(RuntimeError, match="materialization failed") as second:
        list(stream)
    assert first.value is error
    assert second.value is error


def test_candidate_stream_preference_is_lazy_and_has_fallback() -> None:
    stream = CandidateStream(iter([make_candidate("3"), make_candidate("2")]))

    preferred = stream.prefer(lambda candidate: candidate.version == Version("2"))
    fallback = stream.prefer(lambda candidate: candidate.version == Version("1"))

    assert [candidate.version for candidate in preferred] == [Version("2")]
    assert [candidate.version for candidate in fallback] == [Version("3"), Version("2")]
