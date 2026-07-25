"""Source preparation services for installation."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pip.build.build_backend import ProjectMetadata, prepare_project_metadata
from pip.core.direct_url import DirectUrl, DirInfo
from pip.core.errors import BuildError, CommandError
from pip.core.packaging import SpecifierSet, canonicalize_name
from pip.index.artifacts import ArtifactLocator
from pip.resolution.direct_url_helpers import direct_url_from_link
from pip.resolution.req_install import install_req_from_editable


def prepare_editable_source(
    editable: str,
    *,
    build_isolation: bool = True,
    prepare_metadata: bool = True,
) -> tuple[Path, DirectUrl | None, ProjectMetadata | None]:
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
        checkout_name = canonicalize_name(link.egg_fragment or source_path.name)
        checkout_dir = Path(sys.prefix) / "src" / checkout_name
        if checkout_dir.exists():
            shutil.rmtree(checkout_dir)
        checkout_dir.parent.mkdir(parents=True, exist_ok=True)
        materialized_source = source_path
        shutil.copytree(materialized_source, checkout_dir, symlinks=True)
        shutil.rmtree(materialized_source, ignore_errors=True)
        source_path = checkout_dir
        direct_url = DirectUrl(
            url=checkout_dir.as_uri(), dir_info=DirInfo(editable=True)
        )

    if link.subdirectory_fragment:
        source_path = source_path / link.subdirectory_fragment

    if not source_path.is_dir():
        raise CommandError(f"{source_path} is not a valid editable requirement")
    if (
        not (source_path / "setup.py").is_file()
        and not (source_path / "pyproject.toml").is_file()
    ):
        raise CommandError(
            f"{source_path} does not appear to be a Python project: "
            "neither 'setup.py' nor 'pyproject.toml' found"
        )

    if prepare_metadata:
        try:
            metadata = prepare_project_metadata(
                source_path, editable=True, build_isolation=build_isolation
            )
        except BuildError as exc:
            if "build_editable" in str(exc):
                raise BuildError(
                    f"Build backend for {source_path} is missing the "
                    "'build_editable' hook"
                ) from exc
            if not build_isolation and (
                "Cannot import 'setuptools.build_meta'" in str(exc)
                or (source_path / "pyproject.toml").is_file()
            ):
                metadata = prepare_project_metadata(
                    source_path, editable=True, build_isolation=True
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
            f"Fix your #egg={egg} fragments."
        )
    if metadata is not None and metadata.requires_python is not None:
        python_version = ".".join(str(part) for part in sys.version_info[:3])
        if not SpecifierSet(metadata.requires_python).contains(python_version):
            raise CommandError(
                f"Package '{metadata.name}' requires a different Python: "
                f"{python_version} not in '{metadata.requires_python}'"
            )
    return source_path, direct_url, metadata
