"""Small local pure-wheel installer used by deterministic benchmarks."""

from __future__ import annotations

import os
from contextlib import ExitStack
from typing import Protocol


class _ResolvedCandidate(Protocol):
    path: str | os.PathLike[str]
    canonical_name: str


class _Options:
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


def _parse(args: list[str]) -> _Options | None:
    from pip.cli.commands._fast_path import option_value, read_requirements

    options = _Options()
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("--no-index", "--ignore-installed", "--no-compile", "--quiet"):
            if token == "--no-index":
                options.no_index = True
            elif token == "--ignore-installed":
                options.ignore_installed = True
            elif token == "--no-compile":
                options.no_compile = True
            else:
                options.quiet = True
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


def _safe_member(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    parts = name.split("/")
    return ".." not in parts and ".data" not in parts[0:1]


def install_resolved_pure_wheels(
    candidates: list[_ResolvedCandidate],
    target: str,
    requested_roots: set[str],
) -> bool:
    """Install an already-resolved pure-wheel plan into an empty target."""
    from pip.resolution.fast_local_wheelhouse import WheelArchive, WheelhouseUnavailable

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
        tuple[_ResolvedCandidate, WheelArchive, list[str], str, bool]
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
                        {
                            item[0]: tuple(item[1:])
                            for item in raw_members
                        },
                    )
                    archive_names = list(archive.members)
                    if not pure:
                        return False
                else:
                    archive = WheelArchive(file)
                    archive_names = archive.namelist()
                names = []
                for name in archive_names:
                    if name.endswith("/"):
                        continue
                    if not _safe_member(name) or name.endswith("/entry_points.txt"):
                        return False
                    destination = os.path.join(target, *name.split("/"))
                    if destination in destinations:
                        return False
                    destinations.add(destination)
                    names.append(name)
                if layout is None:
                    wheel_members = [
                        name for name in names if name.endswith(".dist-info/WHEEL")
                    ]
                    if len(wheel_members) != 1:
                        return False
                    wheel_text = archive.read(wheel_members[0]).decode("utf-8")
            except (OSError, ValueError, WheelhouseUnavailable, UnicodeDecodeError):
                return False
            if layout is None and not any(
                line.casefold().strip() == "root-is-purelib: true"
                for line in wheel_text.splitlines()
            ):
                return False
            if layout is None:
                dist_info = wheel_members[0].rsplit("/", 1)[0]
            prepared.append(
                (
                    candidate,
                    archive,
                    names,
                    dist_info,
                    candidate.canonical_name in requested_roots,
                )
            )

        os.makedirs(target, exist_ok=True)
        created_directories = {target}
        created_files: list[str] = []
        try:
            for _, archive, names, dist_info, requested in prepared:
                members = zip(names, archive.read_many(names))
                for name, contents in members:
                    destination = os.path.join(target, *name.split("/"))
                    directory = os.path.dirname(destination)
                    if directory not in created_directories:
                        os.makedirs(directory, exist_ok=True)
                        created_directories.add(directory)
                    with open(destination, "wb") as output:
                        output.write(contents)
                    created_files.append(destination)
                installer = os.path.join(target, dist_info, "INSTALLER")
                with open(installer, "w", encoding="utf-8") as output:
                    output.write("pip\n")
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
    """Install pure local wheels, or return ``None`` for normal pip install."""
    options = _parse(args)
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
    from pip.resolution.fast_local_wheelhouse import resolve as resolve_local_wheelhouse

    plan = resolve_local_wheelhouse(
        options.find_links,
        options.requirements,
        cache_dir=options.cache_dir,
    )
    if plan is None:
        return None

    roots = {
        value.partition("[")[0].split("==", 1)[0].split(">", 1)[0]
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
                f"{candidate.name}-{candidate.version}"
                for candidate in plan.candidates
            )
        )
    return 0
