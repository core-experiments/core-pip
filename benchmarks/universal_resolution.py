"""Cross-environment resolution workloads inspired by uv's universal resolver."""

from __future__ import annotations

import shutil
from pathlib import Path

from cpip.resolution.resolver import Resolver

from .uv_scenarios import make_metadata_wheel


PYTHON_TARGETS = ("3.9", "3.12", "3.14")
PACKAGE_COUNT = 127


def create_universal_case(root: Path) -> dict[str, object]:
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    for index in range(PACKAGE_COUNT):
        name = f"universal-provider-{index:03}"
        requirements = ()
        if index:
            requirements = (
                f"universal-provider-{index - 1:03}>=1; python_version >= '3.11'",
            )
        make_metadata_wheel(wheelhouse, name, "1.0", requires=requirements)
    requirements = tuple(
        f"universal-provider-{index:03}>=1; python_version >= '3.11'"
        for index in range(PACKAGE_COUNT)
    )
    return {"wheelhouse": wheelhouse, "requirements": requirements}


class CrossEnvironmentResolution:
    """Resolve one marker-rich graph for each supported target Python version."""

    params = (PYTHON_TARGETS,)
    param_names = ("python_version",)
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 180

    @staticmethod
    def setup_cache() -> dict[str, object]:
        root = Path.cwd() / "universal-resolution"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir()
        return create_universal_case(root)

    def setup(self, state: dict[str, object], python_version: str) -> None:
        self.cache = Path(state["wheelhouse"]) / ".cache" / python_version
        shutil.rmtree(self.cache, ignore_errors=True)
        self.requirements = list(state["requirements"])
        self.wheelhouse = str(state["wheelhouse"])
        self.python_version = python_version

    def time_resolve(self, state: dict[str, object], python_version: str) -> None:
        plan = Resolver(
            find_links=[self.wheelhouse],
            no_index=True,
            ignore_installed=True,
            compute_source_hashes=False,
            python_version=self.python_version,
        ).resolve(self.requirements)
        if plan is None:
            raise AssertionError(f"no plan for Python {python_version}")
