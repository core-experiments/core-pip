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
    make_failing_source_tree,
    make_isolated_source_tree,
    make_nab_deep_backjump_family,
    make_nab_pip_backtracking_family,
    make_nab_smoke_fixture,
    make_source_tree,
    make_stress_graph,
    make_wheel,
    make_wrong_package_graph,
    requirement_lines,
    simple_index_html,
    simple_index_json,
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
def wrong_package_wheelhouses(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    cases = {}
    for name in (
        "boto3-urllib3",
        "numpy-numba",
        "sentry-rapidjson",
        "starlette-fastapi",
        "apache-beam-dill",
    ):
        wheelhouse = tmp_path_factory.mktemp(f"{name}-wheelhouse")
        make_wrong_package_graph(wheelhouse, name, versions=64)
        cases[name] = wheelhouse
    return cases


@pytest.fixture(scope="session")
def catastrophic_wheelhouses(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """The same wrong-package families, sized to their real PyPI release counts.

    ``wrong_package_wheelhouses`` models these at a fixed 64 versions for a
    fast regression signal. This models the same reported incidents
    (https://github.com/astral-sh/uv/issues/8157) at the scale that actually
    made them notable, from live PyPI release counts recorded 2026-08-12:
    boto3 2093 / botocore 2491, numpy 150 / numba 136 / llvmlite 73,
    python-rapidjson 66 / sentry-kafka-schemas 246, starlette 202 /
    fastapi 317, apache-beam 108 / dill 30. ``make_wrong_package_graph``
    takes one version count for the whole family, so each case uses the
    larger of its two real package's counts, rounded for a clean number.
    """
    cases = {}
    for name, versions in (
        ("boto3-urllib3", 2048),
        ("numpy-numba", 150),
        ("sentry-rapidjson", 256),
        ("starlette-fastapi", 320),
        ("apache-beam-dill", 108),
    ):
        wheelhouse = tmp_path_factory.mktemp(f"catastrophic-{name}-wheelhouse")
        make_wrong_package_graph(wheelhouse, name, versions=versions)
        cases[name] = wheelhouse
    return cases


@pytest.fixture(scope="session")
def stress_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("stress-wheelhouse")
    make_stress_graph(wheelhouse)
    return wheelhouse


@pytest.fixture(scope="session")
def candidate_scan_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("candidate-scan-wheelhouse")
    for index in range(128):
        make_wheel(
            wheelhouse,
            "candidate-scan",
            f"1.{index}.0",
            # Keep the newest quarter out of this interpreter's candidate set
            # so the scan includes realistic Requires-Python rejection.
            requires_python=">=99" if index >= 96 else ">=3.9",
        )
    return wheelhouse


@pytest.fixture(scope="session")
def metadata_variation_wheels(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    wheelhouse = tmp_path_factory.mktemp("metadata-variation-wheelhouse")
    wheels = []
    for index in range(96):
        requirements = [f"variation-dependency-{index % 12}>=1"]
        if index % 3 == 0:
            requirements.append("variation-extra>=2")
        wheels.append(
            make_wheel(
                wheelhouse,
                "metadata-variation",
                f"1.{index}.0",
                requires=requirements,
                requires_python=f">={3 + index % 2}.9",
            ),
        )
    return wheels


@pytest.fixture(scope="session")
def source_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_source_tree(tmp_path_factory.mktemp("source-build"))


@pytest.fixture(scope="session")
def isolated_source_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_isolated_source_tree(tmp_path_factory.mktemp("isolated-build"))


@pytest.fixture(scope="session")
def failing_source_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_failing_source_tree(tmp_path_factory.mktemp("failing-build"))


@pytest.fixture(scope="session")
def nab_smoke_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("nab-smoke-wheelhouse")
    make_nab_smoke_fixture(wheelhouse)
    make_nab_pip_backtracking_family(wheelhouse, "nab-smoke-pip", 24)
    make_nab_pip_backtracking_family(
        wheelhouse,
        "nab-smoke-pip-unsat",
        24,
        unsatisfiable=True,
    )
    make_nab_deep_backjump_family(wheelhouse, "nab-smoke-backjump", 6)
    return wheelhouse


@pytest.fixture(scope="session")
def backjump_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("backjump-wheelhouse")
    make_wheel(wheelhouse, "python", "3.12")
    for version in ("4.3.3", "3.0.1", "2.0.0"):
        make_wheel(wheelhouse, "lz4", version)
    make_wheel(
        wheelhouse,
        "clickhouse-driver",
        "0.2.9",
        requires=["lz4", "lz4<=3.0.1"],
    )
    return wheelhouse


@pytest.fixture(scope="session")
def unsatisfiable_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("unsatisfiable-wheelhouse")
    requirements = []
    for index in range(24):
        shared = f"unsat-shared-{index}"
        branch = f"unsat-branch-{index}"
        make_wheel(wheelhouse, shared, "1.0.0")
        make_wheel(wheelhouse, shared, "2.0.0")
        make_wheel(wheelhouse, branch, "1.0.0", requires=[f"{shared}==1.0.0"])
        requirements.extend((f"{branch}==1.0.0", f"{shared}==2.0.0"))
    make_wheel(wheelhouse, "unsatisfiable-root", "1.0.0", requires=requirements)
    return wheelhouse


@pytest.fixture(scope="session")
def extras_marker_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("extras-marker-wheelhouse")
    for index in range(24):
        make_wheel(wheelhouse, f"extra-common-{index}", "1.0.0")
        make_wheel(wheelhouse, f"extra-dev-{index}", "1.0.0")
        make_wheel(wheelhouse, f"extra-platform-{index}", "1.0.0")
    dependencies = []
    for index in range(24):
        dependencies.extend(
            (
                f"extra-common-{index}>=1; extra == 'all'",
                f"extra-dev-{index}>=1; extra == 'dev'",
                f"extra-platform-{index}>=1; sys_platform == 'linux'",
            ),
        )
    make_wheel(
        wheelhouse,
        "extras-root",
        "1.0.0",
        requires=dependencies,
    )
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
