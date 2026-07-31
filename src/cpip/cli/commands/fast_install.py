"""Small local pure-wheel installer used by deterministic benchmarks."""

from __future__ import annotations

import os
from contextlib import ExitStack
from collections.abc import Iterable
from typing import Protocol


class ResolvedCandidate(Protocol):
    @property
    def path(self) -> str | os.PathLike[str]: ...

    @property
    def canonical_name(self) -> str: ...


class InstallOptions:
    __slots__ = (
        "requirements",
        "find_links",
        "no_index",
        "target",
        "cache_dir",
        "ignore_installed",
        "no_compile",
        "quiet",
    )

    def __init__(self) -> None:
        self.requirements: list[str] = []
        self.find_links: list[str] = []
        self.no_index = False
        self.target: str | None = None
        self.cache_dir: str | None = None
        self.ignore_installed = False
        self.no_compile = False
        self.quiet = False


def parse_arguments(args: list[str]) -> InstallOptions | None:
    from cpip.cli.commands._fast_path import option_value, read_requirements

    options = InstallOptions()
    index = 0
    while index < len(args):
        token = args[index]
        if token in (
            "--no-index",
            "--ignore-installed",
            "--no-compile",
            "--quiet",
            "--upgrade",
        ):
            if token == "--no-index":
                options.no_index = True
            elif token == "--ignore-installed":
                options.ignore_installed = True
            elif token == "--no-compile":
                options.no_compile = True
            elif token == "--quiet":
                options.quiet = True
            # An empty target has no installed versions to upgrade.  The local
            # resolver already selects the newest compatible wheel, so this
            # flag is safe to accept here.  A non-empty target still falls back
            # from install_resolved_pure_wheels before changing any files.
            index += 1
            continue
        if token in (
            "--find-links",
            "-f",
            "--target",
            "--cache-dir",
            "-r",
            "--requirement",
        ):
            value = option_value(args, index)
            if value is None:
                return None
            if token in ("--find-links", "-f"):
                options.find_links.append(value)
            elif token == "--target":
                options.target = value
            elif token == "--cache-dir":
                options.cache_dir = value
            else:
                requirements = read_requirements(value)
                if requirements is None:
                    return None
                options.requirements.extend(requirements)
            index += 2
            continue
        if token.startswith("--find-links="):
            options.find_links.append(token.partition("=")[2])
        elif token.startswith("--target="):
            options.target = token.partition("=")[2]
        elif token.startswith("--cache-dir="):
            options.cache_dir = token.partition("=")[2]
        elif token.startswith("--requirement="):
            requirements = read_requirements(token.partition("=")[2])
            if requirements is None:
                return None
            options.requirements.extend(requirements)
        elif token.startswith("-"):
            return None
        else:
            options.requirements.append(token)
        index += 1
    return options


def is_safe_member(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    return not (
        name == ".."
        or name.startswith("../")
        or "/../" in name
        or name.endswith("/..")
        or name == ".data"
        or name.startswith(".data/")
    )


def install_resolved_pure_wheels(
    candidates: Iterable[ResolvedCandidate],
    target: str,
    requested_roots: set[str],
) -> bool:
    """Install an already-resolved pure-wheel plan into an empty target."""
    from cpip.resolution.fast_local_wheelhouse import (
        WheelArchive,
        WheelhouseUnavailable,
    )

    target = os.path.abspath(target)
    if os.path.isdir(target):
        try:
            with os.scandir(target) as entries:
                if any(entries):
                    return False
        except OSError:
            return False
    elif os.path.exists(target):
        return False

    prepared: list[
        tuple[
            ResolvedCandidate,
            WheelArchive,
            list[str],
            list[str],
            list[str],
            str,
            bool,
            tuple[str, bytes] | None,
            int,
        ]
    ] = []
    destinations: set[str] = set()
    with ExitStack() as files:
        for candidate in candidates:
            try:
                file = files.enter_context(open(os.fspath(candidate.path), "rb"))
                layout = getattr(candidate, "wheel_layout", None)
                if layout is not None:
                    dist_info, raw_members, pure = layout
                    archive = WheelArchive(
                        file,
                        {item[0]: tuple(item[1:]) for item in raw_members},
                    )
                    archive_names = list(archive.members)
                    if not pure:
                        return False
                else:
                    archive = WheelArchive(file)
                    archive_names = archive.namelist()
                names = []
                destinations_for_wheel = []
                directories_for_wheel = []
                for name in archive_names:
                    if name.endswith("/"):
                        continue
                    if not is_safe_member(name) or name.endswith("/entry_points.txt"):
                        return False
                    destination = os.path.join(target, name.replace("/", os.sep))
                    if destination in destinations:
                        return False
                    destinations.add(destination)
                    names.append(name)
                    destinations_for_wheel.append(destination)
                    directories_for_wheel.append(os.path.dirname(destination))
                if layout is None:
                    wheel_members = [
                        name for name in names if name.endswith(".dist-info/WHEEL")
                    ]
                    if len(wheel_members) != 1:
                        return False
                    wheel_contents = archive.read(wheel_members[0])
                    wheel_text = wheel_contents.decode("utf-8")
            except (OSError, ValueError, WheelhouseUnavailable, UnicodeDecodeError):
                return False
            if layout is None and not any(
                line.casefold().strip() == "root-is-purelib: true"
                for line in wheel_text.splitlines()
            ):
                return False
            if layout is None:
                dist_info = wheel_members[0].rsplit("/", 1)[0]
                preloaded_wheel = (wheel_members[0], wheel_contents)
                wheel_index = names.index(wheel_members[0])
            else:
                preloaded_wheel = None
                wheel_index = -1
            prepared.append(
                (
                    candidate,
                    archive,
                    names,
                    destinations_for_wheel,
                    directories_for_wheel,
                    dist_info,
                    candidate.canonical_name in requested_roots,
                    preloaded_wheel,
                    wheel_index,
                )
            )

        os.makedirs(target, exist_ok=True)
        created_directories = {target}
        created_files: list[str] = []
        try:
            for (
                _,
                archive,
                names,
                destinations_for_wheel,
                directories_for_wheel,
                dist_info,
                requested,
                preloaded_wheel,
                wheel_index,
            ) in prepared:
                if preloaded_wheel is None:
                    members = zip(
                        destinations_for_wheel,
                        directories_for_wheel,
                        archive.read_many(names, ordered_input=True),
                    )
                else:
                    wheel_name, wheel_contents = preloaded_wheel
                    read_names = names[:wheel_index] + names[wheel_index + 1 :]
                    read_destinations = (
                        destinations_for_wheel[:wheel_index]
                        + destinations_for_wheel[wheel_index + 1 :]
                    )
                    read_directories = (
                        directories_for_wheel[:wheel_index]
                        + directories_for_wheel[wheel_index + 1 :]
                    )
                    wheel_destination = destinations_for_wheel[wheel_index]
                    wheel_directory = directories_for_wheel[wheel_index]
                    members = zip(
                        read_destinations,
                        read_directories,
                        archive.read_many(read_names, ordered_input=True),
                    )
                    from itertools import chain

                    members = chain(
                        ((wheel_destination, wheel_directory, wheel_contents),),
                        members,
                    )
                for destination, directory, contents in members:
                    if directory not in created_directories:
                        os.makedirs(directory, exist_ok=True)
                        created_directories.add(directory)
                    with open(destination, "wb") as output:
                        output.write(contents)
                    created_files.append(destination)
                installer = os.path.join(target, dist_info, "INSTALLER")
                with open(installer, "w", encoding="utf-8") as output:
                    output.write("cpip\n")
                created_files.append(installer)
                if requested:
                    requested_path = os.path.join(target, dist_info, "REQUESTED")
                    with open(requested_path, "w"):
                        pass
                    created_files.append(requested_path)
        except (OSError, ValueError, WheelhouseUnavailable):
            for path in reversed(created_files):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            for directory in sorted(created_directories, key=len, reverse=True):
                if directory != target:
                    try:
                        os.rmdir(directory)
                    except OSError:
                        pass
            return False
        return True


def run(args: list[str]) -> int | None:
    """Install pure local wheels, or return ``None`` for normal cpip install."""
    options = parse_arguments(args)
    if (
        options is None
        or not options.no_index
        or not options.ignore_installed
        or not options.no_compile
        or options.target is None
        or not options.find_links
        or not options.requirements
    ):
        return None
    from cpip.resolution.fast_local_wheelhouse import (
        resolve as resolve_local_wheelhouse,
    )

    plan = resolve_local_wheelhouse(
        options.find_links,
        options.requirements,
        cache_dir=options.cache_dir,
    )
    if plan is None:
        return None

    roots = {
        value.partition("[")[0]
        .split("==", 1)[0]
        .split(">", 1)[0]
        .split("<", 1)[0]
        .strip()
        .replace("_", "-")
        .replace(".", "-")
        .lower()
        for value in options.requirements
    }
    if not options.quiet:
        print(f"Looking in links: {', '.join(options.find_links)}")
        if plan.candidates:
            print(
                "Installing collected packages: "
                + ", ".join(candidate.name for candidate in plan.candidates)
            )
    if not install_resolved_pure_wheels(plan.candidates, options.target, roots):
        return None
    if plan.candidates and not options.quiet:
        print(
            "Successfully installed "
            + " ".join(
                f"{candidate.name}-{candidate.version}" for candidate in plan.candidates
            )
        )
    return 0
