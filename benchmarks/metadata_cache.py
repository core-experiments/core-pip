"""Metadata-cache scaling benchmarks for the local resolver."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import cast

from cpip.resolution.fast_local_wheelhouse import load_candidate, wheel_name
from cpip.resolution.fast_wheelhouse.cache import candidate_cache

from .uv_scenarios import make_metadata_wheel


WHEEL_COUNTS = (10, 100, 1_000, 10_000)
CACHE_STATES = ("cold", "warm", "invalidate")


def create_metadata_workload(root: Path) -> dict[str, object]:
    """Create one deterministic wheelhouse for each cache-size benchmark."""
    wheelhouses: dict[str, str] = {}
    for count in WHEEL_COUNTS:
        wheelhouse = root / str(count)
        wheelhouse.mkdir(parents=True)
        for index in range(count):
            make_metadata_wheel(
                wheelhouse,
                f"metadata-bench-{index:05}",
                "1.0",
            )
        wheelhouses[str(count)] = os.fspath(wheelhouse)
    return {"wheelhouses": wheelhouses}


class MetadataCacheScaling:
    """Measure metadata reads as wheelhouse size and cache state vary."""

    params = (tuple(str(count) for count in WHEEL_COUNTS), CACHE_STATES)
    param_names = ("wheel_count", "cache_state")
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 300

    @staticmethod
    def setup_cache() -> dict[str, object]:
        root = Path.cwd() / "metadata-cache-workload"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return create_metadata_workload(root)

    def setup(
        self,
        state: dict[str, object],
        wheel_count: str,
        cache_state: str,
    ) -> None:
        wheelhouses = cast(dict[str, str], state["wheelhouses"])
        wheelhouse = Path(wheelhouses[wheel_count])
        self.entries = sorted(wheelhouse.glob("*.whl"))
        self.parsed = {str(path): wheel_name(str(path)) for path in self.entries}
        self.cache = {}
        if cache_state in {"warm", "invalidate"}:
            self.load_all()
        if cache_state == "invalidate":
            self.invalidated = self.entries[len(self.entries) // 2]

    def load_all(self) -> None:
        for path in self.entries:
            parsed = self.parsed[str(path)]
            assert parsed is not None
            load_candidate(str(path), self.cache, parsed)

    def time_metadata(
        self, state: dict[str, object], wheel_count: str, cache_state: str
    ) -> None:
        if cache_state == "cold":
            self.cache.clear()
            candidate_cache.clear()
        elif cache_state == "invalidate":
            stat = self.invalidated.stat()
            os.utime(
                self.invalidated,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1),
            )
        self.load_all()
