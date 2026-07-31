"""Compact fixtures modeled on uv's resolver and archive benchmarks."""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path


MANY_FILES = 10_000
SNAPSHOT_DISK_LIMIT = 25 * 1024 * 1024


class Scenario:
    __slots__ = (
        "name", "projects", "versions", "requirements", "expected_projects",
        "extras", "shared_conflict",
    )

    def __init__(
        self, name: str, projects: int, versions: int,
        requirements: tuple[str, ...], expected_projects: int,
        extras: bool = False, shared_conflict: bool = False,
    ) -> None:
        self.name = name
        self.projects = projects
        self.versions = versions
        self.requirements = requirements
        self.expected_projects = expected_projects
        self.extras = extras
        self.shared_conflict = shared_conflict


SCENARIOS = (
    Scenario("trio", 38, 6, ("trio-bench",), 38),
    Scenario("jupyter", 93, 6, ("jupyter-bench",), 93),
    Scenario(
        "airflow",
        160,
        8,
        ("airflow-bench[all]",),
        161,
        extras=True,
        shared_conflict=True,
    ),
)


BACKTRACKING_SCENARIOS = (
    "apache-beam-dill",
    "numpy-numba",
    "numpy-sparse",
    "sentry",
    "starlette-fastapi",
)


def wheel_name_internal(project: str) -> str:
    return project.replace("-", "_")


def make_metadata_wheel(
    wheelhouse: Path,
    project: str,
    version: str,
    *,
    requires: tuple[str, ...] = (),
    provides_extras: tuple[str, ...] = (),
    requires_python: str | None = None,
    files: int = 0,
) -> Path:
    """Create a tiny valid wheel whose dependency metadata drives resolution."""
    distribution = wheel_name_internal(project)
    dist_info = f"{distribution}-{version}.dist-info"
    path = wheelhouse / f"{distribution}-{version}-py3-none-any.whl"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {project}",
        f"Version: {version}",
    ]
    metadata.extend(f"Provides-Extra: {extra}" for extra in provides_extras)
    if requires_python is not None:
        metadata.append(f"Requires-Python: {requires_python}")
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(files):
            archive.writestr(f"{distribution}/{index}.txt", b"")
        archive.writestr(f"{distribution}/__init__.py", b"")
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata) + "\n")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: core-pip-asv\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return path


def create_many_files_archives(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    wheel = make_metadata_wheel(root, "manyfiles", "0.0.0", files=MANY_FILES)
    sdist = root / "manyfiles-0.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for index in range(MANY_FILES):
            info = tarfile.TarInfo(f"manyfiles-0.0.0/manyfiles/{index}.txt")
            info.size = 0
            archive.addfile(info, io.BytesIO())
        for name, contents in (
            (
                "PKG-INFO",
                b"Metadata-Version: 2.1\nName: manyfiles\nVersion: 0.0.0\n",
            ),
            (
                "pyproject.toml",
                b'[project]\nname = "manyfiles"\nversion = "0.0.0"\n',
            ),
        ):
            info = tarfile.TarInfo(f"manyfiles-0.0.0/{name}")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return {"wheel": os.fspath(wheel), "sdist": os.fspath(sdist)}


def project_name_internal(scenario: Scenario, index: int) -> str:
    if index == 0:
        return f"{scenario.name}-bench"
    return f"{scenario.name}-dep-{index:03}"


def create_breadth_scenario(root: Path, scenario: Scenario) -> dict[str, object]:
    wheelhouse = root / scenario.name
    wheelhouse.mkdir(parents=True, exist_ok=True)
    shared = f"{scenario.name}-shared"
    if scenario.shared_conflict:
        for version in range(1, scenario.versions + 1):
            make_metadata_wheel(wheelhouse, shared, f"{version}.0")

    for index in range(1, scenario.projects):
        project = project_name_internal(scenario, index)
        for version in range(1, scenario.versions + 1):
            requires: tuple[str, ...] = ()
            if scenario.shared_conflict:
                requires = (f"{shared} == {version}.0",)
            elif index + 2 < scenario.projects:
                requires = (
                    f"{project_name_internal(scenario, index + 1)} >= 1",
                    f"{project_name_internal(scenario, index + 2)} >= 1",
                )
            make_metadata_wheel(
                wheelhouse,
                project,
                f"{version}.0",
                requires=requires,
            )

    dependencies = []
    for index in range(1, scenario.projects):
        requirement = f"{project_name_internal(scenario, index)} >= 1"
        if scenario.extras and index <= 120:
            requirement += "; extra == 'all'"
        dependencies.append(requirement)
    if scenario.shared_conflict:
        dependencies.append(f"{shared} < {scenario.versions}.0")
    make_metadata_wheel(
        wheelhouse,
        project_name_internal(scenario, 0),
        "1.0",
        requires=tuple(dependencies),
        provides_extras=("all",) if scenario.extras else (),
    )
    make_metadata_wheel(wheelhouse, "incremental-addon", "1.0")
    input_file = wheelhouse / "requirements.in"
    input_file.write_text("\n".join(scenario.requirements) + "\n", encoding="utf-8")
    incremental_input = wheelhouse / "requirements-incremental.in"
    incremental_input.write_text(
        "incremental-addon\n" + input_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return {
        "name": scenario.name,
        "wheelhouse": os.fspath(wheelhouse),
        "requirements": scenario.requirements,
        "input": os.fspath(input_file),
        "incremental_input": os.fspath(incremental_input),
        "expected_projects": scenario.expected_projects,
    }


def create_backtracking_scenario(root: Path, name: str) -> dict[str, object]:
    """Create a compact 80-version conflict graph for a historical uv case."""
    wheelhouse = root / name
    wheelhouse.mkdir(parents=True, exist_ok=True)
    prefix = name.replace("-", "_")
    root_name = f"{prefix}-root"
    left = f"{prefix}-left"
    right = f"{prefix}-right"
    shared = f"{prefix}-shared"
    for version in range(1, 81):
        value = f"{version}.0"
        make_metadata_wheel(wheelhouse, shared, value)
        make_metadata_wheel(wheelhouse, left, value, requires=(f"{shared} == {value}",))
        make_metadata_wheel(
            wheelhouse,
            right,
            value,
            requires=(f"{shared} >= {version}, < {version + 1}",),
        )
        right_version = version - 1 if version > 1 else version
        make_metadata_wheel(
            wheelhouse,
            root_name,
            value,
            requires=(
                f"{left} == {value}",
                f"{right} == {right_version}.0",
            ),
        )
    make_metadata_wheel(wheelhouse, "incremental-addon", "1.0")
    input_file = wheelhouse / "requirements.in"
    input_file.write_text(f"{root_name}\n", encoding="utf-8")
    incremental_input = wheelhouse / "requirements-incremental.in"
    incremental_input.write_text(f"incremental-addon\n{root_name}\n", encoding="utf-8")
    return {
        "name": name,
        "wheelhouse": os.fspath(wheelhouse),
        "requirements": (root_name,),
        "input": os.fspath(input_file),
        "incremental_input": os.fspath(incremental_input),
        "expected_projects": 4,
    }


def create_offline_scenarios(root: Path) -> dict[str, dict[str, object]]:
    scenarios = {
        scenario.name: create_breadth_scenario(root, scenario) for scenario in SCENARIOS
    }
    scenarios.update(
        {
            name: create_backtracking_scenario(root, name)
            for name in BACKTRACKING_SCENARIOS
        }
    )
    size = sum(path.stat().st_size for path in root.rglob("*.whl"))
    if size > SNAPSHOT_DISK_LIMIT:
        raise RuntimeError(
            f"generated uv scenario snapshot is {size} bytes; "
            f"limit is {SNAPSHOT_DISK_LIMIT}"
        )
    return scenarios
