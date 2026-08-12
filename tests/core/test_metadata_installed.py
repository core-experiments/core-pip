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

from cpip.core.metadata import iter_installed_distributions


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
    assert distribution.version == stdlib_dist.version
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
    assert distribution.version == "0.1"
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
