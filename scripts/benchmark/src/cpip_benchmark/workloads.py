from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path


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
            f"VALUE = {index}\n\n"
            "def compute() -> int:\n"
            "    return VALUE * 2\n"
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


def write_offline_workload(root: Path) -> tuple[Path, Path]:
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
                    f"leaf-{(middle * 2 + offset) % 24}>=1.1.0"
                    for offset in range(5)
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
    return wheelhouse, requirements


def write_live_workload(root: Path) -> tuple[Path, Path]:
    fixtures = Path(__file__).resolve().parents[2] / "requirements"
    source = root / "trio.in"
    compiled = root / "trio.txt"
    source.write_text((fixtures / "trio.in").read_text(encoding="utf-8"), encoding="utf-8")
    compiled.write_text(
        (fixtures / "compiled" / "trio.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return source, compiled


def workload_manifest(root: Path, *, workload: str) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    if workload == "offline":
        wheelhouse, requirements = write_offline_workload(root)
        return {
            "wheelhouse": str(wheelhouse),
            "source_requirements": str(requirements),
            "install_requirements": str(requirements),
        }
    source, compiled = write_live_workload(root)
    return {
        "source_requirements": str(source),
        "install_requirements": str(compiled),
    }
