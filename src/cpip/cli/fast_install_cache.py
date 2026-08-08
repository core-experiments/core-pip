"""Persistent metadata snapshot for the specialized local-wheel installer."""

from __future__ import annotations

import atexit
import hashlib
import marshal
import os
import stat
import tempfile
from collections.abc import Sequence

from cpip.core.marshal_cache import load_snapshot, save_snapshot
from cpip.platform.clone import clone_path

VERSION = 3

NAME = "fast-install-v3.marshal"

MAX_ENTRIES = 8_192

MAX_PLANS = 256

TREE_CACHE_BUCKET = "fast-install-trees-v1"

TREE_CACHE_FORMAT = 1


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
            or len(payload) != 4
            or payload[0] != "cpip-fast-install"
            or payload[1] != VERSION
            or not isinstance(payload[2], dict)
            or not isinstance(payload[3], dict)
        ):
            return

        for key, value in payload[2].items():
            metadata = self._coerce_metadata(value)

            if self._valid_identity(key) and metadata is not None:
                self.entries[key] = metadata  # ty: ignore[invalid-assignment]

        for key, value in payload[3].items():
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
            and len(value) in {2, 3}
            and isinstance(value[0], tuple)
            and all(isinstance(item, str) for item in value[0])
            and isinstance(value[1], bool)
        ):
            return None

        digest = value[2] if len(value) == 3 else None

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
            stat = os.stat(path)

        except OSError:
            return None

        return (
            os.path.abspath(path),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
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
                ("cpip-fast-install-tree", TREE_CACHE_FORMAT, key, value[0]),
            ),
        ).hexdigest()

        tree = self._tree_path(tree_key)

        if not os.path.isdir(tree):
            entry = os.path.dirname(tree)

            shard = os.path.dirname(entry)

            try:
                os.makedirs(shard, exist_ok=True)

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
            ("cpip-fast-install", VERSION, self.entries, self.plans),
        ):
            self.dirty = False
