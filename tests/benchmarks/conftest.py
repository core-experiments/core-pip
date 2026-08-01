"""Session fixtures for the CodSpeed benchmark suite.

The fixtures build every workload once per session so that the benchmarked
callables only measure pip's own work and not the cost of generating inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmark_support import (
    make_backtracking_graph,
    make_dependency_graph,
    make_wheel,
    simple_index_html,
    simple_index_json,
    requirement_lines,
)


@pytest.fixture(scope="session")
def index_html() -> str:
    return simple_index_html()


@pytest.fixture(scope="session")
def index_json() -> str:
    return simple_index_json()


@pytest.fixture(scope="session")
def graph_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("graph-wheelhouse")
    make_dependency_graph(wheelhouse)
    return wheelhouse


@pytest.fixture(scope="session")
def backtracking_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("backtracking-wheelhouse")
    make_backtracking_graph(wheelhouse)
    return wheelhouse


@pytest.fixture(scope="session")
def payload_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("payload-wheelhouse")
    return make_wheel(wheelhouse, "payload-pkg", "1.0.0", payload_files=300)


@pytest.fixture(scope="session")
def requirements_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("requirements") / "requirements.txt"
    lines = ["# generated requirements", "--no-binary :all:"]
    lines.extend(requirement_lines())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
