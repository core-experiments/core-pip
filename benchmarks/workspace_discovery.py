"""Large workspace-style TOML discovery and dependency collection benchmarks."""

from __future__ import annotations

import shutil
import json
import tomllib
from pathlib import Path


MEMBER_COUNT = 127


def create_workspace(root: Path) -> None:
    root.mkdir(parents=True)
    for index in range(MEMBER_COUNT):
        member = root / "packages" / f"provider-{index:03}"
        member.mkdir(parents=True)
        dependencies = [f"provider-{max(0, index - offset):03}>=1" for offset in range(1, 5)]
        dependencies.append("sniffio>=1,<2")
        (member / "pyproject.toml").write_text(
            "[project]\n"
            f'name = "provider-{index:03}"\n'
            'version = "1.0"\n'
            f"dependencies = {json.dumps(dependencies)}\n"
            "[tool.workspace]\n"
            f"member = {index}\n"
            'tags = ["discover", "validate", "report"]\n',
            encoding="utf-8",
        )
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"workspace\"\nversion = \"1.0\"\n"
        "[tool.workspace]\nmembers = [\"packages/*\"]\n",
        encoding="utf-8",
    )


class WorkspaceDiscovery:
    params = ("cold", "warm")
    param_names = ("cache_state",)
    number = 1
    repeat = 5
    rounds = 2
    warmup_time = 0

    @staticmethod
    def setup_cache() -> Path:
        root = Path.cwd() / "workspace-discovery"
        shutil.rmtree(root, ignore_errors=True)
        create_workspace(root)
        return root

    def setup(self, root: Path, cache_state: str) -> None:
        self.root = root
        self.files = tuple(sorted(root.glob("packages/*/pyproject.toml")))
        self.cached: tuple[dict[str, object], ...] | None = None
        if cache_state == "warm":
            self.cached = tuple(
                tomllib.loads(path.read_text(encoding="utf-8")) for path in self.files
            )

    def time_discover(self, root: Path, cache_state: str) -> None:
        documents = self.cached or tuple(
            tomllib.loads(path.read_text(encoding="utf-8")) for path in self.files
        )
        dependency_count = sum(
            len(document.get("project", {}).get("dependencies", ()))
            for document in documents
        )
        if dependency_count == 0:
            raise AssertionError("workspace has no dependencies")
