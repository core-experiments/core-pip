"""Implementation of the ``cpip lock`` command."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from cpip.cli.parser import ArgumentParser
    from cpip.resolution.req_install import InstallRequirement


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_lock(packages: list[dict[str, object]]) -> str:
    lines = ['created-by = "cpip"', 'lock-version = "1.0"', ""]
    for package in packages:
        lines.append("[[packages]]")
        lines.append(f"name = {toml_string(str(package['name']))}")
        if "version" in package:
            lines.append(f"version = {toml_string(str(package['version']))}")
        if "vcs" in package:
            vcs = package["vcs"]
            assert isinstance(vcs, dict)
            vcs = cast("dict[str, object]", vcs)
            lines.append("[packages.vcs]")
            lines.append(f"type = {toml_string(str(vcs['type']))}")
            lines.append(f"url = {toml_string(str(vcs['url']))}")
            lines.append(
                f"requested-revision = {toml_string(str(vcs['requested-revision']))}",
            )
            lines.append(f"commit-id = {toml_string(str(vcs['commit-id']))}")
        if "archive" in package:
            archive = package["archive"]
            assert isinstance(archive, dict)
            archive = cast("dict[str, object]", archive)
            hashes = archive["hashes"]
            assert isinstance(hashes, dict)
            hashes = cast("dict[str, object]", hashes)
            lines.append("[packages.archive]")
            lines.append(f"url = {toml_string(str(archive['url']))}")
            lines.append("[packages.archive.hashes]")
            lines.append(f"sha256 = {toml_string(str(hashes['sha256']))}")
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
                assert isinstance(entry, dict)
                entry = cast("dict[str, object]", entry)
                header = (
                    f"[[packages.{artifact_key}]]"
                    if artifact_key == "wheels"
                    else f"[packages.{artifact_key}]"
                )
                lines.append(header)
                lines.append(f"name = {toml_string(str(entry['name']))}")
                lines.append(f"url = {toml_string(str(entry['url']))}")
                hashes = entry["hashes"]
                assert isinstance(hashes, dict)
                hashes = cast("dict[str, object]", hashes)
                lines.append(f"[packages.{artifact_key}.hashes]")
                lines.append(f"sha256 = {toml_string(str(hashes['sha256']))}")
        lines.append("")
    return "\n".join(lines)


def create_parser() -> ArgumentParser:
    """Resolve requirements and write a PEP 751 ``pylock.toml`` file."""
    from cpip.cli.parser import ArgumentParser

    parser = ArgumentParser(prog="cpip lock", allow_abbrev=False)
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
    options = create_parser().parse_args(args)
    from cpip.index.artifacts import ArtifactLocator
    from cpip.network.http import NetworkSession

    resolution_session = NetworkSession()
    artifact_locator = ArtifactLocator(resolution_session)
    quiet_environment = os.environ.get("CPIP_QUIET")
    if options.quiet:
        os.environ["CPIP_QUIET"] = "1"

    format_control = None
    if options.no_binary:
        from cpip.core.format_control import FormatControl

        format_control = FormatControl()
        for value in options.no_binary:
            format_control.apply("no-binary", value)
    requirements: list[str | InstallRequirement] = []
    locked_order: list[str] = []
    archive_packages: list[dict] = []
    directory_packages: list[dict] = []
    for value in options.requirements:
        local_directory = os.path.abspath(value)
        if os.path.isdir(local_directory):
            from cpip.build.build_backend import prepare_project_metadata

            metadata = prepare_project_metadata(
                local_directory,
                build_isolation=False,
            )
            directory_packages.append(
                {"name": metadata.name, "directory": {"path": "."}},
            )
            continue
        if "://" not in value and not value.startswith(("git+", "hg+", "svn+", "bzr+")):
            requirements.append(value)
            continue
        from cpip.core.packaging import parse_requirement

        parsed = parse_requirement(value)
        if parsed.url is None or parsed.name != parsed.url:
            requirements.append(value)
            continue
        from cpip.resolution.engine.input.requirements import install_req_from_line

        item = install_req_from_line(value)
        if item.link is not None and not item.link.is_vcs:
            import hashlib

            from cpip.build.build import unpack_source
            from cpip.build.build_backend import prepare_project_metadata

            source = artifact_locator.ensure_local(item.link.url)
            if os.path.isdir(source):
                metadata = prepare_project_metadata(source, build_isolation=False)
                directory_packages.append(
                    {"name": metadata.name, "directory": {"path": "."}},
                )
                continue
            import shutil
            import tempfile

            with tempfile.TemporaryDirectory(prefix="cpip-lock-source-") as directory:
                archive = os.path.join(directory, "source.tar.gz")
                shutil.copyfile(source, archive)
                project = unpack_source(archive, os.path.join(directory, "project"))
                metadata = prepare_project_metadata(project, build_isolation=False)
            with open(source, "rb") as file:
                source_digest = hashlib.sha256(file.read()).hexdigest()
            archive_packages.append(
                {
                    "name": metadata.name,
                    "archive": {
                        "url": value,
                        "hashes": {
                            "sha256": source_digest,
                        },
                    },
                },
            )
            continue
        requirements.append(value)
    editable_packages: list[dict] = []
    for value in options.editable:
        from cpip.build.build_backend import prepare_project_metadata
        from cpip.resolution.engine.input.requirements import install_req_from_line

        item = install_req_from_line(value)
        item.editable = True
        requirements.append(item)
        editable_path = os.path.realpath(value)
        metadata = prepare_project_metadata(editable_path)
        editable_packages.append(
            {
                "name": metadata.name,
                "directory": {"editable": True, "path": "."},
            },
        )
    for filename in options.requirement:
        if os.path.basename(filename).startswith("pylock") and filename.endswith(
            ".toml",
        ):
            from cpip.core.urls import url_to_path
            from cpip.resolution.engine.input.files import parse_requirements
            from cpip.resolution.engine.input.requirements import install_req_from_line

            for item in parse_requirements(filename, resolution_session):
                if item.locked_name is not None:
                    locked_order.append(item.locked_name)
                if (
                    item.locked_direct
                    and item.locked_name is not None
                    and item.locked_link is not None
                    and not item.locked_link.startswith(("git+", "hg+", "svn+", "bzr+"))
                    and not (
                        item.locked_link.startswith("file:")
                        and os.path.isdir(url_to_path(item.locked_link))
                    )
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
                        },
                    )
                    continue
                requirements.append(
                    install_req_from_line(
                        f"{item.locked_name} @ {item.locked_link}"
                        if item.locked_name is not None and item.locked_link is not None
                        else item.requirement,
                    ),
                )
        else:
            with open(filename, encoding="utf-8") as requirement_file:
                requirements.extend(
                    line.strip()
                    for line in requirement_file.read().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
    if not requirements and not archive_packages and not directory_packages:
        from cpip.core.errors import CommandError

        raise CommandError("You must give at least one requirement")

    plan = None
    string_requirements = [item for item in requirements if isinstance(item, str)]
    if (
        len(string_requirements) == len(requirements)
        and string_requirements
        and options.no_index
        and not options.no_binary
    ):
        from cpip.resolution.engine import ResolutionEngine

        plan = ResolutionEngine.resolve_wheelhouse(
            options.find_links,
            string_requirements,
        )
    if plan is None and requirements:
        from cpip.index.provider import CandidateProvider

        provider = CandidateProvider.from_options(
            find_links=options.find_links,
            no_index=options.no_index,
            format_control=format_control,
            build_isolation=not options.no_build_isolation,
            session=resolution_session,
        )
        from cpip.resolution.engine import ResolutionEngine
        from cpip.resolution.engine.input.requirements import install_req_from_line

        install_requirements = [
            item if not isinstance(item, str) else install_req_from_line(item)
            for item in requirements
        ]
        plan = ResolutionEngine(
            provider=provider,
            no_deps=False,
            ignore_installed=True,
        ).resolve(install_requirements)
    packages: list[dict] = [
        *editable_packages,
        *directory_packages,
        *archive_packages,
    ]
    editable_names = {str(package["name"]) for package in editable_packages}
    for candidate in plan.candidates if plan is not None else []:
        source = candidate.source_url
        if source is None:
            continue
        candidate_path = None
        if candidate.source_kind == "wheel":
            from cpip.resolution.engine.sources.wheelhouse.models import (
                LocalWheelCandidate,
            )

            if isinstance(candidate, LocalWheelCandidate):
                candidate_path = candidate.path
        if candidate_path is not None:
            source_path = candidate_path
        elif source.startswith("file:"):
            from cpip.core.urls import url_to_path

            source_path = url_to_path(source)
        else:
            source_path = None
        if candidate.source_vcs:
            from cpip.core.temp_dir import remove_temp_directory
            from cpip.index.vcs import git_revision, materialize_vcs, vcs_reference

            reference = vcs_reference(source)
            commit_id = getattr(candidate, "source_vcs_revision", None)
            if commit_id is None:
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
                },
            )
            continue
        if candidate.source_kind == "source-tree" and candidate.name in editable_names:
            continue
        if candidate.source_kind == "source-tree":
            packages.append({"name": candidate.name, "directory": {"path": "."}})
            continue
        if source_path is None:
            if source.startswith(("http://", "https://")):
                import hashlib

                archive_path = artifact_locator.ensure_local(source)
                with open(archive_path, "rb") as file:
                    archive_digest = hashlib.sha256(file.read()).hexdigest()
                packages.append(
                    {
                        "name": candidate.name,
                        "archive": {
                            "url": source,
                            "hashes": {
                                "sha256": archive_digest,
                            },
                        },
                    },
                )
            continue
        digest = (candidate.source_hashes or {}).get("sha256")
        if digest is None:
            import hashlib

            with open(source_path, "rb") as source_file:
                digest = hashlib.sha256(source_file.read()).hexdigest()
        if candidate_path is not None:
            artifact_url = source
        else:
            from cpip.core.urls import path_to_url

            artifact_url = path_to_url(str(source_path))
        artifact = {
            "name": os.path.basename(source_path),
            "url": artifact_url,
            "hashes": {"sha256": digest},
        }
        key = "sdist" if candidate.source_kind == "sdist" else "wheels"
        value: object = [artifact] if key == "wheels" else artifact
        packages.append(
            {"name": candidate.name, "version": str(candidate.version), key: value},
        )
    if locked_order:
        order = {name: index for index, name in enumerate(locked_order)}
        packages.sort(key=lambda package: order.get(str(package["name"]), len(order)))

    rendered = render_lock(packages)
    if options.output == "-":
        print(rendered, end="")
    else:
        with open(options.output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered)
    if quiet_environment is None:
        os.environ.pop("CPIP_QUIET", None)
    else:
        os.environ["CPIP_QUIET"] = quiet_environment
    return 0
