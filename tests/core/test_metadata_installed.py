"""Correctness of the installed-distribution metadata fast path.

``InstalledDistribution`` reads Name/Version/Requires-Dist through
``parse_metadata_headers`` instead of ``importlib.metadata``'s full RFC822
email parsing (see ``core/metadata.py``). These tests build real dist-info
directories on disk and assert cpip's fast path agrees exactly with what
``importlib.metadata`` itself would report.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest
from cpip.core.metadata import iter_installed_distributions
from cpip.core.versions import Version


def _write_dist_info(
    site_packages: Path,
    dist_info_name: str,
    metadata_text: str,
    *,
    filename: str = "METADATA",
) -> None:
    dist_info = site_packages / dist_info_name
    dist_info.mkdir(parents=True)
    (dist_info / filename).write_text(metadata_text, encoding="utf-8")


def test_iter_installed_distributions_matches_stdlib(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "widget-1.2.3.dist-info",
        "Metadata-Version: 2.1\n"
        "Name: widget\n"
        "Version: 1.2.3\n"
        "Requires-Dist: gadget>=1.0\n"
        'Requires-Dist: extra-thing==2.0; extra == "extra"\n'
        "Provides-Extra: extra\n",
    )

    [distribution] = iter_installed_distributions(paths=[str(site_packages)])

    stdlib_dist = next(
        iter(importlib.metadata.distributions(path=[str(site_packages)])),
    )

    assert distribution.name == stdlib_dist.metadata.get("Name")
    assert distribution.raw_version == stdlib_dist.version
    assert distribution.canonical_name == "widget"

    assert sorted(distribution._fast_metadata_headers()["requires-dist"]) == sorted(
        stdlib_dist.metadata.get_all("Requires-Dist", []),
    )

    fast_deps = sorted(str(dep) for dep in distribution.dependencies())
    assert fast_deps == ["gadget>=1.0"]

    with_extra = sorted(dep.name for dep in distribution.dependencies(["extra"]))
    assert with_extra == ["extra-thing", "gadget"]


def test_iter_installed_distributions_falls_back_to_pkg_info(tmp_path: Path) -> None:
    """Old-style egg-info installs ship PKG-INFO instead of METADATA."""
    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "legacy-0.1.egg-info",
        "Metadata-Version: 1.0\nName: legacy\nVersion: 0.1\n",
        filename="PKG-INFO",
    )

    [distribution] = iter_installed_distributions(paths=[str(site_packages)])

    assert distribution.name == "legacy"
    assert distribution.raw_version == "0.1"
    assert distribution.dependencies() == []


def test_installed_distribution_dependencies_reuse_parsed_headers(
    tmp_path: Path,
) -> None:
    """dependencies() must not re-read the file iter_installed_distributions
    already parsed."""
    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "widget-1.0.dist-info",
        "Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n",
    )

    [distribution] = iter_installed_distributions(paths=[str(site_packages)])
    assert distribution._fast_headers is not None

    # Corrupt the on-disk file: if dependencies() re-read it, this would blow
    # up or silently disagree with the cached name/version already reported.
    (site_packages / "widget-1.0.dist-info" / "METADATA").write_text(
        "not valid metadata at all",
        encoding="utf-8",
    )
    assert distribution.dependencies() == []


def _widget_metadata(version: str) -> str:
    return f"Metadata-Version: 2.1\nName: widget\nVersion: {version}\n"


def test_find_installed_matches_an_uncached_scan(tmp_path: Path) -> None:
    from cpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(site_packages, "widget-1.2.3.dist-info", _widget_metadata("1.2.3"))
    _write_dist_info(
        site_packages,
        "Other_Thing-0.1.dist-info",
        "Metadata-Version: 2.1\nName: Other_Thing\nVersion: 0.1\n",
    )
    clear_installed_index()
    paths = [str(site_packages)]
    found = find_installed("WIDGET", paths)
    assert found is not None
    assert (found.name, found.raw_version, found.version) == (
        "widget",
        "1.2.3",
        Version("1.2.3"),
    )
    assert find_installed("other-thing", paths) is not None
    assert find_installed("missing", paths) is None
    expected = {
        d.canonical_name: d.raw_version for d in iter_installed_distributions(paths)
    }
    assert expected == {"widget": "1.2.3", "other-thing": "0.1"}


def test_find_installed_reads_metadata_once_per_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    from cpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    for index in range(5):
        _write_dist_info(
            site_packages,
            f"pkg{index}-1.0.dist-info",
            f"Metadata-Version: 2.1\nName: pkg{index}\nVersion: 1.0\n",
        )
    clear_installed_index()
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):  # noqa: ANN001, ANN202
        if isinstance(file, str) and file.endswith("METADATA"):
            opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    paths = [str(site_packages)]
    for name in ("pkg0", "pkg3", "pkg4", "nope", "pkg0"):
        find_installed(name, paths)
    # One scan: five METADATA files, read once each, for five lookups.
    assert len(opened) == 5


def test_find_installed_notices_a_new_distribution(tmp_path: Path) -> None:
    import os
    import time

    from cpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(site_packages, "widget-1.2.3.dist-info", _widget_metadata("1.2.3"))
    clear_installed_index()
    paths = [str(site_packages)]
    assert find_installed("gadget", paths) is None
    _write_dist_info(
        site_packages,
        "gadget-2.0.dist-info",
        "Metadata-Version: 2.1\nName: gadget\nVersion: 2.0\n",
    )
    # Creating the dist-info directory bumps site-packages' mtime; make the
    # bump unambiguous for coarse filesystem clocks.
    later = time.time() + 2
    os.utime(site_packages, (later, later))
    found = find_installed("gadget", paths)
    assert found is not None
    assert found.raw_version == "2.0"


def test_find_installed_first_path_wins(tmp_path: Path) -> None:
    from cpip.core.metadata import clear_installed_index, find_installed

    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_dist_info(first, "widget-1.0.dist-info", _widget_metadata("1.0"))
    _write_dist_info(second, "widget-2.0.dist-info", _widget_metadata("2.0"))
    clear_installed_index()
    found = find_installed("widget", [str(first), str(second)])
    assert found is not None
    assert found.raw_version == "1.0"
    found = find_installed("widget", [str(second), str(first)])
    assert found is not None
    assert found.raw_version == "2.0"


def test_find_installed_accepts_a_generator_of_paths(tmp_path: Path) -> None:
    from cpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(site_packages, "widget-1.2.3.dist-info", _widget_metadata("1.2.3"))
    clear_installed_index()
    found = find_installed("widget", (path for path in [str(site_packages)]))
    assert found is not None
    assert found.raw_version == "1.2.3"
    # And the cached index built from that generator is the full one.
    assert find_installed("widget", [str(site_packages)]) is found


def test_default_and_explicit_scans_are_cached_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``paths=None`` consults every metadata finder; an explicit list equal to
    sys.path consults only the path finder. Neither order may let one answer
    stand in for the other."""
    import sys

    from cpip.core import metadata as metadata_module

    metadata_module.clear_installed_index()
    calls: list[object] = []
    real = metadata_module._iter_raw_distributions

    def recording(paths):  # noqa: ANN001, ANN202
        calls.append("<default>" if paths is None else list(paths))
        return real(paths)

    monkeypatch.setattr(metadata_module, "_iter_raw_distributions", recording)
    explicit = list(sys.path)
    for order in ((None, explicit), (explicit, None)):
        metadata_module.clear_installed_index()
        calls.clear()
        for paths in order:
            metadata_module.find_installed("not-installed-anywhere", paths)
        assert len(calls) == 2
        assert "<default>" in calls
        assert any(call != "<default>" for call in calls)
        # Repeats hit the respective cached index.
        for paths in order:
            metadata_module.find_installed("not-installed-anywhere", paths)
        assert len(calls) == 2


def _stdlib_view(paths: list[str]) -> list[tuple[str, str, str]]:
    """(name, metadata path, location) for every entry the stdlib finds
    that carries metadata -- the same filter cpip applies."""
    found = []
    for dist in importlib.metadata.distributions(path=paths):
        text = dist.read_text("METADATA") or dist.read_text("PKG-INFO")
        if not text:
            text = dist.read_text("")
        if not text:
            continue
        found.append(
            (
                dist.metadata["Name"],
                str(getattr(dist, "_path", None)),
                str(dist.locate_file("")),
            )
        )
    return sorted(found)


def _cpip_view(paths: list[str]) -> list[tuple[str, str, str]]:
    return sorted(
        (dist.name, str(dist.metadata_location), dist.location)
        for dist in iter_installed_distributions(paths=paths)
    )


def _populate_mixed_root(root: Path) -> None:
    _write_dist_info(root, "demo-1.0.dist-info", "Name: demo\nVersion: 1.0\n")
    _write_dist_info(
        root, "Up-3.0.DIST-INFO", "Name: Up\nVersion: 3.0\n"
    )  # suffix match is case-insensitive
    _write_dist_info(
        root, "legacy-0.1.egg-info", "Name: legacy\nVersion: 0.1\n", filename="PKG-INFO"
    )
    (root / "flat-2.0.egg-info").write_text("Name: flat\nVersion: 2.0\n")
    (root / "empty-4.0.dist-info").mkdir()  # no metadata file: skipped by both
    (root / "notes.txt").write_text("not a distribution\n")
    (root / "package").mkdir()


def test_directory_scan_matches_stdlib_for_every_root_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct listing reports exactly the stdlib's entries, metadata
    paths and locations, however the root is spelled."""
    root = tmp_path / "site-packages"
    root.mkdir()
    _populate_mixed_root(root)

    for spelling in (
        str(root),
        f"{root}/",
        f"{root}/./",
        str(root.relative_to(tmp_path)),
    ):
        monkeypatch.chdir(tmp_path)
        view = _cpip_view([spelling])
        assert view == _stdlib_view([spelling]), spelling
        assert sorted(name for name, _, _ in view) == ["Up", "demo", "flat", "legacy"]

    # "" is the current directory, as for the stdlib (and `python -m`).
    monkeypatch.chdir(root)
    assert _cpip_view([""]) == _stdlib_view([""])
    assert len(_cpip_view([""])) == 4
    assert _cpip_view(["."]) == _stdlib_view(["."])


def test_directory_scan_skips_roots_the_stdlib_skips(tmp_path: Path) -> None:
    plain_file = tmp_path / "plain.txt"
    plain_file.write_text("x")
    for root in (str(tmp_path / "missing"), str(plain_file)):
        assert _cpip_view([root]) == []
        assert _stdlib_view([root]) == []


def test_zip_and_egg_roots_still_go_through_the_stdlib(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("zipped-5.0.dist-info/METADATA", "Name: zipped\nVersion: 5.0\n")
    egg = tmp_path / "legacy-6.0.egg"
    _write_dist_info(
        egg, "EGG-INFO", "Name: legacy\nVersion: 6.0\n", filename="PKG-INFO"
    )
    _write_dist_info(egg, "inner-7.0.dist-info", "Name: inner\nVersion: 7.0\n")

    for root in (str(archive), str(egg)):
        view = _cpip_view([root])
        assert view == _stdlib_view([root]), root
        assert view, root
    assert [name for name, _, _ in _cpip_view([str(archive)])] == ["zipped"]
    assert sorted(name for name, _, _ in _cpip_view([str(egg)])) == ["inner", "legacy"]


def test_default_scan_honours_other_distribution_finders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finder on sys.meta_path that offers find_distributions (a custom
    importer) keeps the default scan on the stdlib so its entries are seen."""
    import sys

    from cpip.core.metadata import clear_installed_index, find_installed

    private = tmp_path / "private"
    _write_dist_info(
        private, "finder_only-1.0.dist-info", "Name: finder-only\nVersion: 1.0\n"
    )

    class PrivateFinder:
        @staticmethod
        def find_distributions(context=None):  # noqa: ANN001, ANN205
            yield importlib.metadata.PathDistribution(
                private / "finder_only-1.0.dist-info"
            )

    clear_installed_index()
    try:
        assert find_installed("finder-only") is None
        monkeypatch.setattr(sys, "meta_path", [PrivateFinder(), *sys.meta_path])
        clear_installed_index()
        found = find_installed("finder-only")
        assert found is not None
        assert found.raw_version == "1.0"
        # An explicit path list consults only the path finder, as before.
        assert find_installed("finder-only", [str(tmp_path)]) is None
    finally:
        clear_installed_index()


def test_path_distribution_answers_like_the_stdlib(tmp_path: Path) -> None:
    root = tmp_path / "site-packages"
    _write_dist_info(
        root,
        "widget-1.2.3.dist-info",
        "Name: widget\nVersion: 1.2.3\nRequires-Dist: gadget>=1.0\n",
    )
    dist_info = root / "widget-1.2.3.dist-info"
    (dist_info / "RECORD").write_text(
        "widget/__init__.py,,\nwidget-1.2.3.dist-info/RECORD,,\n"
    )
    (dist_info / "top_level.txt").write_text("widget\n")
    (dist_info / "sub").mkdir()

    [distribution] = iter_installed_distributions(paths=[str(root)])
    stdlib_dist = importlib.metadata.PathDistribution(dist_info)

    for name in ("RECORD", "top_level.txt", "METADATA", "missing", "sub"):
        assert distribution.raw.read_text(name) == stdlib_dist.read_text(name), name
    with pytest.raises(FileNotFoundError):
        distribution.read_text("missing")
    assert distribution.files() == sorted(str(f) for f in stdlib_dist.files or ())
    assert distribution.metadata.get_all("Requires-Dist") == ["gadget>=1.0"]
    assert distribution.metadata["Name"] == "widget"
    assert str(distribution.raw.locate_file("")) == str(root)


def test_installed_distribution_keeps_a_legacy_version_as_text(tmp_path: Path) -> None:
    """A non-PEP 440 version never matches a Version (so it is replaced on
    install and ignored by the resolver) but the distribution stays
    inspectable and removable through its text."""
    from cpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "legacy-1.0_beta.dist-info",
        "Metadata-Version: 2.1\nName: legacy\nVersion: 1.0 beta\n",
    )
    clear_installed_index()
    found = find_installed("legacy", [str(site_packages)])
    assert found is not None
    assert found.raw_version == "1.0 beta"
    assert found.version is None
    assert found.version != Version("1.0")


class _RecordingHeaderCache:
    """An in-memory stand-in for the wheel metadata store's header side."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, int, int], dict[str, list[str]]] = {}
        self.prefetched: list[list[tuple[str, int, int]]] = []
        self.puts: list[tuple[str, int, int]] = []

    def prefetch(self, identities) -> None:  # noqa: ANN001
        self.prefetched.append(list(identities))

    def get_reference(self, identity):  # noqa: ANN001, ANN202
        return self.entries.get(identity)

    def put(self, identity, headers) -> None:  # noqa: ANN001
        self.puts.append(identity)
        self.entries[identity] = headers


@pytest.fixture
def header_cache():  # noqa: ANN201
    from cpip.core.metadata import clear_installed_index, use_header_cache

    cache = _RecordingHeaderCache()
    clear_installed_index()
    use_header_cache(cache)
    try:
        yield cache
    finally:
        use_header_cache(None)
        clear_installed_index()


def test_header_cache_serves_unchanged_metadata_without_reading_it(
    tmp_path: Path, header_cache: _RecordingHeaderCache
) -> None:
    import os

    from cpip.index.metadata_cache import metadata_identity

    root = tmp_path / "site-packages"
    _write_dist_info(
        root,
        "widget-1.0.dist-info",
        "Name: widget\nVersion: 1.0\nRequires-Dist: gadget>=1.0\n",
    )
    _write_dist_info(
        root, "legacy-0.1.egg-info", "Name: legacy\nVersion: 0.1\n", filename="PKG-INFO"
    )
    metadata_file = root / "widget-1.0.dist-info" / "METADATA"

    first = {d.name: d for d in iter_installed_distributions(paths=[str(root)])}
    assert sorted(first) == ["legacy", "widget"]
    # Only a METADATA file has an identity; PKG-INFO-only entries are read
    # as before and never cached. The key is the wheel store's own shape.
    identity = metadata_identity(metadata_file)
    assert header_cache.puts == [identity]
    assert header_cache.prefetched == [[identity]]
    assert [str(dep) for dep in first["widget"].dependencies()] == ["gadget>=1.0"]

    # Same size and mtime: the cached headers are trusted and the file is
    # not read, so a same-size in-place rewrite is invisible (as documented
    # for the in-process index).
    stat = metadata_file.stat()
    metadata_file.write_text("Name: widget\nVersion: 9.9\nRequires-Dist: gadget>=1.0\n")
    os.utime(metadata_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = {d.name: d for d in iter_installed_distributions(paths=[str(root)])}
    assert second["widget"].raw_version == "1.0"
    assert header_cache.puts == [identity]
    assert [str(dep) for dep in second["widget"].dependencies()] == ["gadget>=1.0"]

    # A different mtime is a different identity: read and cached afresh.
    os.utime(metadata_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = {d.name: d for d in iter_installed_distributions(paths=[str(root)])}
    assert third["widget"].raw_version == "9.9"
    assert len(header_cache.puts) == 2
    assert header_cache.puts[1] == metadata_identity(metadata_file)


def test_header_cache_round_trips_through_the_wheel_metadata_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second process finds the first one's headers on disk and reads no
    METADATA file for an unchanged environment."""
    from cpip.core.metadata import clear_installed_index, use_header_cache
    from cpip.index.metadata_cache import WheelMetadataCache

    root = tmp_path / "site-packages"
    for index in range(5):
        _write_dist_info(
            root,
            f"pkg{index}-1.{index}.dist-info",
            f"Name: pkg{index}\nVersion: 1.{index}\nRequires-Dist: pkg0\n",
        )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    first_store = WheelMetadataCache(cache_dir)
    use_header_cache(first_store)
    try:
        clear_installed_index()
        first = iter_installed_distributions(paths=[str(root)])
        assert len(first) == 5
        first_store.flush()

        second_store = WheelMetadataCache(cache_dir)
        use_header_cache(second_store)
        clear_installed_index()

        def no_reads(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError(f"METADATA read on a warm scan: {args[0]}")

        monkeypatch.setattr("cpip.core.metadata._read_text_file", no_reads)
        second = iter_installed_distributions(paths=[str(root)])
    finally:
        use_header_cache(None)
        clear_installed_index()

    assert [(d.name, d.raw_version) for d in second] == [
        (d.name, d.raw_version) for d in first
    ]
    assert [str(dep) for dep in second[3].dependencies()] == ["pkg0"]
    assert not second_store._pending_puts
