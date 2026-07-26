from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from pip.build import build as build_module
from pip.build.build_backend import (
    ProjectBuilder,
    prepare_project_metadata,
    ProjectMetadataReader,
)
from pip.core.errors import BuildError


def test_build_backend_builds_static_wheel_with_typed_marker(tmp_path: Path) -> None:
    project = write_project(tmp_path, "typed-pkg", "typed_pkg", "1.0")
    (project / "src" / "typed_pkg" / "py.typed").write_text("", encoding="utf-8")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    assert wheel_name == "typed_pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        assert "typed_pkg/__init__.py" in archive.namelist()
        assert "typed_pkg/py.typed" in archive.namelist()
        metadata = archive.read("typed_pkg-1.0.dist-info/METADATA").decode()
    assert "Name: typed-pkg\n" in metadata
    assert "Version: 1.0\n" in metadata


def test_build_backend_includes_package_data(tmp_path: Path) -> None:
    project = write_project(tmp_path, "data-pkg", "data_pkg", "1.0")
    package_dir = project / "src" / "data_pkg"
    package_dir.joinpath("payload.dat").write_text("Data\n", encoding="utf-8")
    package_dir.joinpath(".hidden").write_text("Hidden\n", encoding="utf-8")
    pycache_dir = package_dir / "__pycache__"
    pycache_dir.mkdir()
    pycache_dir.joinpath("module.pyc").write_bytes(b"bytecode")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        names = set(archive.namelist())
        assert "data_pkg/payload.dat" in names
        assert "data_pkg/.hidden" not in names
        assert "data_pkg/__pycache__/module.pyc" not in names


def test_build_backend_builds_editable_wheel(tmp_path: Path) -> None:
    project = write_project(tmp_path, "editable-pkg", "editable_pkg", "1.0")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_editable(wheel_dir)

    assert wheel_name == "editable_pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        pth = archive.read("__editable__.editable_pkg.pth").decode()
        assert pth == str((project / "src").resolve()) + "\n"
        assert "editable_pkg-1.0.dist-info/METADATA" in archive.namelist()
        assert "editable_pkg-1.0.dist-info/RECORD" in archive.namelist()


def test_build_backend_reads_setup_py_console_scripts(tmp_path: Path) -> None:
    project = tmp_path / "script-pkg"
    project.mkdir()
    project.joinpath("script_pkg.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "\n".join(
            [
                "from setuptools import setup",
                "setup(",
                '    name="script-pkg",',
                '    version="1.0",',
                '    py_modules=["script_pkg"],',
                "    entry_points=dict("
                'console_scripts=["script-pkg=script_pkg:main"]),',
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        entry_points = archive.read(
            "script_pkg-1.0.dist-info/entry_points.txt"
        ).decode()
    assert "script-pkg = script_pkg:main" in entry_points


def test_build_backend_uses_setuptools_for_dynamic_legacy_metadata(
    tmp_path: Path,
) -> None:
    project = tmp_path / "dynamic-pkg"
    project.mkdir()
    project.joinpath("dynamic_pkg.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "\n".join(
            [
                "from setuptools import setup",
                "",
                "def project_version():",
                "    return '4.1.3'",
                "",
                "setup(",
                "    name='dynamic-pkg',",
                "    version=project_version(),",
                "    py_modules=['dynamic_pkg'],",
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    metadata = prepare_project_metadata(project)
    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    assert metadata.name == "dynamic-pkg"
    assert metadata.version == "4.1.3"
    assert wheel_name == "dynamic_pkg-4.1.3-py3-none-any.whl"


def test_build_backend_defaults_to_setuptools_when_backend_is_omitted(
    tmp_path: Path,
) -> None:
    project = tmp_path / "default-backend-pkg"
    project.mkdir()
    project.joinpath("default_backend_pkg.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='default-backend-pkg', version='2.3.4', py_modules=['default_backend_pkg'])\n",
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    metadata = prepare_project_metadata(project)
    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    assert metadata.name == "default-backend-pkg"
    assert metadata.version == "2.3.4"
    assert wheel_name == "default_backend_pkg-2.3.4-py3-none-any.whl"


def test_build_backend_rejects_invalid_package_version(tmp_path: Path) -> None:
    project = tmp_path / "bad-version-pkg"
    package_dir = project / "src" / "bad_version_pkg"
    package_dir.mkdir(parents=True)
    package_dir.joinpath("__init__.py").write_text(
        '__version__ = "not-a-version"\n',
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text("", encoding="utf-8")

    with pytest.raises(BuildError, match="use the project's build backend"):
        ProjectMetadataReader(project).read()


def test_default_wheel_directories_are_isolated() -> None:
    first = build_module.default_wheel_dir()
    second = build_module.default_wheel_dir()

    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def write_project(tmp_path: Path, name: str, package: str, version: str) -> Path:
    project = tmp_path / name
    package_dir = project / "src" / package
    package_dir.mkdir(parents=True)
    package_dir.joinpath("__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    project.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                f'version = "{version}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return project
