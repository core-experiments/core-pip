from __future__ import annotations

import atexit
import os
import shutil
import tarfile
import tempfile
import zipfile

from cpip.core.errors import BuildError

from .build_backend import ProjectBuilder


def build_wheel_from_source(
    source: str,
    wheel_dir: str | None = None,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> str:
    source_text = os.fspath(source)
    output_text = os.fspath(wheel_dir) if wheel_dir is not None else default_wheel_dir_internal()
    os.makedirs(output_text, exist_ok=True)
    source_is_dir = os.path.isdir(source_text)
    with tempfile.TemporaryDirectory(prefix="pip-build-") as temp_dir:
        project = source_text if source_is_dir else unpack_source_internal(source_text, temp_dir)
        if source_is_dir:
            os.makedirs(os.path.join(project, "build"), exist_ok=True)
        wheel_name = ProjectBuilder(
            project,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        ).build_wheel(output_text, config_settings=config_settings)
    wheel_path = os.path.join(output_text, wheel_name)
    if not os.path.isfile(wheel_path):
        raise BuildError(f"Build backend did not create expected wheel: {wheel_name}")
    return wheel_path


def build_editable_from_source(
    source: str,
    wheel_dir: str | None = None,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> str:
    source_text = os.fspath(source)
    output_text = os.fspath(wheel_dir) if wheel_dir is not None else default_wheel_dir_internal()
    os.makedirs(output_text, exist_ok=True)
    source_is_dir = os.path.isdir(source_text)
    with tempfile.TemporaryDirectory(prefix="pip-build-editable-") as temp_dir:
        project = source_text if source_is_dir else unpack_source_internal(source_text, temp_dir)
        builder = ProjectBuilder(
            project,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        )
        try:
            editable_metadata = not (
                builder.backend_spec is not None
                and builder.backend_spec.name.startswith("setuptools.build_meta")
                and builder.backend_spec.setup_py_present
            )
            builder.prepare_metadata(editable=editable_metadata)
        except BuildError as exc:
            # Let build_editable translate a missing PEP 660 hook into the
            # standard actionable error. Other metadata failures remain fatal.
            if "build_editable" not in str(exc):
                raise
            if os.path.isfile(os.path.join(source_text, "setup.py")) and os.path.isfile(
                os.path.join(source_text, "pyproject.toml"),
            ):
                return build_wheel_from_source(
                    source_text,
                    wheel_dir=output_text,
                    config_settings=config_settings,
                    build_constraints=build_constraints,
                    build_isolation=build_isolation,
                )
            raise BuildError(
                f"Build backend for {source_text} is missing the 'build_editable' hook",
            ) from exc
        wheel_name = builder.build_editable(output_text, config_settings=config_settings)
    wheel_path = os.path.join(output_text, wheel_name)
    if not os.path.isfile(wheel_path):
        raise BuildError(f"Build backend did not create expected wheel: {wheel_name}")
    return wheel_path


def default_wheel_dir() -> str:
    # Each process and build invocation gets its own directory.  A shared
    # predictable directory lets one process' atexit cleanup delete another
    # process' in-flight wheel.
    return default_wheel_dir_internal()


def default_wheel_dir_internal() -> str:
    path = tempfile.mkdtemp(prefix="pip-build-wheelhouse-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def unpack_source(source: str, destination: str) -> str:
    return unpack_source_internal(source, destination)


def unpack_source_internal(source: str, destination: str) -> str:
    source_text = os.fspath(source)
    destination_text = os.fspath(destination)
    if source_text.endswith(".zip"):
        with zipfile.ZipFile(source_text) as archive:
            archive.extractall(destination_text)
    elif os.path.basename(source_text).endswith(
        (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tar"),
    ):
        with tarfile.open(source_text) as archive:
            archive.extractall(destination_text)
    elif zipfile.is_zipfile(source_text):
        with zipfile.ZipFile(source_text) as archive:
            archive.extractall(destination_text)
    elif tarfile.is_tarfile(source_text):
        with tarfile.open(source_text) as archive:
            archive.extractall(destination_text)
    else:
        raise BuildError(f"Unsupported source archive: {source}")
    return single_project_root_internal(destination)


def single_project_root_internal(destination: str) -> str:
    destination_text = os.fspath(destination)
    with os.scandir(destination_text) as entries:
        children = [entry.path for entry in entries if entry.name != "__MACOSX"]
    if len(children) == 1 and os.path.isdir(children[0]):
        return children[0]
    project = os.path.join(destination_text, "project")
    os.mkdir(project)
    for child in children:
        shutil.move(child, os.path.join(project, os.path.basename(child)))
    return project
