"""Implementation of the ``pip lock`` command."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pip.core.errors import CommandError
from pip.core.format_control import FormatControl
from pip.core.temp_dir import remove_temp_directory
from pip.core.urls import path_to_url, url_to_path


def create_parser() -> argparse.ArgumentParser:
    """Resolve requirements and write a PEP 751 ``pylock.toml`` file."""

    parser = argparse.ArgumentParser(prog="pip lock", allow_abbrev=False)
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("-e", "--editable", action="append", default=[])
    parser.add_argument("-r", "--requirement", action="append", default=[])
    parser.add_argument("-f", "--find-links", action="append", default=[])
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--no-binary", action="append", default=[])
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--output", default="pylock.toml")
    return parser


def run_lock(args: list[str]) -> int:
    from pip.index.provider import CandidateProvider
    from pip.resolution.req_install import install_req_from_line
    from pip.resolution.resolver import Resolver

    options = create_parser().parse_args(args)
    quiet_environment = os.environ.get("PIP_QUIET")
    if options.quiet:
        os.environ["PIP_QUIET"] = "1"

    format_control = FormatControl()
    for value in options.no_binary:
        format_control.apply("no-binary", value)
    requirements = []
    locked_order: list[str] = []
    archive_packages: list[dict[str, Any]] = []
    directory_packages: list[dict[str, Any]] = []
    for value in options.requirements:
        local_directory = Path(value).resolve()
        if local_directory.is_dir():
            from pip.build.build_backend import prepare_project_metadata

            metadata = prepare_project_metadata(local_directory, build_isolation=False)
            directory_packages.append(
                {"name": metadata.name, "directory": {"path": "."}}
            )
            continue
        item = install_req_from_line(value)
        if (
            item.req is not None
            and item.link is not None
            and item.req.url is not None
            and item.req.name == item.req.url
            and not item.link.is_vcs
        ):
            from pip.build.build import unpack_source
            from pip.build.build_backend import prepare_project_metadata
            from pip.index.artifacts import ArtifactLocator

            source = ArtifactLocator().ensure_local(item.link.url)
            if source.is_dir():
                metadata = prepare_project_metadata(source, build_isolation=False)
                directory_packages.append(
                    {"name": metadata.name, "directory": {"path": "."}}
                )
                continue
            with tempfile.TemporaryDirectory(prefix="pip-lock-source-") as directory:
                archive = Path(directory) / "source.tar.gz"
                shutil.copyfile(source, archive)
                project = unpack_source(archive, Path(directory) / "project")
                metadata = prepare_project_metadata(project, build_isolation=False)
            archive_packages.append(
                {
                    "name": metadata.name,
                    "archive": {
                        "url": value,
                        "hashes": {
                            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()
                        },
                    },
                }
            )
            continue
        requirements.append(item)
    editable_packages: list[dict[str, Any]] = []
    for value in options.editable:
        from pip.build.build_backend import prepare_project_metadata

        item = install_req_from_line(value)
        item.editable = True
        requirements.append(item)
        editable_path = Path(value).resolve()
        metadata = prepare_project_metadata(editable_path)
        editable_packages.append(
            {
                "name": metadata.name,
                "directory": {"editable": True, "path": "."},
            }
        )
    lock_session = None
    for filename in options.requirement:
        if Path(filename).name.startswith("pylock") and filename.endswith(".toml"):
            from pip.network.http import NetworkSession
            from pip.resolution.req_file import parse_requirements

            if lock_session is None:
                lock_session = NetworkSession()
            for item in parse_requirements(filename, lock_session):
                if item.locked_name is not None:
                    locked_order.append(item.locked_name)
                if (
                    item.locked_direct
                    and item.locked_name is not None
                    and item.locked_link is not None
                    and not item.locked_link.startswith(("git+", "hg+", "svn+", "bzr+"))
                    and not Path(item.locked_link.removeprefix("file://")).is_dir()
                ):
                    archive_packages.append(
                        {
                            "name": item.locked_name,
                            "archive": {
                                "url": item.locked_link,
                                "hashes": {
                                    algorithm: values[0]
                                    for algorithm, values in (
                                        item.locked_hashes or {}
                                    ).items()
                                    if values
                                },
                            },
                        }
                    )
                    continue
                requirements.append(
                    install_req_from_line(
                        f"{item.locked_name} @ {item.locked_link}"
                        if item.locked_name is not None and item.locked_link is not None
                        else item.requirement
                    )
                )
        else:
            requirements.extend(
                install_req_from_line(line.strip())
                for line in Path(filename).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
    if not requirements and not archive_packages and not directory_packages:
        raise CommandError("You must give at least one requirement")

    provider = CandidateProvider.from_options(
        find_links=options.find_links,
        no_index=options.no_index,
        format_control=format_control,
        build_isolation=not options.no_build_isolation,
    )
    plan = (
        Resolver(provider=provider, no_deps=False, ignore_installed=True).resolve(
            requirements
        )
        if requirements
        else None
    )
    packages: list[dict[str, Any]] = [
        *editable_packages,
        *directory_packages,
        *archive_packages,
    ]
    editable_names = {str(package["name"]) for package in editable_packages}
    for candidate in plan.candidates if plan is not None else []:
        source = candidate.source_url
        if source is None:
            continue
        source_path = Path(url_to_path(source)) if source.startswith("file:") else None
        if candidate.source_vcs:
            from pip.index.vcs import git_revision, materialize_vcs, vcs_reference

            reference = vcs_reference(source)
            checkout = materialize_vcs(source, emit_resolution=False)
            commit_id = git_revision(checkout)
            remove_temp_directory(checkout)
            packages.append(
                {
                    "name": candidate.name,
                    "vcs": {
                        "type": candidate.source_vcs,
                        "url": reference.repo_url,
                        "requested-revision": reference.requested_revision,
                        "commit-id": commit_id,
                    },
                }
            )
            continue
        if candidate.source_kind == "source-tree" and candidate.name in editable_names:
            continue
        if candidate.source_kind == "source-tree":
            packages.append({"name": candidate.name, "directory": {"path": "."}})
            continue
        if source_path is None:
            if source.startswith(("http://", "https://")):
                from pip.index.artifacts import ArtifactLocator

                archive_path = ArtifactLocator().ensure_local(source)
                packages.append(
                    {
                        "name": candidate.name,
                        "archive": {
                            "url": source,
                            "hashes": {
                                "sha256": hashlib.sha256(
                                    archive_path.read_bytes()
                                ).hexdigest()
                            },
                        },
                    }
                )
            continue
        digest = (candidate.source_hashes or {}).get("sha256")
        if digest is None:
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        artifact = {
            "name": source_path.name,
            "url": path_to_url(str(source_path)),
            "hashes": {"sha256": digest},
        }
        key = "sdist" if candidate.source_kind == "sdist" else "wheels"
        value: object = [artifact] if key == "wheels" else artifact
        packages.append(
            {"name": candidate.name, "version": str(candidate.version), key: value}
        )
    if locked_order:
        order = {name: index for index, name in enumerate(locked_order)}
        packages.sort(key=lambda package: order.get(str(package["name"]), len(order)))

    def toml_string(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ['created-by = "pip"', 'lock-version = "1.0"', ""]
    for package in packages:
        lines.append("[[packages]]")
        lines.append(f"name = {toml_string(str(package['name']))}")
        if "version" in package:
            lines.append(f"version = {toml_string(str(package['version']))}")
        if "vcs" in package:
            vcs = package["vcs"]
            lines.append("[packages.vcs]")
            lines.append(f"type = {toml_string(str(vcs['type']))}")
            lines.append(f"url = {toml_string(str(vcs['url']))}")
            lines.append(
                f"requested-revision = {toml_string(str(vcs['requested-revision']))}"
            )
            lines.append(f"commit-id = {toml_string(str(vcs['commit-id']))}")
        if "archive" in package:
            archive = package["archive"]
            lines.append("[packages.archive]")
            lines.append(f"url = {toml_string(str(archive['url']))}")
            lines.append("[packages.archive.hashes]")
            lines.append(f"sha256 = {toml_string(str(archive['hashes']['sha256']))}")
        if "directory" in package:
            directory = package["directory"]
            lines.append("[packages.directory]")
            if isinstance(directory, dict) and directory.get("editable"):
                lines.append("editable = true")
            lines.append('path = "."')
        for artifact_key in ("sdist", "wheels"):
            artifact = package.get(artifact_key)
            if artifact is None:
                continue
            artifacts = artifact if isinstance(artifact, list) else [artifact]
            for entry in artifacts:
                header = (
                    f"[[packages.{artifact_key}]]"
                    if artifact_key == "wheels"
                    else f"[packages.{artifact_key}]"
                )
                lines.append(header)
                lines.append(f"name = {toml_string(str(entry['name']))}")
                lines.append(f"url = {toml_string(str(entry['url']))}")
                lines.append(f"[packages.{artifact_key}.hashes]")
                lines.append(f"sha256 = {toml_string(str(entry['hashes']['sha256']))}")
        lines.append("")
    rendered = "\n".join(lines)
    if options.output == "-":
        print(rendered, end="")
    else:
        Path(options.output).write_text(rendered, encoding="utf-8")
    if quiet_environment is None:
        os.environ.pop("PIP_QUIET", None)
    else:
        os.environ["PIP_QUIET"] = quiet_environment
    return 0
