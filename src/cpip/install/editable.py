"""Source preparation services for installation."""

from __future__ import annotations

import os
import shutil
import sys
from typing import TYPE_CHECKING

from cpip.core.direct_url import DirectUrl, DirInfo
from cpip.core.errors import BuildError, CommandError
from cpip.core.packaging import SpecifierSet, canonicalize_name
from cpip.core.python import CURRENT_PYTHON_VERSION_FULL
from cpip.core.temp_dir import remove_temp_directory
from cpip.core.urls import path_to_url
from cpip.index.artifacts import ArtifactLocator
from cpip.install.direct_url import direct_url_from_link
from cpip.resolution.engine.input.requirements import install_req_from_editable

if TYPE_CHECKING:
    from cpip.build.build_backend import ProjectMetadata


def prepare_editable_source(
    editable: str,
    *,
    build_isolation: bool = True,
    prepare_metadata: bool = True,
) -> tuple[str, DirectUrl | None, ProjectMetadata | None]:
    """Validate and prepare an editable source for the build service."""
    requirement = install_req_from_editable(editable)
    link = requirement.link
    if link is None or not (link.is_vcs or link.is_existing_dir or link.is_file):
        raise CommandError(f"{editable} is not a valid editable requirement")

    source_path = ArtifactLocator().ensure_local(link.url)
    if link.url.startswith("file:"):
        direct_url = DirectUrl(url=link.url, dir_info=DirInfo(editable=True))
    elif link.is_vcs:
        direct_url = direct_url_from_link(link)
    else:
        direct_url = None
    if link.is_vcs:
        checkout_name = canonicalize_name(
            link.egg_fragment or os.path.basename(source_path),
        )
        checkout_dir = os.path.join(sys.prefix, "src", checkout_name)
        try:
            shutil.rmtree(checkout_dir)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(checkout_dir), exist_ok=True)
        materialized_source = source_path
        shutil.copytree(materialized_source, checkout_dir, symlinks=True)
        remove_temp_directory(materialized_source)
        source_path = checkout_dir
        direct_url = DirectUrl(
            url=path_to_url(checkout_dir),
            dir_info=DirInfo(editable=True),
        )

    if link.subdirectory_fragment:
        source_path = os.path.join(source_path, link.subdirectory_fragment)

    project_files: set[str] = set()
    try:
        with os.scandir(source_path) as entries:
            for entry in entries:
                if entry.name in {"setup.py", "pyproject.toml"} and entry.is_file():
                    project_files.add(entry.name)
    except OSError:
        raise CommandError(f"{source_path} is not a valid editable requirement")
    if not project_files:
        raise CommandError(
            f"{source_path} does not appear to be a Python project: "
            "neither 'setup.py' nor 'pyproject.toml' found",
        )

    if prepare_metadata:
        from cpip.build.build_backend import BackendSpec, prepare_project_metadata

        try:
            metadata = prepare_project_metadata(
                source_path,
                editable=True,
                build_isolation=build_isolation,
            )
        except BuildError as exc:
            if "build_editable" in str(exc):
                backend_spec = BackendSpec.from_project(source_path)
                if (
                    backend_spec is not None
                    and backend_spec.name.startswith("setuptools.build_meta")
                    and "setup.py" in project_files
                    and "pyproject.toml" in project_files
                ):
                    metadata = None
                else:
                    raise BuildError(
                        f"Build backend for {source_path} is missing the "
                        "'build_editable' hook",
                    ) from exc
            if not build_isolation and (
                "Cannot import 'setuptools.build_meta'" in str(exc)
                or "pyproject.toml" in project_files
            ):
                metadata = prepare_project_metadata(
                    source_path,
                    editable=True,
                    build_isolation=True,
                )
            else:
                metadata = None
    else:
        metadata = None
    egg = link.egg_fragment
    if (
        metadata is not None
        and egg is not None
        and canonicalize_name(egg) != canonicalize_name(metadata.name)
    ):
        print(f"{editable} has inconsistent name: expected {egg}, got {metadata.name}")
        raise CommandError(
            "Generating metadata for package "
            f"{egg} produced metadata for project name {metadata.name}. "
            f"Fix your #egg={egg} fragments.",
        )
    if metadata is not None and metadata.requires_python is not None:
        python_version = CURRENT_PYTHON_VERSION_FULL
        if not SpecifierSet(metadata.requires_python).contains(python_version):
            raise CommandError(
                f"Package '{metadata.name}' requires a different Python: "
                f"{python_version} not in '{metadata.requires_python}'",
            )
    return source_path, direct_url, metadata
