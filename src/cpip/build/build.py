from __future__ import annotations

import atexit
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .build_backend import ProjectBuilder
from cpip.core.errors import BuildError


def build_wheel_from_source(
    source: str | Path,
    wheel_dir: str | Path | None = None,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> Path:
    source_path = Path(source)
    output_dir = Path(wheel_dir) if wheel_dir is not None else default_wheel_dir()
    os.makedirs(os.fspath(output_dir), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pip-build-") as temp_dir:
        project = (
            source_path
            if os.path.isdir(os.fspath(source_path))
            else unpack_source(source_path, Path(temp_dir))
        )
        if os.path.isdir(os.fspath(source_path)):
            os.makedirs(os.fspath(project / "build"), exist_ok=True)
        wheel_name = ProjectBuilder(
            project,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        ).build_wheel(output_dir, config_settings=config_settings)
    wheel_path = output_dir / wheel_name
    if not os.path.isfile(os.fspath(wheel_path)):
        raise BuildError(f"Build backend did not create expected wheel: {wheel_name}")
    return wheel_path


def build_editable_from_source(
    source: str | Path,
    wheel_dir: str | Path | None = None,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> Path:
    source_path = Path(source)
    output_dir = Path(wheel_dir) if wheel_dir is not None else default_wheel_dir()
    os.makedirs(os.fspath(output_dir), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pip-build-editable-") as temp_dir:
        project = (
            source_path
            if os.path.isdir(os.fspath(source_path))
            else unpack_source(source_path, Path(temp_dir))
        )
        builder = ProjectBuilder(
            project,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        )
        try:
            editable_metadata = not (
                os.path.isfile(os.fspath(project / "setup.py"))
                and builder.backend_spec is not None
                and builder.backend_spec.name.startswith("setuptools.build_meta")
            )
            builder.prepare_metadata(editable=editable_metadata)
        except BuildError as exc:
            # Let build_editable translate a missing PEP 660 hook into the
            # standard actionable error. Other metadata failures remain fatal.
            if "build_editable" not in str(exc):
                raise
            if os.path.isfile(os.fspath(source_path / "setup.py")) and os.path.isfile(
                os.fspath(source_path / "pyproject.toml")
            ):
                return build_wheel_from_source(
                    source_path,
                    wheel_dir=output_dir,
                    config_settings=config_settings,
                    build_constraints=build_constraints,
                    build_isolation=build_isolation,
                )
            raise BuildError(
                f"Build backend for {source_path} is missing the 'build_editable' hook"
            ) from exc
        wheel_name = builder.build_editable(output_dir, config_settings=config_settings)
    wheel_path = output_dir / wheel_name
    if not os.path.isfile(os.fspath(wheel_path)):
        raise BuildError(f"Build backend did not create expected wheel: {wheel_name}")
    return wheel_path


def default_wheel_dir() -> Path:
    # Each process and build invocation gets its own directory.  A shared
    # predictable directory lets one process' atexit cleanup delete another
    # process' in-flight wheel.
    path = Path(tempfile.mkdtemp(prefix="pip-build-wheelhouse-"))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def unpack_source(source: Path, destination: Path) -> Path:
    if source.suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            archive.extractall(destination)
    elif source.name.endswith(
        (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tar")
    ):
        with tarfile.open(source) as archive:
            archive.extractall(destination)
    else:
        raise BuildError(f"Unsupported source archive: {source}")
    return single_project_root(destination)


def single_project_root(destination: Path) -> Path:
    with os.scandir(os.fspath(destination)) as entries:
        children = [Path(entry.path) for entry in entries if entry.name != "__MACOSX"]
    if len(children) == 1 and os.path.isdir(os.fspath(children[0])):
        return children[0]
    project = destination / "project"
    os.mkdir(os.fspath(project))
    for child in children:
        shutil.move(str(child), project / child.name)
    return project
