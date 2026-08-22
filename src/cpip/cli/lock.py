"""Implementation of the ``cpip lock`` command."""

from __future__ import annotations

import os
import tempfile

from cpip.build.build import unpack_source
from cpip.cli.fast import read_requirements
from cpip.cli.lock_format import LOCK_HEADER, toml_string, write_lock_output
from cpip.cli.parsers.lock import create_parser
from cpip.core.appdirs import configured_cache_dir
from cpip.core.errors import CommandError
from cpip.core.format_control import FormatControl
from cpip.core.packaging import parse_requirement
from cpip.core.temp_dir import remove_temp_directory
from cpip.core.urls import path_to_url, url_to_path
from cpip.index.artifacts import ArtifactLocator
from cpip.index.provider import CandidateProvider
from cpip.index.vcs import git_revision, materialize_vcs, vcs_reference
from cpip.core.appdirs import http_cache_path
from cpip.network.http import NetworkSession
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.files import parse_requirements
from cpip.resolution.input_requirements import install_req_from_line

TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.resolution.req_install import InstallRequirement


def read_requirement_lines(filename: str) -> list[str]:
    """Read requirement lines, raising ``CommandError`` if the file is unreadable."""
    values = read_requirements(filename)

    if values is None:
        raise CommandError(f"Could not read requirement file: {filename}")

    return values


def render_lock(packages: list[dict[str, object]]) -> str:
    lines = list(LOCK_HEADER)

    for package in packages:
        lines.append("[[packages]]")

        lines.append(f"name = {toml_string(str(package['name']))}")

        if "version" in package:
            lines.append(f"version = {toml_string(str(package['version']))}")

        if "vcs" in package:
            vcs = package["vcs"]

            assert isinstance(vcs, dict)

            lines.append("[packages.vcs]")

            lines.append(f"type = {toml_string(str(vcs['type']))}")  # ty:ignore[invalid-argument-type]

            lines.append(f"url = {toml_string(str(vcs['url']))}")  # ty:ignore[invalid-argument-type]

            lines.append(
                f"requested-revision = {toml_string(str(vcs['requested-revision']))}",  # ty:ignore[invalid-argument-type]
            )

            lines.append(f"commit-id = {toml_string(str(vcs['commit-id']))}")  # ty:ignore[invalid-argument-type]

        if "archive" in package:
            archive = package["archive"]

            assert isinstance(archive, dict)

            hashes = archive["hashes"]  # ty:ignore[invalid-argument-type]

            assert isinstance(hashes, dict)

            lines.append("[packages.archive]")

            lines.append(f"url = {toml_string(str(archive['url']))}")  # ty:ignore[invalid-argument-type]

            lines.append("[packages.archive.hashes]")

            lines.append(f"sha256 = {toml_string(str(hashes['sha256']))}")  # ty:ignore[invalid-argument-type]

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

                header = (
                    f"[[packages.{artifact_key}]]"
                    if artifact_key == "wheels"
                    else f"[packages.{artifact_key}]"
                )

                lines.append(header)

                lines.append(f"name = {toml_string(str(entry['name']))}")  # ty:ignore[invalid-argument-type]

                lines.append(f"url = {toml_string(str(entry['url']))}")  # ty:ignore[invalid-argument-type]

                hashes = entry["hashes"]  # ty:ignore[invalid-argument-type]

                assert isinstance(hashes, dict)

                lines.append(f"[packages.{artifact_key}.hashes]")

                lines.append(f"sha256 = {toml_string(str(hashes['sha256']))}")  # ty:ignore[invalid-argument-type]

        lines.append("")

    return "\n".join(lines)


def run_lock(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    cache_dir = configured_cache_dir()

    resolution_session = NetworkSession(
        cache=(http_cache_path(cache_dir) if cache_dir else None),
    )

    artifact_locator = ArtifactLocator(resolution_session, cache_dir=cache_dir)

    quiet_environment = os.environ.get("CPIP_QUIET")

    if options.quiet:
        os.environ["CPIP_QUIET"] = "1"

    format_control = None

    if options.no_binary:
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
            # Only local-directory, editable, and non-VCS archive-URL
            # requirements reach build_backend; plain name==version
            # specifiers never need it.
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

        parsed = parse_requirement(value)

        if parsed.url is None or parsed.name != parsed.url:
            requirements.append(value)

            continue

        item = install_req_from_line(value)

        if item.link is not None and not item.link.is_vcs:
            import hashlib

            from cpip.build.build_backend import prepare_project_metadata

            source = artifact_locator.ensure_local(item.link.url)

            if os.path.isdir(source):
                metadata = prepare_project_metadata(source, build_isolation=False)

                directory_packages.append(
                    {"name": metadata.name, "directory": {"path": "."}},
                )

                continue

            import shutil

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
            requirements.extend(read_requirement_lines(filename))

    constraints = [
        requirement
        for filename in options.constraints
        for requirement in read_requirement_lines(filename)
    ]

    if not requirements and not archive_packages and not directory_packages:
        raise CommandError("You must give at least one requirement")

    plan = None

    string_requirements = [item for item in requirements if isinstance(item, str)]

    if (
        len(string_requirements) == len(requirements)
        and string_requirements
        and options.no_index
        and not options.no_binary
    ):
        plan = ResolutionEngine.resolve_wheelhouse(
            options.find_links,
            string_requirements,
            constraints=constraints,
        )

    if plan is None and requirements:
        provider = CandidateProvider.from_options(
            find_links=options.find_links,
            no_index=options.no_index,
            format_control=format_control,
            build_isolation=not options.no_build_isolation,
            wheel_cache_dir=cache_dir,
            session=resolution_session,
            dry_run=True,
        )

        install_requirements = [
            item if not isinstance(item, str) else install_req_from_line(item)
            for item in requirements
        ]

        plan = ResolutionEngine(
            provider=provider,
            no_deps=False,
            ignore_installed=True,
            constraints=constraints,
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
            candidate_path = candidate.path

        if candidate_path is not None:
            source_path = candidate_path

        elif source.startswith("file:"):
            source_path = url_to_path(source)

        else:
            source_path = None

        if candidate.source_vcs:
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
                archive_digest = (candidate.source_hashes or {}).get("sha256")
                archive_path = artifact_locator.ensure_local(source)

                if archive_digest is None:
                    import hashlib

                    with open(archive_path, "rb") as file:
                        archive_digest = hashlib.sha256(file.read()).hexdigest()

                package_name = candidate.name
                with tempfile.TemporaryDirectory(prefix="cpip-lock-") as temp_dir:
                    from cpip.build.build_backend import prepare_project_metadata

                    try:
                        project = prepare_project_metadata(
                            unpack_source(archive_path, temp_dir),
                            build_isolation=False,
                        )
                    except Exception:
                        pass
                    else:
                        package_name = project.name

                packages.append(
                    {
                        "name": package_name,
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

    write_lock_output(options.output, rendered)

    if quiet_environment is None:
        os.environ.pop("CPIP_QUIET", None)

    else:
        os.environ["CPIP_QUIET"] = quiet_environment

    return 0
