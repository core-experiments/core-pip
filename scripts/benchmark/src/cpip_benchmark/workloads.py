from __future__ import annotations

import base64
import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class OfficialWorkload:
    """One workload mirrored from uv's official benchmark corpus."""

    name: str
    source: str
    description: str
    compiled: str | None = None
    constraint: str | None = None
    source_kind: Literal["requirements", "project"] = "requirements"
    python: str | None = None


OFFICIAL_WORKLOADS = (
    OfficialWorkload("airflow", "airflow.in", "Apache Airflow with all extras"),
    OfficialWorkload(
        "airflow2",
        "airflow2-req.in",
        "Airflow 2.3.4 with its official constraints",
        constraint="airflow2-constraints.txt",
        python="3.8",
    ),
    OfficialWorkload(
        "all-kinds",
        "all-kinds.in",
        "Indexes, direct URLs, sdists, wheels, and VCS",
        compiled="compiled/all-kinds.txt",
    ),
    OfficialWorkload(
        "bio_embeddings",
        "bio_embeddings.in",
        "Pathological BioEmbeddings dependency graph",
        python="3.12",
    ),
    OfficialWorkload(
        "black",
        "black.in",
        "Small common application graph",
        compiled="compiled/black.txt",
    ),
    OfficialWorkload(
        "boto3",
        "boto3.in",
        "Boto3 with an old urllib3 upper bound",
        compiled="compiled/boto3.txt",
    ),
    OfficialWorkload(
        "dtlssocket",
        "dtlssocket.in",
        "Small source-build-oriented graph",
        compiled="compiled/dtlssocket.txt",
    ),
    OfficialWorkload(
        "flyte",
        "flyte.in",
        "Large Flyte development and ML stack",
        compiled="compiled/flyte.txt",
    ),
    OfficialWorkload(
        "home-assistant",
        "home-assistant.in",
        "Very large Home Assistant dependency graph",
    ),
    OfficialWorkload(
        "jupyter",
        "jupyter.in",
        "Broad Jupyter dependency graph",
        compiled="compiled/jupyter.txt",
    ),
    OfficialWorkload(
        "meine_stadt_transparent",
        "meine_stadt_transparent.in",
        "Resolver regression case from a real project",
    ),
    OfficialWorkload(
        "pdm_2193",
        "pdm_2193.in",
        "PDM resolver regression case",
        compiled="compiled/pdm_2193.txt",
    ),
    OfficialWorkload(
        "pydantic",
        "pydantic.in",
        "Pydantic development and documentation stack",
    ),
    OfficialWorkload(
        "scispacy",
        "scispacy.in",
        "Scientific Python and spaCy stack",
        compiled="compiled/scispacy.txt",
    ),
    OfficialWorkload(
        "slow",
        "slow.in",
        "Known slow pip-tools resolver case",
    ),
    OfficialWorkload(
        "transformers-extras",
        "transformers-extras.in",
        "Transformers with its large extras matrix",
    ),
    OfficialWorkload(
        "transformers-project",
        "transformers/pyproject.toml",
        "Transformers represented as a PEP 621 project",
        source_kind="project",
    ),
    OfficialWorkload(
        "trio",
        "trio.in",
        "Trio documentation and runtime dependencies",
        compiled="compiled/trio.txt",
    ),
    OfficialWorkload(
        "backtracking-apache-beam-dill",
        "backtracking/apache-beam-dill.in",
        "Apache Beam and dill conflict case",
        python="3.10",
    ),
    OfficialWorkload(
        "backtracking-numpy-numba",
        "backtracking/numpy-numba.in",
        "NumPy and Numba backtracking case",
        python="3.12",
    ),
    OfficialWorkload(
        "backtracking-numpy-sparse",
        "backtracking/numpy-sparse.in",
        "NumPy and sparse backtracking case",
        python="3.12",
    ),
    OfficialWorkload(
        "backtracking-sentry",
        "backtracking/sentry.in",
        "Sentry schema backtracking case",
        python="3.12",
    ),
    OfficialWorkload(
        "backtracking-starlette-fastapi",
        "backtracking/starlette-fastapi.in",
        "Starlette and FastAPI backtracking case",
        python="3.12",
    ),
)

OFFICIAL_WORKLOADS_BY_NAME = {
    workload.name: workload for workload in OFFICIAL_WORKLOADS
}
OFFICIAL_WORKLOAD_NAMES = tuple(OFFICIAL_WORKLOADS_BY_NAME)
WORKLOAD_NAMES = ("offline", "live", *OFFICIAL_WORKLOAD_NAMES)


def official_workload(name: str) -> OfficialWorkload | None:
    return OFFICIAL_WORKLOADS_BY_NAME.get(name)


def make_wheel(
    wheelhouse: Path,
    project: str,
    version: str,
    *,
    requires: list[str] | None = None,
    payload_files: int = 0,
) -> Path:
    distribution = project.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    path = wheelhouse / f"{distribution}-{version}-py3-none-any.whl"
    requires_metadata = "".join(
        f"Requires-Dist: {requirement}\n" for requirement in requires or []
    )
    files = {
        f"{distribution}/__init__.py": f"NAME = {project!r}\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {project}\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.9\n"
            f"{requires_metadata}"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: cpip-benchmark\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    for index in range(payload_files):
        files[f"{distribution}/module_{index}.py"] = (
            f"VALUE = {index}\n\ndef compute() -> int:\n    return VALUE * 2\n"
        )

    rows = []
    for name, data in files.items():
        raw = data.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
        rows.append((name, f"sha256={digest.decode()}", str(len(raw))))
    rows.append((f"{dist_info}/RECORD", "", ""))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(
            f"{dist_info}/RECORD",
            "\n".join(",".join(row) for row in rows) + "\n",
        )
    return path


def write_incremental_workload(
    root: Path,
    wheelhouse: Path,
) -> tuple[Path, Path]:
    base = root / "incremental-base.txt"
    update = root / "incremental-update.txt"
    make_wheel(
        wheelhouse,
        "incremental-application",
        "1.0.0",
        payload_files=64,
    )
    make_wheel(
        wheelhouse,
        "incremental-application",
        "2.0.0",
        payload_files=96,
    )
    base.write_text("incremental-application==1.0.0\n", encoding="utf-8")
    update.write_text("incremental-application==2.0.0\n", encoding="utf-8")
    return base, update


def write_offline_workload(
    root: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    requirements = root / "requirements.in"

    for leaf in range(24):
        for minor in range(4):
            make_wheel(wheelhouse, f"leaf-{leaf}", f"1.{minor}.0")
    for middle in range(12):
        for minor in range(4):
            make_wheel(
                wheelhouse,
                f"middle-{middle}",
                f"2.{minor}.0",
                requires=[
                    f"leaf-{(middle * 2 + offset) % 24}>=1.1.0" for offset in range(5)
                ],
            )
    make_wheel(
        wheelhouse,
        "application",
        "1.0.0",
        requires=[f"middle-{index}>=2.1.0" for index in range(12)],
        payload_files=24,
    )
    requirements.write_text("application\n", encoding="utf-8")
    incremental_wheelhouse = root / "incremental-wheelhouse"
    incremental_wheelhouse.mkdir()
    incremental_base, incremental_update = write_incremental_workload(
        root,
        incremental_wheelhouse,
    )
    return (
        wheelhouse,
        requirements,
        incremental_wheelhouse,
        incremental_base,
        incremental_update,
    )


def fixture_root() -> Path:
    return Path(__file__).resolve().parents[2] / "requirements"


def copy_fixture(root: Path, relative: str) -> Path:
    source = fixture_root() / relative
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def write_official_workload(
    root: Path,
    workload: OfficialWorkload,
) -> dict[str, str]:
    source = copy_fixture(root, workload.source)
    manifest = {
        "workload": workload.name,
        "source_kind": workload.source_kind,
        "source_requirements": str(source),
        "cpip_source": str(source.parent)
        if workload.source_kind == "project"
        else str(source),
    }
    if workload.compiled is not None:
        manifest["install_requirements"] = str(
            copy_fixture(root, workload.compiled),
        )
    if workload.constraint is not None:
        manifest["constraint_requirements"] = str(
            copy_fixture(root, workload.constraint),
        )
    if workload.python is not None:
        manifest["recommended_python"] = workload.python

    incremental_wheelhouse = root / "incremental-wheelhouse"
    incremental_wheelhouse.mkdir()
    incremental_base, incremental_update = write_incremental_workload(
        root,
        incremental_wheelhouse,
    )
    manifest.update(
        {
            "incremental_wheelhouse": str(incremental_wheelhouse),
            "incremental_base_requirements": str(incremental_base),
            "incremental_update_requirements": str(incremental_update),
        },
    )
    return manifest


def workload_manifest(root: Path, *, workload: str) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    if workload == "offline":
        (
            wheelhouse,
            requirements,
            incremental_wheelhouse,
            incremental_base,
            incremental_update,
        ) = write_offline_workload(root)
        return {
            "workload": "offline",
            "source_kind": "requirements",
            "wheelhouse": str(wheelhouse),
            "source_requirements": str(requirements),
            "cpip_source": str(requirements),
            "install_requirements": str(requirements),
            "incremental_wheelhouse": str(incremental_wheelhouse),
            "incremental_base_requirements": str(incremental_base),
            "incremental_update_requirements": str(incremental_update),
        }
    definition = official_workload(workload)
    if definition is None:
        available = ", ".join(WORKLOAD_NAMES)
        raise ValueError(f"Unknown workload {workload!r}; choose one of: {available}")
    return write_official_workload(root, definition)
