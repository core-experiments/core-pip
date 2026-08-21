"""Link.from_local_file must produce exactly what Link.from_path produces."""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest
from cpip.core.urls import path_to_url
from cpip.index.links import Link
from cpip.index.source_locations import FindLinksSource

NAMES = (
    "pkg-1.0-py3-none-any.whl",
    "pkg-1.0+local-py3-none-any.whl",
    "pkg-1.0.tar.gz",
    "index.html",
    "README",
    "with space.whl",
    "percent%20literal.whl",
    "hash#frag.whl",
    "query?q=1.whl",
    "amp&egg=x.whl",
    "equals=egg=x.whl",
    "semi;colon.whl",
    "colon:name.whl",
    "tilde~name.whl",
    "unicode-éè.whl",
    "日本語.whl",
    ".hidden",
    "..dots",
    "trailing.",
    "brackets[1].whl",
    "quote'd\".whl",
    "back\\slash.whl",
)

DIRECTORY_NAMES = ("plain", "with space", "üñí", "pct%41", "q?x", "h#x", "semi;c")

# filename_internal is a lazily-filled cache: from_local_file fills it up
# front with the value the filename property would compute, so it is
# compared through that property instead of the raw slot.
SLOTS = tuple(slot for slot in Link.__slots__ if slot != "filename_internal")


def _fields(link: Link) -> dict[str, object]:
    fields: dict[str, object] = {slot: getattr(link, slot) for slot in SLOTS}
    fields["filename"] = link.filename
    return fields


def _write(directory: Path, names: tuple[str, ...]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"x")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file URL layout")
@pytest.mark.parametrize("directory_name", DIRECTORY_NAMES)
def test_from_local_file_matches_from_path(tmp_path: Path, directory_name: str) -> None:
    directory = tmp_path / directory_name
    _write(directory, NAMES)
    path_text = os.fspath(directory)
    directory_url = path_to_url(path_text)
    directory_path = os.path.abspath(path_text)
    for name in NAMES:
        entry_path = os.path.join(path_text, name)
        expected = Link.from_path(
            entry_path,
            source_url=path_text,
            is_dir=False,
            local_identity="stat:1:2:3:4",
        )
        actual = Link.from_local_file(
            name,
            directory_path=directory_path,
            directory_url=directory_url,
            path_text=entry_path,
            source_url=path_text,
            local_identity="stat:1:2:3:4",
        )
        assert _fields(actual) == _fields(expected), name
        assert actual == expected
        assert hash(actual) == hash(expected)
        assert actual.filename_internal == expected.filename
        assert actual.file_path == expected.file_path
        assert actual.is_file
        assert not actual.is_vcs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file URL layout")
def test_from_local_file_matches_from_path_for_relative_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "rel" / "wheels"
    _write(directory, NAMES[:6])
    monkeypatch.chdir(tmp_path)
    path_text = os.path.join("rel", "wheels")
    directory_url = path_to_url(path_text)
    directory_path = os.path.abspath(path_text)
    for name in NAMES[:6]:
        entry_path = os.path.join(path_text, name)
        expected = Link.from_path(entry_path, source_url=path_text, is_dir=False)
        actual = Link.from_local_file(
            name,
            directory_path=directory_path,
            directory_url=directory_url,
            path_text=entry_path,
            source_url=path_text,
            local_identity=None,
        )
        assert _fields(actual) == _fields(expected), name
        assert actual.file_path == entry_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file URL layout")
def test_from_local_file_matches_from_path_at_filesystem_root() -> None:
    directory_url = path_to_url("/")
    for name in NAMES[:4]:
        expected = Link.from_path(f"/{name}", source_url="/", is_dir=False)
        actual = Link.from_local_file(
            name,
            directory_path="/",
            directory_url=directory_url,
            path_text=f"/{name}",
            source_url="/",
            local_identity=None,
        )
        assert _fields(actual) == _fields(expected), name


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file URL layout")
def test_from_local_file_random_names_match_from_path(tmp_path: Path) -> None:
    rng = random.Random(20260820)
    alphabet = "abcZ09-_.~+ %#?&=;:[]'\"éü日\\"
    names = tuple(
        {
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
            for _ in range(400)
        }
        - {".", ".."}
    )
    names = tuple(name for name in names if "/" not in name)
    path_text = os.fspath(tmp_path)
    directory_url = path_to_url(path_text)
    directory_path = os.path.abspath(path_text)
    for name in names:
        entry_path = os.path.join(path_text, name)
        expected = Link.from_path(entry_path, source_url=path_text, is_dir=False)
        actual = Link.from_local_file(
            name,
            directory_path=directory_path,
            directory_url=directory_url,
            path_text=entry_path,
            source_url=path_text,
            local_identity=None,
        )
        assert _fields(actual) == _fields(expected), name


def test_links_from_local_path_matches_from_path_per_entry(tmp_path: Path) -> None:
    directory = tmp_path / "wheelhouse"
    _write(directory, NAMES)
    path_text = os.fspath(directory)
    source = FindLinksSource((path_text,), set(), None)
    links = source.links_from_local_path(path_text)
    # The scan filters by artifact suffix; compare against the very
    # snapshot it cached rather than re-scanning with different rules.
    snapshot = source.local_snapshots[path_text]
    assert snapshot is not None
    assert snapshot.entries
    expected = [
        Link.from_path(
            item.path,
            source_url=path_text,
            is_dir=False,
            local_identity=item.identity,
        )
        for item in snapshot.entries
    ]
    assert [_fields(link) for link in links] == [_fields(link) for link in expected]
    produced = {link.filename for link in links}
    assert {name for name in NAMES if name.endswith((".whl", ".tar.gz"))} <= produced
    assert "README" not in produced
