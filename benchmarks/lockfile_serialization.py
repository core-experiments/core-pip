"""Benchmarks for the lockfile serializers used by cpip lock."""

from __future__ import annotations

from typing import cast

from cpip.cli.commands.fast_lock import render_lock as render_fast_lock
from cpip.cli.commands.lock import render_lock as render_lock

PACKAGE_COUNTS = (10, 100, 1_000, 10_000)


def create_lock_workload(count: int) -> dict[str, object]:
    packages: list[dict[str, object]] = []
    fast_packages: list[tuple[str, str, str, str, str]] = []
    for index in range(count):
        name = f"lock-bench-{index:05}"
        version = "1.0"
        digest = f"{index:064x}"[-64:]
        artifact = {
            "name": f"{name}-1.0-py3-none-any.whl",
            "url": f"https://files.example.test/{name}-1.0-py3-none-any.whl",
            "hashes": {"sha256": digest},
        }
        packages.append({"name": name, "version": version, "wheels": [artifact]})
        fast_packages.append(
            (name, version, artifact["name"], artifact["url"], digest)
        )
    return {"packages": packages, "fast_packages": fast_packages}


class LockfileSerialization:
    """Measure TOML rendering independently from resolution and file I/O."""

    params = (tuple(str(count) for count in PACKAGE_COUNTS),)
    param_names = ("package_count",)
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 300

    @staticmethod
    def setup_cache() -> dict[str, object]:
        return {
            str(count): create_lock_workload(count) for count in PACKAGE_COUNTS
        }

    def setup(self, state: dict[str, object], package_count: str) -> None:
        workload = cast(dict[str, object], state[package_count])
        self.packages = cast(list[dict[str, object]], workload["packages"])
        self.fast_packages = cast(
            list[tuple[str, str, str, str, str]], workload["fast_packages"]
        )

    def time_render(self, state: dict[str, object], package_count: str) -> None:
        render_lock(self.packages)

    def time_render_fast(self, state: dict[str, object], package_count: str) -> None:
        render_fast_lock(self.fast_packages)
