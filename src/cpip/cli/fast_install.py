"""Fast paths for installing wheels without initializing the normal CLI.

Three entry points cover three target states, all guarded by
:mod:`cpip.cli.fast` and all declining to normal install dispatch:

- :func:`run_cached_remote` -- a missing target and an exact-pin remote plan
  already validated in the archive cache.
- :func:`run` -- an empty target and a local ``--no-index`` wheelhouse, which
  can clone a cached completed target or run the minimal resolver.
- :func:`run_local_fallback` -- a non-empty local target, resolved the same way
  but installed through the archive or transactional installer.
"""

from __future__ import annotations

import atexit
import hashlib
import marshal
import os
import stat
import sys
from collections.abc import Iterable, Sequence

from cpip.cli.fast import consume_option, extend_requirements
from cpip.core.appdirs import resolve_cache_dir, versioned_cache_dir
from cpip.core.packaging import EMPTY_FROZENSET
from cpip.core.versions import Version
from cpip.core.utils import CACHE_INTERPRETER_TAG, load_snapshot, save_snapshot
from cpip.core.wheel import PureWheelCandidate, WheelCandidate
from cpip.core.wheel import parse_wheel_filename as parse_wheel_filename_core
from cpip.install.target import InstallTarget
from cpip.install.wheel_archive import mode_from_external_attr
from cpip.install.wheel_archive_cache import prepare_cached_wheel
from cpip.install.wheel_archive_installer import install_wheels_from_archive_cache
from cpip.install.wheel_install_plan_cache import (
    REMOTE_EXACT_CONTEXT,
    exact_install_plan_key_from_strings,
    load_cached_install_plan,
)
from cpip.platform.clone import clone_path
from cpip.resolution.archive import (
    WheelArchive,
    WheelhouseUnavailable,
)

# Scoped to the interpreter: marshal's wire format is not guaranteed
# compatible across Python versions/implementations. TREE_CACHE_BUCKET does
# not need the same treatment -- it is only ever reached through a plan
# entry recorded in this file, so a tagged (and therefore missing, for a
# different interpreter) snapshot already makes tree lookups miss cleanly.
NAME = f"fast-install-{CACHE_INTERPRETER_TAG}.marshal"
MAX_ENTRIES = 8_192
MAX_PLANS = 256
TREE_CACHE_BUCKET = "fast-install-trees"

Metadata = tuple[tuple[str, ...], bool]
StoredMetadata = tuple[tuple[str, ...], bool, str | None]
Identity = tuple[str, int, int, int]
LinkIdentity = tuple[str, str, int, int]
PlanKey = tuple[tuple[LinkIdentity, ...], tuple[str, ...]]
PlanCandidate = tuple[str, str, str, tuple[str, ...]]
PlanRecord = tuple[str, str, str, tuple[str, ...], int, int, int]
Plan = tuple[PlanRecord, ...]
PlanValue = tuple[Plan, str | None]


class FastInstallMetadataCache:
    __slots__ = ("dirty", "entries", "path", "plan_hit", "plans")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = os.path.join(os.fspath(cache_dir), NAME)
        self.entries: dict[Identity, StoredMetadata] = {}
        self.plans: dict[PlanKey, PlanValue] = {}
        self.plan_hit = False
        self.dirty = False
        self._load()
        atexit.register(self.flush)

    def _load(self) -> None:
        payload = load_snapshot(self.path)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or payload[0] != "cpip-fast-install"
            or not isinstance(payload[1], dict)
            or not isinstance(payload[2], dict)
        ):
            return

        for key, value in payload[1].items():
            metadata = self._coerce_metadata(value)
            if self._valid_identity(key) and metadata is not None:
                self.entries[key] = metadata  # ty: ignore[invalid-assignment]

        for key, value in payload[2].items():
            if self._valid_plan_key(key) and self._valid_plan_value(value):
                self.plans[key] = value  # ty: ignore[invalid-assignment]

    @staticmethod
    def _valid_identity(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 4
            and isinstance(value[0], str)
            and isinstance(value[1], int)
            and isinstance(value[2], int)
            and isinstance(value[3], int)
        )

    @staticmethod
    def _coerce_metadata(value: object) -> StoredMetadata | None:
        if not (
            isinstance(value, tuple)
            and len(value) == 3
            and isinstance(value[0], tuple)
            and all(isinstance(item, str) for item in value[0])
            and isinstance(value[1], bool)
        ):
            return None

        digest = value[2]
        if digest is not None and not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            return None

        return (
            tuple(item for item in value[0] if isinstance(item, str)),
            value[1],
            digest,
        )

    @staticmethod
    def _valid_link_identity(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 4
            and value[0] in ("d", "f")
            and isinstance(value[1], str)
            and isinstance(value[2], int)
            and isinstance(value[3], int)
        )

    @classmethod
    def _valid_plan_key(cls, value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], tuple)
            and all(cls._valid_link_identity(item) for item in value[0])
            and isinstance(value[1], tuple)
            and all(isinstance(item, str) for item in value[1])
        )

    @staticmethod
    def _valid_plan(value: object) -> bool:
        return isinstance(value, tuple) and all(
            isinstance(record, tuple)
            and len(record) == 7
            and isinstance(record[0], str)
            and isinstance(record[1], str)
            and isinstance(record[2], str)
            and isinstance(record[3], tuple)
            and all(isinstance(item, str) for item in record[3])
            and isinstance(record[4], int)
            and isinstance(record[5], int)
            and isinstance(record[6], int)
            for record in value
        )

    @classmethod
    def _valid_plan_value(cls, value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 2
            and cls._valid_plan(value[0])
            and (
                value[1] is None
                or (
                    isinstance(value[1], str)
                    and len(value[1]) == 64
                    and all(character in "0123456789abcdef" for character in value[1])
                )
            )
        )

    @staticmethod
    def identity(path: str) -> Identity | None:
        try:
            stat_val = os.stat(path)
        except OSError:
            return None
        return (
            os.path.abspath(path),
            stat_val.st_size,
            stat_val.st_mtime_ns,
            stat_val.st_ctime_ns,
        )

    @staticmethod
    def _link_identity(path: str) -> LinkIdentity | None:
        absolute = os.path.abspath(path)
        try:
            path_stat = os.stat(absolute)
        except OSError:
            return None
        if stat.S_ISDIR(path_stat.st_mode):
            kind = "d"
        elif stat.S_ISREG(path_stat.st_mode):
            kind = "f"
        else:
            return None
        return (kind, absolute, path_stat.st_size, path_stat.st_mtime_ns)

    @classmethod
    def _plan_key(
        cls,
        find_links: Sequence[str],
        requirements: Sequence[str],
    ) -> PlanKey | None:
        links = []
        for path in find_links:
            identity = cls._link_identity(path)
            if identity is None:
                return None
            links.append(identity)
        return (tuple(links), tuple(requirements))

    def get(self, identity: Identity) -> Metadata | None:
        value = self.entries.get(identity)
        return None if value is None else (value[0], value[1])

    def put(self, identity: Identity, metadata: Metadata) -> None:
        if identity not in self.entries and len(self.entries) >= MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        previous = self.entries.get(identity)
        digest = None if previous is None else previous[2]
        self.entries[identity] = (metadata[0], metadata[1], digest)
        self.dirty = True

    def get_digest(self, identity: Identity) -> str | None:
        value = self.entries.get(identity)
        return None if value is None else value[2]

    def put_digest(
        self,
        identity: Identity,
        digest: str,
        metadata: Metadata,
    ) -> None:
        value = (metadata[0], metadata[1], digest)
        if self.entries.get(identity) == value:
            return
        if identity not in self.entries and len(self.entries) >= MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        self.entries[identity] = value
        self.dirty = True

    def get_plan(
        self,
        find_links: Sequence[str],
        requirements: Sequence[str],
    ) -> tuple[PlanCandidate, ...] | None:
        self.plan_hit = False
        key = self._plan_key(find_links, requirements)
        if key is None:
            return None
        value = self.plans.get(key)
        if value is None:
            return None
        plan = value[0]
        result = []
        for name, version, path, dependencies, size, mtime_ns, ctime_ns in plan:
            if self.identity(path) != (path, size, mtime_ns, ctime_ns):
                self.plans.pop(key, None)
                self.dirty = True
                return None
            result.append((name, version, path, dependencies))
        self.plan_hit = True
        return tuple(result)

    def put_plan(
        self,
        find_links: Sequence[str],
        requirements: Sequence[str],
        candidates: Sequence[PlanCandidate],
    ) -> None:
        key = self._plan_key(find_links, requirements)
        if key is None:
            return
        records = []
        for name, version, path, dependencies in candidates:
            identity = self.identity(path)
            if identity is None:
                return
            absolute, size, mtime_ns, ctime_ns = identity
            records.append(
                (
                    name,
                    version,
                    absolute,
                    dependencies,
                    size,
                    mtime_ns,
                    ctime_ns,
                ),
            )
        if key not in self.plans and len(self.plans) >= MAX_PLANS:
            self.plans.pop(next(iter(self.plans)))
        plan = tuple(records)
        previous = self.plans.get(key)
        tree_key = previous[1] if previous is not None and previous[0] == plan else None
        self.plans[key] = (plan, tree_key)
        self.dirty = True

    def _tree_path(self, tree_key: str) -> str:
        return os.path.join(
            os.path.dirname(self.path),
            TREE_CACHE_BUCKET,
            tree_key[:2],
            tree_key,
            "tree",
        )

    def get_install_tree(
        self,
        find_links: Sequence[str],
        requirements: Sequence[str],
    ) -> str | None:
        if not self.plan_hit:
            return None
        key = self._plan_key(find_links, requirements)
        if key is None:
            return None
        value = self.plans.get(key)
        if value is None or value[1] is None:
            return None
        tree = self._tree_path(value[1])
        return tree if os.path.isdir(tree) else None

    def put_install_tree(
        self,
        find_links: Sequence[str],
        requirements: Sequence[str],
        target: str,
    ) -> None:
        if not self.plan_hit:
            return
        key = self._plan_key(find_links, requirements)
        if key is None:
            return
        value = self.plans.get(key)
        if value is None:
            return
        tree_key = hashlib.sha256(
            marshal.dumps(
                ("cpip-fast-install-tree", key, value[0]),
            ),
        ).hexdigest()
        tree = self._tree_path(tree_key)
        if not os.path.isdir(tree):
            entry = os.path.dirname(tree)
            shard = os.path.dirname(entry)
            try:
                os.makedirs(shard, exist_ok=True)
                # Deferred: tempfile (and shutil behind it) only when a tree is published.
                import tempfile

                temporary = tempfile.mkdtemp(prefix=f".{tree_key[:12]}-", dir=shard)
            except OSError:
                return
            try:
                clone_path(target, os.path.join(temporary, "tree"))
                try:
                    os.rename(temporary, entry)
                except FileExistsError:
                    if not os.path.isdir(tree):
                        return
                else:
                    temporary = ""
            except OSError:
                return
            finally:
                if temporary:
                    import shutil

                    shutil.rmtree(temporary, ignore_errors=True)
        self.plans[key] = (value[0], tree_key)
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        if save_snapshot(
            self.path,
            ("cpip-fast-install", self.entries, self.plans),
        ):
            self.dirty = False


class FastCandidate(PureWheelCandidate):
    """Lightweight candidate shared by local resolution and installation.

    Metadata is loaded only after resolution selects a filename candidate.  The

    remaining attributes intentionally match the cached-wheel installer

    boundary so local resolution does not need to materialize the much heavier

    general resolver candidate type before installing.

    """

    __slots__ = (
        "archive_members",
        "archive_modes",
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
        self.archive_modes: dict[str, int] | None = None

        self.dependencies = dependencies

        self.canonical_name = normalize_name(name)

        self.pure: bool | None = None

        self.provided_extras: frozenset[str] = EMPTY_FROZENSET

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

        option = consume_option(
            args,
            index,
            ("--find-links", "-f", "--target", "--cache-dir", "-r", "--requirement"),
        )
        if option is not None:
            name, value, index = option
            if name in ("--find-links", "-f"):
                options.find_links.append(value)
            elif name == "--target":
                options.target = value
            elif name == "--cache-dir":
                # The versioned directory every other writer uses.
                options.cache_dir = versioned_cache_dir(value)
            else:
                if not extend_requirements(options.requirements, value):
                    return None
            continue

        if token.startswith("-"):
            return None

        else:
            options.requirements.append(token)

        index += 1

    return options


def _remote_index_url() -> str | None:
    """Return the effective sole index, or decline non-default source shapes."""

    # Only the remote-exact-pin fast path reaches here; local/--no-index
    # installs never need config parsing, so keep it off their import cost.
    from cpip.cli.config import load_source_config

    config = load_source_config("install")

    if config.find_links or config.extra_index_urls or config.no_index:
        return None

    return config.index_url


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

    cache_dir = options.cache_dir or resolve_cache_dir()

    index_url = _remote_index_url()

    if index_url is None:
        return None

    keyed = exact_install_plan_key_from_strings(
        tuple(options.requirements),
        (
            REMOTE_EXACT_CONTEXT,
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

    key, roots = keyed

    plan = load_cached_install_plan(cache_dir, key)

    if plan is None:
        return None

    installed = install_wheels_from_archive_cache(
        tuple(
            (
                candidate.path,
                candidate.canonical_name in roots,
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
    """Reject wheel members that would escape the install target.

    This is a purely lexical check, deliberately weaker than
    ``install.wheel_archive.validate_member_parts`` plus the resolved-parent
    containment test the staged installer performs. It is sound here only
    because ``install_resolved_pure_wheels`` refuses any target that is not
    empty, so no attacker-controlled symlink can already sit on the
    destination path, and because every member is written as a regular file
    rather than reproduced as a symlink.

    If that emptiness precondition is ever relaxed, this check is no longer
    sufficient and the caller must adopt the resolving check instead.
    """

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


def requested_roots(requirements: Iterable[str]) -> set[str]:
    """Return the canonical names named directly on the command line."""

    return {
        normalize_name(
            value.partition("[")[0]
            .split("==", 1)[0]
            .split(">", 1)[0]
            .split("<", 1)[0]
            .strip(),
        )
        for value in requirements
    }


def is_purelib(wheel_text: str) -> bool:
    """Report whether a ``WHEEL`` file declares a pure-Python root."""

    return any(
        line.casefold().strip() == "root-is-purelib: true"
        for line in wheel_text.splitlines()
    )


def report_plan(
    find_links: Sequence[str],
    candidates: Sequence[FastCandidate],
) -> None:
    print(f"Looking in links: {', '.join(find_links)}")

    if candidates:
        print(
            "Installing collected packages: "
            + ", ".join(candidate.name for candidate in candidates),
        )


def report_installed(candidates: Sequence[FastCandidate]) -> None:
    if not candidates:
        return

    print(
        "Successfully installed "
        + " ".join(f"{candidate.name}-{candidate.version}" for candidate in candidates),
    )


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
    parsed = parse_wheel_filename_core(path)
    if parsed is None or version_key(parsed[1]) is None:
        return None
    return parsed


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
            candidate.archive_modes = archive.modes

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

    pure = is_purelib(wheel)

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

        bucket = candidates_by_name.get(candidate.canonical_name)
        if bucket is None:
            bucket = []
            candidates_by_name[candidate.canonical_name] = bucket
        bucket.append(candidate)

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

    target = os.path.abspath(target)

    separator = os.sep

    # Load-bearing for member safety, not just for correctness: an empty target
    # is what lets this path use the lexical `is_safe_member` check instead of
    # resolving each destination. See that function's docstring.
    if not target_is_empty(target):
        return False

    prepared: list[
        tuple[str, bool, bool, list[tuple[str, str, bytes, int | None]]]
    ] = []

    destinations: set[str] = set()

    for candidate in candidates:
        try:
            with open(os.fspath(candidate.path), "rb") as wheel_file:
                # Members pre-read by the resolver come with their modes;
                # a member table without modes would leave archive.modes
                # empty, so read the directory again instead.
                members = getattr(candidate, "archive_members", None)

                modes = getattr(candidate, "archive_modes", None)

                if modes is None:
                    members = None

                archive = WheelArchive(wheel_file, members=members, modes=modes)

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

                if not is_purelib(wheel_text):
                    return False

                dist_info = wheel_members[0].rsplit("/", 1)[0]

                # archive.modes is a plain dict, safe to read after the
                # archive's file handle closes -- unlike zipfile, this
                # low-level reader never restores permission bits itself,
                # so an executable regular file bundled directly in the
                # package tree (outside .data/entry_points, which are
                # rejected above) needs its mode carried through explicitly.
                modes_for_wheel = [
                    mode_from_external_attr(archive.modes[name]) for name in names
                ]

                members = [
                    (
                        destination,
                        directory,
                        contents,
                        mode,
                    )
                    for destination, directory, contents, mode in zip(
                        destinations_for_wheel,
                        directories_for_wheel,
                        contents,
                        modes_for_wheel,
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
                    for destination, _, contents, _ in members
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

            # A wheel may ship its own RECORD, in which case extraction below
            # already added it to the rollback list.  Noting it here keeps the
            # duplicate check off ``created_files``, which grows to one entry
            # per installed file and so made the check cost O(wheels x files).
            record_is_member = False

            if not reuse_record:
                import base64
                import csv
                import hashlib

            for destination, directory, contents, mode in members:
                if directory not in created_directories:
                    os.makedirs(directory, exist_ok=True)

                    created_directories.add(directory)

                with open(destination, "wb", buffering=0) as output:
                    output.write(contents)

                if mode is not None:
                    os.chmod(destination, mode)

                created_files.append(destination)

                if not reuse_record:
                    relative = os.path.relpath(destination, target).replace(
                        os.sep,
                        "/",
                    )

                    if relative == record_relative:
                        record_is_member = True
                    else:
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

                if not record_is_member:
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


def target_is_empty(target: str) -> bool:
    """Report whether ``target`` is an empty directory or does not exist."""

    if os.path.isdir(target):
        try:
            with os.scandir(target) as entries:
                return not any(entries)

        except OSError:
            return False

    return not os.path.exists(target)


def _install_cached_tree(tree: str, target: str) -> bool:
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

    if not target_is_empty(options.target):
        return None

    metadata_cache = None

    if options.cache_dir is not None:
        metadata_cache = FastInstallMetadataCache(options.cache_dir)

    candidates = resolve_simple_wheelhouse(
        options.find_links,
        options.requirements,
        metadata_cache,
    )

    if candidates is None:
        return None

    roots = requested_roots(options.requirements)

    if not options.quiet:
        report_plan(options.find_links, candidates)

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

    if not options.quiet:
        report_installed(candidates)

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
        or target_is_empty(options.target)
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

    metadata_cache = None

    if options.cache_dir is not None:
        metadata_cache = FastInstallMetadataCache(options.cache_dir)

    candidates = resolve_simple_wheelhouse(
        options.find_links,
        options.requirements,
        metadata_cache,
    )

    if candidates is None:
        return None

    if (
        options.upgrade
        and not options.ignore_installed
        and any(candidate.dependencies for candidate in candidates)
    ):
        return None

    if options.cache_dir is not None:
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

    roots = requested_roots(options.requirements)

    if not options.quiet:
        report_plan(options.find_links, candidates)

    requests = tuple(
        (candidate.path, candidate.canonical_name in roots, None)
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
        from cpip.install.wheel_transaction import install_wheels_transactionally

        wheel_candidates = [
            WheelCandidate(
                name=candidate.name,
                version=Version(str(candidate.version)),
                path=candidate.path,
                dependencies=(),
                provided_extras=EMPTY_FROZENSET,
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

    if not options.quiet:
        report_installed(candidates)

    if metadata_cache is not None:
        metadata_cache.flush()

    return 0
