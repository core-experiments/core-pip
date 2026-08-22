"""The ``cpip freeze`` command and the requirement rendering it uses."""

from __future__ import annotations

import collections
import logging
import os
import re
import site
import sys
from collections.abc import Generator, Iterable

from cpip.cli.parsers.freeze import create_parser
from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES
from cpip.core.errors import InstallationError
from cpip.core.packaging import canonicalize_name
from cpip.core.versions import InvalidVersion

TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.core.light_metadata import LightDistribution

logger = logging.getLogger(__name__)

VALID_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


if TYPE_CHECKING:
    from typing import NamedTuple

    class EditableInfo(NamedTuple):
        requirement: str

        comments: list[str]

else:
    EditableInfo = collections.namedtuple("EditableInfo", ["requirement", "comments"])


def freeze(
    requirement: list[str] | None = None,
    local_only: bool = False,
    user_only: bool = False,
    paths: list[str] | None = None,
    exclude_editable: bool = False,
    exclude: Iterable[str] = (),
    skip: Iterable[str] = (),
) -> Generator[str, None, None]:
    from cpip.core.light_metadata import LightDistributionStore

    installations: dict[str, FrozenRequirement] = {}

    excluded = {canonicalize_name(name) for name in exclude}

    dists = LightDistributionStore(
        paths=paths,
        user_site=site.getusersitepackages(),
    ).iter(local_only=local_only, user_only=user_only)

    for dist in dists:
        if VALID_NAME.fullmatch(dist.raw_name) is None:
            logger.warning(
                "Ignoring invalid distribution %s (%s)",
                dist.canonical_name,
                dist.raw_name,
            )

            continue

        req = FrozenRequirement.from_dist(dist)

        if req.canonical_name in excluded or (exclude_editable and req.editable):
            continue

        installations[req.canonical_name] = req

    if requirement:
        # Only `-r`/`--requirement` needs requirement-file parsing, so a plain
        # `cpip freeze` never pays for the resolution package.
        from cpip.resolution.files.parser import COMMENT_RE
        from cpip.resolution.input_requirements import (
            install_req_from_editable,
            install_req_from_line,
        )

        # the options that don't get turned into an InstallRequirement

        # should only be emitted once, even if the same option is in multiple

        # requirements files, so we need to keep track of what has been emitted

        # so that we don't emit it again if it's seen again

        emitted_options: set[str] = set()

        # keep track of which files a requirement is in so that we can

        # give an accurate warning if a requirement appears multiple times.

        req_files: dict[str, list[str]] = collections.defaultdict(list)

        for req_file_path in requirement:
            with open(req_file_path) as req_file:
                for line in req_file:
                    if (
                        not line.strip()
                        or line.strip().startswith("#")
                        or line.startswith(
                            (
                                "-r",
                                "--requirement",
                                "-f",
                                "--find-links",
                                "-i",
                                "--index-url",
                                "--pre",
                                "--trusted-host",
                                "--process-dependency-links",
                                "--extra-index-url",
                                "--use-feature",
                            ),
                        )
                    ):
                        line = line.rstrip()

                        if line not in emitted_options:
                            emitted_options.add(line)

                            yield line + "\n"

                        continue

                    if line.startswith(("-e", "--editable")):
                        if line.startswith("-e"):
                            line = line[2:].strip()

                        else:
                            line = line[len("--editable") :].strip().lstrip("=")

                        line_req = install_req_from_editable(
                            line,
                        )

                    else:
                        line_req = install_req_from_line(
                            COMMENT_RE.sub("", line).strip(),
                        )

                    if not line_req.name:
                        logger.info(
                            "Skipping line in requirement file [%s] because "
                            "it's not clear what it would install: %s",
                            req_file_path,
                            line.strip(),
                        )

                        logger.info(
                            "  (add #egg=PackageName to the URL to avoid this warning)",
                        )

                    else:
                        line_req_canonical_name = canonicalize_name(line_req.name)

                        if line_req_canonical_name not in installations:
                            # either it's not installed, or it is installed

                            # but has been processed already

                            if not req_files[line_req.name]:
                                logger.warning(
                                    "Requirement file [%s] contains %s, but "
                                    "package %r is not installed",
                                    req_file_path,
                                    COMMENT_RE.sub("", line).strip(),
                                    line_req.name,
                                )

                            else:
                                req_files[line_req.name].append(req_file_path)

                        else:
                            frozen = installations[line_req_canonical_name]

                            if not frozen.editable and line_req.name:
                                output_name = (
                                    "INITools"
                                    if frozen.canonical_name == "initools"
                                    else frozen.canonical_name
                                )

                                frozen = frozen.copy_with(
                                    req=re.sub(
                                        r"^[A-Za-z0-9][A-Za-z0-9._-]*",
                                        output_name,
                                        frozen.req,
                                        count=1,
                                    ),
                                )

                            yield str(frozen).rstrip() + "\n"

                            del installations[line_req_canonical_name]

                            req_files[line_req.name].append(req_file_path)

        # Warn about requirements that were included multiple times (in a

        # single requirements file or in different requirements files).

        for name, files in req_files.items():
            if len(files) > 1:
                logger.warning(
                    "Requirement %s included multiple times [%s]",
                    name,
                    ", ".join(sorted(set(files))),
                )

        yield "## The following requirements were added by cpip freeze:\n"

    for installation in sorted(installations.values(), key=lambda x: x.name.lower()):
        if installation.canonical_name not in skip:
            yield str(installation).rstrip() + "\n"


def format_as_name_version(dist: LightDistribution) -> str:
    try:
        dist_version = dist.version

    except InvalidVersion:
        # legacy version

        return f"{dist.raw_name}==={dist.raw_version}"

    else:
        return f"{dist.raw_name}=={dist_version}"


def get_editable_info(dist: LightDistribution) -> EditableInfo:
    """Compute and return values (req, comments) for use in

    FrozenRequirement.from_dist().

    """
    # Only editable installs reach this, and it's the only place freeze
    # needs VCS backends -- keep them off every other `cpip freeze` run.
    from cpip.vcs.errors import BadCommand
    from cpip.vcs.versioncontrol import RemoteNotFoundError, RemoteNotValidError, vcs

    editable_project_location = dist.editable_project_location

    assert editable_project_location

    location = os.path.normcase(os.path.abspath(editable_project_location))

    vcs_backend = vcs.get_backend_for_dir(location)

    if vcs_backend is None:
        display = format_as_name_version(dist)

        logger.debug(
            'No VCS found for editable requirement "%s" in: %r',
            display,
            location,
        )

        return EditableInfo(
            requirement=location,
            comments=[f"# Editable install with no version control ({display})"],
        )

    vcs_name = type(vcs_backend).__name__

    try:
        req = vcs_backend.get_src_requirement(location, dist.raw_name)

    except RemoteNotFoundError:
        display = format_as_name_version(dist)

        return EditableInfo(
            requirement=location,
            comments=[f"# Editable {vcs_name} install with no remote ({display})"],
        )

    except RemoteNotValidError as ex:
        display = format_as_name_version(dist)

        return EditableInfo(
            requirement=location,
            comments=[
                f"# Editable {vcs_name} install ({display}) with either a deleted "
                f"local remote or invalid URI:",
                f"# '{ex.url}'",
            ],
        )

    except BadCommand:
        logger.warning(
            "cannot determine version of editable source in %s (%s command not found in path)",
            location,
            vcs_backend.name,
        )

        return EditableInfo(requirement=location, comments=[])

    except InstallationError as exc:
        logger.warning("Error when trying to get requirement for VCS system %s", exc)

    else:
        return EditableInfo(requirement=req, comments=[])

    logger.warning("Could not determine repository location of %s", location)

    return EditableInfo(
        requirement=location,
        comments=["## !! Could not determine repository location"],
    )


class FrozenRequirement:
    __slots__ = ("comments", "editable", "name", "req")

    def __init__(
        self,
        name: str,
        req: str,
        editable: bool,
        comments: Iterable[str] = (),
    ) -> None:
        self.name = name

        self.req = req

        self.editable = editable

        self.comments = comments

    def copy_with(self, **changes: object) -> FrozenRequirement:
        values = {
            "name": self.name,
            "req": self.req,
            "editable": self.editable,
            "comments": self.comments,
        }

        values.update(changes)

        return type(self)(**values)

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    @classmethod
    def from_dist(cls, dist: LightDistribution) -> FrozenRequirement:
        editable = dist.editable

        if editable:
            req, comments = get_editable_info(dist)

        else:
            comments = []

            direct_url = dist.direct_url

            if direct_url:
                # if PEP 610 metadata is present, use it

                req = direct_url.as_pep440_direct_reference(dist.raw_name)

            else:
                # name==version requirement

                req = format_as_name_version(dist)

        return cls(dist.raw_name, req, editable, comments=comments)

    def __str__(self) -> str:
        req = self.req

        if self.editable:
            req = f"-e {req}"

        return "\n".join(list(self.comments) + [str(req)]) + "\n"


def run_freeze(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    from cpip.core.light_metadata import stdlib_pkgs

    excluded = {canonicalize_name(name) for name in options.exclude}

    if "cpip" in excluded:
        excluded.update(canonicalize_name(name) for name in CPIP_DISTRIBUTION_NAMES)

    skip = set(stdlib_pkgs)

    if not options.all:
        skip.update(CPIP_DISTRIBUTION_NAMES)

        if sys.version_info < (3, 12):
            skip.add("setuptools")

    paths = [os.path.normpath(path) for path in options.path] if options.path else None

    for line in freeze(
        requirement=options.requirement,
        user_only=options.user,
        paths=paths,
        exclude_editable=options.exclude_editable,
        exclude=excluded,
        skip=skip,
    ):
        print(line, end="")

    return 0
