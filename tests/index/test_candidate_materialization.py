from collections.abc import Iterator
import os
from pathlib import Path
from typing import Any

import pytest
from cpip.core.packaging import Version
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import (
    CandidateMaterializer,
    CandidateStream,
    candidate_metadata_fingerprint,
)
from cpip.index.links import Link
from cpip.index.source_models import CandidateRecord


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


def test_source_hashes_reuse_the_local_artifact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0.tar.gz"
    artifact.write_bytes(b"artifact")
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_path(artifact, source_url=None),
    )
    materializer = CandidateMaterializer()
    original_open = open
    reads = 0

    def counting_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        if args and os.fspath(args[0]) == os.fspath(artifact):
            reads += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)

    assert materializer.source_hashes_for(record) == materializer.source_hashes_for(
        record
    )
    assert reads == 1


def test_file_url_identity_stat_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0-py3-none-any.whl"
    artifact.write_bytes(b"artifact")
    original_stat = os.stat
    stats = 0

    def counting_stat(*args: Any, **kwargs: Any) -> Any:
        nonlocal stats
        stats += 1
        return original_stat(*args, **kwargs)

    monkeypatch.setattr("cpip.index.links.os.stat", counting_stat)
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(artifact.as_uri(), source_url=None),
    )

    assert candidate_metadata_fingerprint(record).startswith("stat:")
    assert stats == 1
