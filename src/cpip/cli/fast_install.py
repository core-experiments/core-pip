"""Small local pure-wheel installer used by deterministic benchmarks."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence


class PureWheelCandidate:
    __slots__ = ()

    canonical_name: str
    path: str


class FastCandidate(PureWheelCandidate):
    """Lightweight candidate shared by local resolution and installation.

    Metadata is loaded only after resolution selects a filename candidate.  The
    remaining attributes intentionally match the cached-wheel installer
    boundary so local resolution does not need to materialize the much heavier
    general resolver candidate type before installing.
    """

    __slots__ = (
        "archive_members",
        "canonical_name",
        "dependencies",
        "from_cache",
        "name",
        "path",
        "provided_extras",
        "pure",
        "requires_python",
        "source_hashes",
        "source_kind",
        "source_url",
        "source_vcs",
        "version",
        "wheel_layout",
        "yanked_reason",
    )

    def __init__(
        self,
        name: str,
        version: str,
        path: str,
        dependencies: list[str] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.path = path
        self.archive_members: dict[str, tuple[int, int, int, int, int]] | None = None
        self.dependencies = dependencies
        self.canonical_name = normalize_name(name)
        self.pure: bool | None = None
        self.provided_extras: frozenset[str] = frozenset()
        self.requires_python: str | None = None
        self.source_hashes: dict[str, str] | None = None
        self.source_kind: str | None = "wheel"
        self.source_url: str | None = None
        self.source_vcs: str | None = None
        self.from_cache = False
        self.yanked_reason: str | None = None
        self.wheel_layout: object | None = None


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
        "upgrade",
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
        self.upgrade = False


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
            elif token == "--upgrade":
                options.upgrade = True
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


def _remote_index_url() -> str | None:
    """Return the effective sole index, or decline non-default source shapes."""
    from cpip.cli.config import ConfigurationStore
    from cpip.core.errors import ConfigurationError
    from cpip.index.config import DEFAULT_INDEX_URL

    store = ConfigurationStore()
    try:
        store.load()
    except ConfigurationError:
        index_url = DEFAULT_INDEX_URL
        find_links = None
        extra_index_urls = None
        no_index = None
    else:

        def configured(option: str) -> str | None:
            value = store.get_optional(f"install.{option}")
            if value is not None:
                return value
            return store.get_optional(f"global.{option}")

        index_url = configured("index-url") or DEFAULT_INDEX_URL
        find_links = configured("find-links")
        extra_index_urls = configured("extra-index-url")
        no_index = configured("no-index")

    if (value := os.environ.get("CPIP_INDEX_URL")) is not None:
        index_url = value
    if (value := os.environ.get("CPIP_FIND_LINKS")) is not None:
        find_links = value
    if (value := os.environ.get("CPIP_EXTRA_INDEX_URL")) is not None:
        extra_index_urls = value
    if (value := os.environ.get("CPIP_NO_INDEX")) is not None:
        no_index = value
    if (
        find_links
        or extra_index_urls
        or (no_index and no_index.strip().lower() in {"1", "true", "yes", "on"})
    ):
        return None
    return index_url


def run_cached_remote(args: list[str]) -> int | None:
    """Install a previously validated exact-pin plan without CLI initialization."""
    options = parse_arguments(args)
    if (
        options is None
        or not options.quiet
        or not options.ignore_installed
        or not options.no_compile
        or options.no_index
        or options.find_links
        or options.upgrade
        or options.target is None
        or os.path.lexists(options.target)
    ):
        return None
    cache_dir = options.cache_dir or os.environ.get("CPIP_CACHE_DIR")
    if cache_dir is None:
        from cpip.core.appdirs import user_cache_dir

        cache_dir = user_cache_dir("cpip")
    index_url = _remote_index_url()
    if index_url is None:
        return None

    from cpip.install.wheel_archive_cache import (
        exact_install_plan_key_from_strings,
        install_wheels_from_archive_cache,
        load_cached_install_plan,
    )

    keyed = exact_install_plan_key_from_strings(
        tuple(options.requirements),
        (
            "remote-exact-v1",
            index_url,
            (),
            (),
            None,
            f"{sys.version_info.major}{sys.version_info.minor}",
            (),
            "only-if-needed",
            False,
        ),
    )
    if keyed is None:
        return None
    key, requested_roots = keyed
    plan = load_cached_install_plan(cache_dir, key)
    if plan is None:
        return None

    from cpip.install.target import InstallTarget

    installed = install_wheels_from_archive_cache(
        tuple(
            (
                candidate.path,
                candidate.canonical_name in requested_roots,
                None,
            )
            for candidate in plan.candidates
        ),
        tuple(plan.candidates),
        target=InstallTarget.from_options("cpip", target=options.target),
        cache_dir=cache_dir,
        report=not options.quiet,
    )
    return 0 if installed is not None else None


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


def wheel_metadata(
    candidate: FastCandidate,
    cache=None,
) -> tuple[list[str], bool] | None:
    from cpip.resolution.engine.sources.wheelhouse.archive import (
        WheelArchive,
        WheelhouseUnavailable,
    )

    path = candidate.path
    identity = cache.identity(path) if cache is not None else None
    if identity is not None and cache is not None:
        cached = cache.get(identity)
        if cached is not None:
            dependencies, pure = cached
            return list(dependencies), pure
    try:
        with open(path, "rb") as wheel_file:
            archive = WheelArchive(wheel_file)
            names = archive.namelist()
            candidate.archive_members = archive.members
            metadata_members = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            wheel_members = [
                name for name in names if name.endswith(".dist-info/WHEEL")
            ]
            if len(metadata_members) != 1 or len(wheel_members) != 1:
                return None
            metadata = archive.read(metadata_members[0]).decode("utf-8")
            wheel = archive.read(wheel_members[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, WheelhouseUnavailable):
        return None
    dependencies = []
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist:"):
            dependencies.append(line.partition(":")[2].strip())
    pure = any(
        line.casefold().strip() == "root-is-purelib: true"
        for line in wheel.splitlines()
    )
    result = (dependencies, pure)
    if identity is not None and cache is not None:
        cache.put(identity, (tuple(dependencies), pure))
    return result


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
    metadata_cache: object | None = None,
) -> list[FastCandidate] | None:
    get_plan = (
        getattr(metadata_cache, "get_plan", None)
        if metadata_cache is not None
        else None
    )
    if get_plan is not None:
        cached_plan = get_plan(find_links, requirements)
        if cached_plan is not None:
            result = []
            for name, version, path, dependencies in cached_plan:
                candidate = FastCandidate(name, version, path, list(dependencies))
                candidate.pure = True
                result.append(candidate)
            return result

    paths = iter_wheel_paths(find_links)
    if paths is None:
        return None
    candidates_by_name: dict[str, list[FastCandidate]] = {}
    for path in paths:
        parsed = parse_wheel_filename(path)
        if parsed is None:
            return None
        name, version = parsed
        candidate = FastCandidate(name, version, path)
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
        if selected.dependencies is None:
            metadata = wheel_metadata(selected, metadata_cache)
            if metadata is None:
                return False
            dependencies, pure = metadata
            selected.dependencies = dependencies
            selected.pure = pure
        if not selected.pure:
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
    result = list(resolved.values())
    put_plan = (
        getattr(metadata_cache, "put_plan", None)
        if metadata_cache is not None
        else None
    )
    if put_plan is not None:
        put_plan(
            find_links,
            requirements,
            tuple(
                (
                    candidate.name,
                    candidate.version,
                    candidate.path,
                    tuple(candidate.dependencies or ()),
                )
                for candidate in result
            ),
        )
    return result


def install_resolved_pure_wheels(
    candidates: Sequence[PureWheelCandidate],
    target: str,
    requested_roots: set[str],
) -> bool:
    """Install an already-resolved pure-wheel plan into an empty target."""
    from cpip.resolution.engine.sources.wheelhouse.archive import (
        WheelArchive,
        WheelhouseUnavailable,
    )

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

    prepared: list[tuple[str, bool, bool, list[tuple[str, str, bytes]]]] = []
    destinations: set[str] = set()
    for candidate in candidates:
        try:
            with open(os.fspath(candidate.path), "rb") as wheel_file:
                archive = WheelArchive(
                    wheel_file,
                    members=getattr(candidate, "archive_members", None),
                )
                archive_names = archive.namelist()
                names = []
                destinations_for_wheel = []
                directories_for_wheel = []
                for name in archive_names:
                    if name.endswith("/"):
                        continue
                    top_level = name.split("/", 1)[0]
                    if (
                        not is_safe_member(name)
                        or top_level.endswith(".data")
                        or name.endswith("/entry_points.txt")
                    ):
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
                contents = archive.read_many(names)
                wheel_contents = contents[names.index(wheel_members[0])]
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
                        contents,
                    )
                    for destination, directory, contents in zip(
                        destinations_for_wheel,
                        directories_for_wheel,
                        contents,
                    )
                ]
        except (OSError, ValueError, UnicodeDecodeError, WheelhouseUnavailable):
            return False
        prepared.append(
            (
                dist_info,
                candidate.canonical_name in requested_roots,
                any(
                    destination == os.path.join(target, dist_info, "RECORD")
                    and bool(contents.strip())
                    for destination, _, contents in members
                ),
                members,
            ),
        )

    os.makedirs(target, exist_ok=True)
    created_directories = {target}
    created_files: list[str] = []
    try:
        for dist_info, requested, reuse_record, members in prepared:
            record_relative = f"{dist_info}/RECORD"
            record_rows: dict[str, tuple[str, str, str]] = {}
            if not reuse_record:
                import base64
                import csv
                import hashlib
            for destination, directory, contents in members:
                if directory not in created_directories:
                    os.makedirs(directory, exist_ok=True)
                    created_directories.add(directory)
                with open(destination, "wb", buffering=0) as output:
                    output.write(contents)
                created_files.append(destination)
                if not reuse_record:
                    relative = os.path.relpath(destination, target).replace(
                        os.sep,
                        "/",
                    )
                if not reuse_record and relative != record_relative:
                    digest = base64.urlsafe_b64encode(
                        hashlib.sha256(contents).digest(),
                    ).rstrip(b"=")
                    record_rows[relative] = (
                        relative,
                        f"sha256={digest.decode('ascii')}",
                        str(len(contents)),
                    )
            installer = os.path.join(target, dist_info, "INSTALLER")
            with open(installer, "w", encoding="utf-8") as output:
                output.write("cpip\n")
            created_files.append(installer)
            if not reuse_record:
                installer_relative = f"{dist_info}/INSTALLER"
                installer_digest = base64.urlsafe_b64encode(
                    hashlib.sha256(b"cpip\n").digest(),
                ).rstrip(b"=")
                record_rows[installer_relative] = (
                    installer_relative,
                    f"sha256={installer_digest.decode('ascii')}",
                    "5",
                )
            if requested:
                requested_path = os.path.join(target, dist_info, "REQUESTED")
                with open(requested_path, "w"):
                    pass
                created_files.append(requested_path)
                if not reuse_record:
                    requested_relative = f"{dist_info}/REQUESTED"
                    empty_digest = base64.urlsafe_b64encode(
                        hashlib.sha256(b"").digest(),
                    ).rstrip(b"=")
                    record_rows[requested_relative] = (
                        requested_relative,
                        f"sha256={empty_digest.decode('ascii')}",
                        "0",
                    )
            if not reuse_record:
                record_rows[record_relative] = (record_relative, "", "")
                record_path = os.path.join(target, dist_info, "RECORD")
                with open(
                    record_path,
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as output:
                    csv.writer(output).writerows(
                        record_rows[name] for name in sorted(record_rows)
                    )
                if record_path not in created_files:
                    created_files.append(record_path)
    except (OSError, ValueError):
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


def _install_cached_tree(tree: str, target: str) -> bool:
    from cpip.platform.clone import clone_path

    target_existed = os.path.isdir(target)
    try:
        if target_existed:
            os.rmdir(target)
        clone_path(tree, target)
    except OSError:
        if target_existed and not os.path.lexists(target):
            try:
                os.makedirs(target)
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
    # A non-empty target cannot use the specialized installer.  Check this
    # before resolving so the normal fallback does not resolve the same local
    # wheelhouse a second time just to reject the plan.
    if not _target_is_empty(options.target):
        return None

    metadata_cache = None
    if options.cache_dir is not None:
        from cpip.cli.fast_install_cache import FastInstallMetadataCache

        metadata_cache = FastInstallMetadataCache(options.cache_dir)
    candidates = resolve_simple_wheelhouse(
        options.find_links,
        options.requirements,
        metadata_cache,
    )
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
    installed = False
    if metadata_cache is not None:
        tree = metadata_cache.get_install_tree(
            options.find_links,
            options.requirements,
        )
        if tree is not None:
            installed = _install_cached_tree(tree, options.target)
    if not installed:
        if not install_resolved_pure_wheels(candidates, options.target, roots):
            return None
        if metadata_cache is not None:
            metadata_cache.put_install_tree(
                options.find_links,
                options.requirements,
                options.target,
            )
    if candidates and not options.quiet:
        print(
            "Successfully installed "
            + " ".join(
                f"{candidate.name}-{candidate.version}" for candidate in candidates
            ),
        )
    if metadata_cache is not None:
        metadata_cache.flush()
    return 0


def run_local_fallback(args: list[str]) -> int | None:
    """Handle the narrow local-wheel shape when fast install needs fallback."""
    options = parse_arguments(args)
    if (
        options is None
        or not options.no_index
        or not (options.ignore_installed or options.upgrade)
        or not options.no_compile
        or options.target is None
        or not options.find_links
        or not options.requirements
        or "--no-compile" not in args
        or _target_is_empty(options.target)
    ):
        return None

    if options.upgrade and not options.ignore_installed:
        for requirement in options.requirements:
            name, separator, version = requirement.partition("==")
            if (
                not separator
                or not name.strip()
                or not version.strip()
                or "*" in version
                or any(character in requirement for character in "[];@<>,!")
            ):
                return None

    from cpip.install.target import InstallTarget

    metadata_cache = None
    if options.cache_dir is not None:
        from cpip.cli.fast_install_cache import FastInstallMetadataCache

        metadata_cache = FastInstallMetadataCache(options.cache_dir)
    local_candidates = resolve_simple_wheelhouse(
        options.find_links,
        options.requirements,
        metadata_cache,
    )
    if local_candidates is None:
        return None
    if (
        options.upgrade
        and not options.ignore_installed
        and any(candidate.dependencies for candidate in local_candidates)
    ):
        return None

    candidates = local_candidates

    if options.cache_dir is not None:
        from cpip.install.wheel_archive_cache import (
            install_wheels_from_archive_cache,
            prepare_cached_wheel,
        )

        try:
            for candidate in candidates:
                identity = (
                    metadata_cache.identity(candidate.path)
                    if metadata_cache is not None
                    else None
                )
                digest = (
                    metadata_cache.get_digest(identity)
                    if metadata_cache is not None and identity is not None
                    else None
                )
                if digest is not None:
                    candidate.source_hashes = {"sha256": digest}
                archive = prepare_cached_wheel(
                    candidate,
                    options.cache_dir,
                )
                candidate.wheel_layout = archive
                candidate.source_hashes = {"sha256": archive.digest}
                if metadata_cache is not None and identity is not None:
                    metadata_cache.put_digest(
                        identity,
                        archive.digest,
                        (tuple(candidate.dependencies or ()), bool(candidate.pure)),
                    )
        except OSError:
            return None

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
    requests = tuple(
        (candidate.path, candidate.canonical_name in requested_roots, None)
        for candidate in candidates
    )
    target = InstallTarget.from_options("cpip", target=options.target)
    if options.cache_dir is not None:
        installed = install_wheels_from_archive_cache(
            requests,
            tuple(candidates),
            target=target,
            cache_dir=options.cache_dir,
            force=options.ignore_installed,
            preserve_existing=options.ignore_installed,
            report=not options.quiet,
        )
        if installed is None:
            return None
    else:
        from cpip.core.packaging import Version
        from cpip.core.wheel import WheelCandidate
        from cpip.install.wheel_transaction import install_wheels_transactionally

        wheel_candidates = [
            WheelCandidate(
                name=candidate.name,
                version=Version(str(candidate.version)),
                path=candidate.path,
                dependencies=(),
                provided_extras=frozenset(),
                requires_python=None,
                source_kind="wheel",
            )
            for candidate in candidates
        ]
        install_wheels_transactionally(
            requests,
            target=target,
            pycompile=False,
            force=options.ignore_installed,
            preserve_existing=options.ignore_installed,
            candidates=wheel_candidates,
        )
    if candidates and not options.quiet:
        print(
            "Successfully installed "
            + " ".join(
                f"{candidate.name}-{candidate.version}" for candidate in candidates
            ),
        )
    if metadata_cache is not None:
        metadata_cache.flush()
    return 0
