from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

from benchmarks.uv_scenarios import (
    MANY_FILES,
    SNAPSHOT_DISK_LIMIT,
    create_many_files_archives,
    create_offline_scenarios,
)
from cpip.index.provider import CandidateProvider
from cpip.resolution.resolver import Resolver


def test_offline_scenarios_resolve_within_disk_budget(tmp_path: Path) -> None:
    scenarios = create_offline_scenarios(tmp_path)
    size = sum(path.stat().st_size for path in tmp_path.rglob("*.whl"))

    assert size < SNAPSHOT_DISK_LIMIT
    for scenario in scenarios.values():
        provider = CandidateProvider.from_options(
            find_links=[str(scenario["wheelhouse"])], no_index=True
        )
        plan = Resolver(provider=provider, ignore_installed=True).resolve(
            list(scenario["requirements"])
        )
        assert len(plan.candidates) == scenario["expected_projects"]


def test_many_files_archives_match_uv_fixture_size(tmp_path: Path) -> None:
    archives = create_many_files_archives(tmp_path)

    with zipfile.ZipFile(archives["wheel"]) as wheel:
        package_files = [
            name for name in wheel.namelist() if name.startswith("manyfiles/")
        ]
    with tarfile.open(archives["sdist"]) as sdist:
        package_files_sdist = [
            member
            for member in sdist.getmembers()
            if member.name.startswith("manyfiles-0.0.0/manyfiles/")
        ]

    assert len(package_files) == MANY_FILES + 1
    assert len(package_files_sdist) == MANY_FILES
