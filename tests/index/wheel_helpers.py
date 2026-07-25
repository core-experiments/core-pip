from __future__ import annotations

import base64
import hashlib
import tarfile
import zipfile
from pathlib import Path


def make_wheel(
    wheelhouse: Path,
    project: str,
    import_name: str,
    version: str,
    *,
    requires: list[str] | None = None,
) -> Path:
    dist = project.replace("-", "_")
    wheel = wheelhouse / f"{dist}-{version}-py3-none-any.whl"
    requires_metadata = "".join(
        f"Requires-Dist: {requirement}\n" for requirement in requires or []
    )
    files = {
        f"{import_name}/__init__.py": f"NAME = {project!r}\n",
        f"{dist}-{version}.dist-info/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {project}\n"
            f"Version: {version}\n"
            f"{requires_metadata}"
        ),
        f"{dist}-{version}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    rows = []
    for path, data in files.items():
        raw = data.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
        rows.append((path, f"sha256={digest.decode()}", str(len(raw))))
    rows.append((f"{dist}-{version}.dist-info/RECORD", "", ""))

    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
        archive.writestr(
            f"{dist}-{version}.dist-info/RECORD",
            "\n".join(",".join(row) for row in rows) + "\n",
        )
    return wheel


def make_sdist(
    dist_dir: Path,
    project: str,
    import_name: str,
    version: str,
    *,
    requires: list[str] | None = None,
    backend: bool = False,
) -> Path:
    dist_name = project.replace("-", "_")
    root = dist_dir / f"{dist_name}-{version}"
    package = root / import_name
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text(
        f"NAME = {project!r}\n",
        encoding="utf-8",
    )
    dependencies = "\n".join(f'    "{requirement}",' for requirement in requires or [])
    pyproject_lines = []
    if backend:
        backend_dir = root / "backend"
        backend_dir.mkdir()
        backend_dir.joinpath("local_backend.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "import zipfile",
                    "",
                    "def build_wheel(\n"
                    "    wheel_directory, config_settings=None, "
                    "metadata_directory=None\n"
                    "): ",
                    f'    name = "{dist_name}-{version}-py3-none-any.whl"',
                    "    dist_info = name.removesuffix('.whl') + '.dist-info'",
                    "    target = Path(wheel_directory) / name",
                    "    with zipfile.ZipFile(target, 'w') as archive:",
                    f'        archive.writestr("{import_name}/__init__.py", '
                    f'"NAME = {project!r}\\n")',
                    '        archive.writestr(dist_info + "/METADATA", '
                    f'"Metadata-Version: 2.1\\nName: {project}\\n"'
                    f'"Version: {version}\\n")',
                    '        archive.writestr(dist_info + "/WHEEL", '
                    '"Wheel-Version: 1.0\\nTag: py3-none-any\\n")',
                    '        archive.writestr(dist_info + "/RECORD", "")',
                    "    return name",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        pyproject_lines.extend(
            [
                "[build-system]",
                "requires = []",
                'build-backend = "local_backend"',
                'backend-path = ["backend"]',
                "",
            ]
        )
    pyproject_lines.extend(
        [
            "[project]",
            f'name = "{project}"',
            f'version = "{version}"',
            "dependencies = [",
            dependencies,
            "]",
            "",
        ]
    )
    root.joinpath("pyproject.toml").write_text(
        "\n".join(pyproject_lines),
        encoding="utf-8",
    )

    archive_path = dist_dir / f"{project}-{version}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(root, arcname=root.name)
    return archive_path
