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
    import importlib.metadata
    import sys

    from cpip.core import metadata as metadata_module

    metadata_module.clear_installed_index()
    calls: list[object] = []
    real = importlib.metadata.distributions

    def recording(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs.get("path", "<default>"))
        return real(**kwargs)

    monkeypatch.setattr(importlib.metadata, "distributions", recording)
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
