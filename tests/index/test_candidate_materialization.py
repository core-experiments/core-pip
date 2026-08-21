import os
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cpip.core.errors import UnsupportedWheel
from cpip.core.packaging import Version, parse_requirement
from cpip.core.wheel import WheelCandidate, parse_wheel
from cpip.index.candidate_materialization import (
    CandidateMaterializer,
    CandidateStream,
    LazyWheelCandidate,
    candidate_metadata_fingerprint,
)
from cpip.index.links import Link
from cpip.index.provider import CandidateProvider
from cpip.index.source_models import CandidateRecord
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.archive import WheelArchive

from ..wheel_helpers import make_wheel


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


def test_lazy_wheel_candidate_loads_release_record_once() -> None:
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://example.invalid/demo-1.0-py3-none-any.whl",
            source_url=None,
        ),
    )
    calls = 0

    def load_record() -> CandidateRecord:
        nonlocal calls
        calls += 1
        return record

    candidate = LazyWheelCandidate(
        None,
        parse_requirement("demo"),
        CandidateMaterializer(),
        record_loader=load_record,
    )

    assert candidate.version == Version("1.0")
    assert candidate.source_url.endswith("demo-1.0-py3-none-any.whl")
    assert calls == 1


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


def resolve_names(wheelhouse: Path, requirements: list[str]) -> list[str]:
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    return [candidate.name for candidate in engine.resolve(requirements).candidates]


def test_resolution_reads_only_the_metadata_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution must not decompress WHEEL for candidates it only inspects.

    Locating the ``.dist-info`` directory comes free with the central
    directory ZipFile already parsed; reading WHEEL on top of METADATA
    doubles the member decompressions per candidate, and resolution has no
    use for the text.

    Candidate materialization opens the archive through ``WheelArchive`` --
    a leaner central-directory reader tried ahead of ``zipfile.ZipFile`` --
    so both read entry points are patched here; only whichever one the
    resolve actually took records anything.
    """
    make_wheel(tmp_path, "demo-pkg", "demo_pkg", "1.0.0")

    members: list[str] = []
    original_zipfile_read = zipfile.ZipFile.read
    original_wheel_archive_read = WheelArchive.read

    def recording_zipfile_read(
        self: zipfile.ZipFile,
        name: Any,
        pwd: Any = None,
    ) -> bytes:
        members.append(name if isinstance(name, str) else name.filename)
        return original_zipfile_read(self, name, pwd)

    def recording_wheel_archive_read(self: WheelArchive, name: str) -> bytes:
        members.append(name)
        return original_wheel_archive_read(self, name)

    monkeypatch.setattr(zipfile.ZipFile, "read", recording_zipfile_read)
    monkeypatch.setattr(WheelArchive, "read", recording_wheel_archive_read)

    assert resolve_names(tmp_path, ["demo-pkg"]) == ["demo-pkg"]

    assert any(member.endswith("/METADATA") for member in members)
    assert not [member for member in members if member.endswith("/WHEEL")]


def test_resolution_defers_the_wheel_version_check_to_install(
    tmp_path: Path,
) -> None:
    """A future ``Wheel-Version`` is the installer's to reject, not resolution.

    Skipping the WHEEL read moves this diagnostic from resolve time to
    install time.  Nothing is installed unchecked -- ``parse_wheel`` still
    refuses the same archive -- so the trade is only where it surfaces.
    """
    wheel = make_wheel(tmp_path, "future-pkg", "future_pkg", "3.0", wheel_version="3.0")

    assert resolve_names(tmp_path, ["future-pkg"]) == ["future-pkg"]

    with zipfile.ZipFile(wheel) as archive, pytest.raises(UnsupportedWheel):
        parse_wheel(archive, "future-pkg")


def test_materialized_candidate_keeps_the_resolver_layout(tmp_path: Path) -> None:
    """The layout built during materialization must reach the returned
    candidate: it is what lets the installer open the wheel again without
    another central-directory scan."""
    from cpip.install.wheel_archive_runtime import RawWheelArchive, open_wheel_archive
    from cpip.resolution import archive as archive_module

    make_wheel(tmp_path, "layout-pkg", "layout_pkg", "1.0")
    provider = CandidateProvider.from_options(find_links=[str(tmp_path)], no_index=True)
    [candidate] = list(provider.find_candidates(parse_requirement("layout-pkg")))
    layout = candidate.wheel_layout
    assert isinstance(layout, tuple)
    dist_info, members, root_is_purelib = layout
    assert dist_info == "layout_pkg-1.0.dist-info"
    assert members
    assert all(len(member) == 7 for member in members)
    assert root_is_purelib is True

    scans = []
    original = archive_module.WheelArchive.read_central_directory

    def counting(self):  # noqa: ANN001, ANN202
        scans.append(1)
        return original(self)

    archive_module.WheelArchive.read_central_directory = counting
    try:
        with open_wheel_archive(candidate.path, candidate) as archive:
            assert isinstance(archive, RawWheelArchive)
            assert f"{dist_info}/METADATA" in archive.namelist()
    finally:
        archive_module.WheelArchive.read_central_directory = original
    assert scans == []
