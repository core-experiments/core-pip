"""Small local pure-wheel installer used by deterministic benchmarks."""

from __future__ import annotations

import os


class FastCandidate:
    __slots__ = ("canonical_name", "dependencies", "name", "path", "version")

    def __init__(
        self,
        name: str,
        version: str,
        path: str,
        dependencies: list[str],
    ) -> None:
        self.name = name
        self.version = version
        self.path = path
        self.dependencies = dependencies
        self.canonical_name = normalize_name(name)


class InstallOptions:
    __slots__ = (
        "cache_dir",
        "find_links",
        "ignore_installed",
        "no_compile",
        "no_index",
        "quiet",
        "requirements",
        "target",
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


def option_value(args: list[str], index: int) -> str | None:
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    return None if value.startswith("-") else value


def read_requirements(path: str) -> list[str] | None:
    try:
        with open(path, encoding="utf-8") as requirement_file:
            return [
                line.strip()
                for line in requirement_file.read().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        return None


def parse_arguments(args: list[str]) -> InstallOptions | None:
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
    if not name or "\\" in name:
        return False
    return not (
        name in ("..", ".data")
        or name.startswith(("/", "../", ".data/"))
        or "/../" in name
        or name.endswith("/..")
    )


def normalize_name(value: str) -> str:
    return value.replace("_", "-").replace(".", "-").lower()


def version_key(value: str) -> tuple[int, ...] | None:
    parts = value.split(".")
    if not parts:
        return None
    result = []
    for part in parts:
        if not part.isdigit():
            return None
        result.append(int(part))
    while len(result) > 1 and result[-1] == 0:
        result.pop()
    return tuple(result)


def parse_wheel_filename(path: str) -> tuple[str, str] | None:
    filename = os.path.basename(path)
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    name, version = parts[0], parts[1]
    if version_key(version) is None:
        return None
    return name.replace("_", "-"), version


def parse_requirement(value: str) -> tuple[str, str, tuple[int, ...] | None] | None:
    value = value.split(";", 1)[0].strip()
    if not value:
        return None
    for operator in ("==", ">=", "<=", ">", "<"):
        if operator in value:
            name, _, version = value.partition(operator)
            key = version_key(version.strip())
            if key is None:
                return None
            return normalize_name(name.partition("[")[0].strip()), operator, key
    return normalize_name(value.partition("[")[0].strip()), "", None


def requirement_satisfied(
    requirement: tuple[str, str, tuple[int, ...] | None],
    candidate: FastCandidate,
) -> bool:
    _, operator, expected = requirement
    if not operator:
        return True
    key = version_key(candidate.version)
    if key is None or expected is None:
        return False
    if operator == "==":
        return key == expected
    if operator == ">=":
        return key >= expected
    if operator == "<=":
        return key <= expected
    if operator == ">":
        return key > expected
    if operator == "<":
        return key < expected
    return False


def wheel_metadata(path: str) -> tuple[list[str], bool] | None:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            metadata_members = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            wheel_members = [
                name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
            ]
            if len(metadata_members) != 1 or len(wheel_members) != 1:
                return None
            metadata = archive.read(metadata_members[0]).decode("utf-8")
            wheel = archive.read(wheel_members[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    dependencies = []
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist:"):
            dependencies.append(line.partition(":")[2].strip())
    pure = any(
        line.casefold().strip() == "root-is-purelib: true"
        for line in wheel.splitlines()
    )
    return dependencies, pure


def iter_wheel_paths(find_links: list[str]) -> list[str] | None:
    result = []
    for value in find_links:
        if value.endswith(".whl"):
            if not os.path.isfile(value):
                return None
            result.append(os.path.abspath(value))
            continue
        try:
            with os.scandir(value) as entries:
                for entry in entries:
                    if entry.name.endswith(".whl") and entry.is_file():
                        result.append(os.path.abspath(entry.path))
        except OSError:
            return None
    return result


def resolve_simple_wheelhouse(
    find_links: list[str],
    requirements: list[str],
) -> list[FastCandidate] | None:
    paths = iter_wheel_paths(find_links)
    if paths is None:
        return None
    candidates_by_name: dict[str, list[FastCandidate]] = {}
    for path in paths:
        parsed = parse_wheel_filename(path)
        if parsed is None:
            return None
        name, version = parsed
        metadata = wheel_metadata(path)
        if metadata is None:
            return None
        dependencies, pure = metadata
        if not pure:
            return None
        candidate = FastCandidate(name, version, path, dependencies)
        candidates_by_name.setdefault(candidate.canonical_name, []).append(candidate)
    for candidates in candidates_by_name.values():
        candidates.sort(
            key=lambda candidate: version_key(candidate.version) or (), reverse=True
        )

    resolved: dict[str, FastCandidate] = {}
    visiting: set[str] = set()

    def add_requirement(raw: str) -> bool:
        requirement = parse_requirement(raw)
        if requirement is None:
            return False
        name = requirement[0]
        existing = resolved.get(name)
        if existing is not None:
            return requirement_satisfied(requirement, existing)
        if name in visiting:
            return True
        candidates = candidates_by_name.get(name, ())
        selected = next(
            (
                candidate
                for candidate in candidates
                if requirement_satisfied(requirement, candidate)
            ),
            None,
        )
        if selected is None:
            return False
        visiting.add(name)
        for dependency in selected.dependencies:
            if not add_requirement(dependency):
                return False
        visiting.remove(name)
        resolved[name] = selected
        return True

    for requirement in requirements:
        if not add_requirement(requirement):
            return None
    return list(resolved.values())


def install_resolved_pure_wheels(
    candidates: list[FastCandidate],
    target: str,
    requested_roots: set[str],
) -> bool:
    """Install an already-resolved pure-wheel plan into an empty target."""
    import zipfile

    target = os.path.abspath(target)
    separator = os.sep
    if os.path.isdir(target):
        try:
            with os.scandir(target) as entries:
                if any(entries):
                    return False
        except OSError:
            return False
    elif os.path.exists(target):
        return False

    prepared: list[tuple[str, bool, list[tuple[str, str, bytes]]]] = []
    destinations: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, FastCandidate):
            return False
        try:
            with zipfile.ZipFile(os.fspath(candidate.path)) as archive:
                archive_names = archive.namelist()
                names = []
                destinations_for_wheel = []
                directories_for_wheel = []
                for name in archive_names:
                    if name.endswith("/"):
                        continue
                    if not is_safe_member(name) or name.endswith("/entry_points.txt"):
                        return False
                    destination = os.path.join(
                        target,
                        name if separator == "/" else name.replace("/", separator),
                    )
                    if destination in destinations:
                        return False
                    destinations.add(destination)
                    names.append(name)
                    destinations_for_wheel.append(destination)
                    directories_for_wheel.append(os.path.dirname(destination))
                wheel_members = [
                    name for name in names if name.endswith(".dist-info/WHEEL")
                ]
                if len(wheel_members) != 1:
                    return False
                wheel_contents = archive.read(wheel_members[0])
                wheel_text = wheel_contents.decode("utf-8")
                if not any(
                    line.casefold().strip() == "root-is-purelib: true"
                    for line in wheel_text.splitlines()
                ):
                    return False
                dist_info = wheel_members[0].rsplit("/", 1)[0]
                members = [
                    (
                        destination,
                        directory,
                        wheel_contents
                        if name == wheel_members[0]
                        else archive.read(name),
                    )
                    for name, destination, directory in zip(
                        names,
                        destinations_for_wheel,
                        directories_for_wheel,
                    )
                ]
        except (OSError, ValueError, zipfile.BadZipFile, UnicodeDecodeError):
            return False
        prepared.append(
            (
                dist_info,
                candidate.canonical_name in requested_roots,
                members,
            ),
        )

    os.makedirs(target, exist_ok=True)
    created_directories = {target}
    created_files: list[str] = []
    try:
        for dist_info, requested, members in prepared:
            for destination, directory, contents in members:
                if directory not in created_directories:
                    os.makedirs(directory, exist_ok=True)
                    created_directories.add(directory)
                with open(destination, "wb", buffering=0) as output:
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
    except (OSError, ValueError, zipfile.BadZipFile):
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


def _target_is_empty(target: str) -> bool:
    if os.path.isdir(target):
        try:
            with os.scandir(target) as entries:
                return not any(entries)
        except OSError:
            return False
    return not os.path.exists(target)


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
    # A non-empty target cannot use the specialized installer.  Check this
    # before resolving so the normal fallback does not resolve the same local
    # wheelhouse a second time just to reject the plan.
    if not _target_is_empty(options.target):
        return None

    candidates = resolve_simple_wheelhouse(options.find_links, options.requirements)
    if candidates is None:
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
        if candidates:
            print(
                "Installing collected packages: "
                + ", ".join(candidate.name for candidate in candidates),
            )
    if not install_resolved_pure_wheels(candidates, options.target, roots):
        return None
    if candidates and not options.quiet:
        print(
            "Successfully installed "
            + " ".join(
                f"{candidate.name}-{candidate.version}" for candidate in candidates
            ),
        )
    return 0


def run_local_fallback(args: list[str]) -> int | None:
    """Handle the narrow local-wheel shape when fast install needs fallback."""
    options = parse_arguments(args)
    if (
        options is None
        or not options.no_index
        or not options.ignore_installed
        or not options.no_compile
        or options.target is None
        or not options.find_links
        or not options.requirements
        or "--no-compile" not in args
        or _target_is_empty(options.target)
    ):
        return None

    from cpip.core.packaging import Version
    from cpip.core.wheel import WheelCandidate, parse_wheel, wheel_candidate
    from cpip.install.target import InstallTarget
    from cpip.install.wheel_transaction import install_wheels_transactionally
    from cpip.resolution.engine import ResolutionEngine

    plan = ResolutionEngine.resolve_wheelhouse(
        options.find_links,
        options.requirements,
        cache_dir=options.cache_dir,
    )
    if plan is None:
        return None

    candidates: list[WheelCandidate] = []
    try:
        for local_candidate in plan.candidates:
            candidates.append(
                WheelCandidate(
                    name=local_candidate.name,
                    version=Version(str(local_candidate.version)),
                    path=local_candidate.path,
                    dependencies=(),
                    provided_extras=local_candidate.provided_extras,
                    requires_python=local_candidate.requires_python,
                    source_kind="wheel",
                ),
            )
    except (OSError, TypeError, ValueError):
        return None

    if (
        sum(os.stat(candidate.path).st_size for candidate in candidates)
        > 4 * 1024 * 1024
    ):
        import zipfile

        for index, candidate in enumerate(candidates):
            with zipfile.ZipFile(candidate.path) as archive:
                dist_info, _ = parse_wheel(
                    archive,
                    os.path.basename(candidate.path)[:-4].split("-", 1)[0],
                )
                layout = wheel_candidate(
                    candidate.path,
                    archive=archive,
                    dist_info_dir=dist_info,
                ).wheel_layout
            candidates[index] = candidate.copy_with(wheel_layout=layout)

    requested_roots = {
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
        if candidates:
            print(
                "Installing collected packages: "
                + ", ".join(candidate.name for candidate in candidates),
            )
    install_wheels_transactionally(
        [
            (candidate.path, candidate.canonical_name in requested_roots, None)
            for candidate in candidates
        ],
        target=InstallTarget.from_options("cpip", target=options.target),
        pycompile=False,
        preserve_existing=True,
        candidates=candidates,
    )
    if candidates and not options.quiet:
        print(
            "Successfully installed "
            + " ".join(
                f"{candidate.name}-{candidate.version}" for candidate in candidates
            ),
        )
    return 0
