"""Archive-backed batch installer: clone cached wheel trees into a target.

Consumes the immutable, validated trees the archive cache
(:mod:`cpip.install.wheel_archive_cache`) already extracted and unpacked, and
performs the batch clone/relocate/finalize/atomic-swap sequence that installs
them into a real target directory.
"""

from __future__ import annotations

import csv
import functools
import errno
import io
import os
import shutil
from typing import TYPE_CHECKING

from cpip.core.errors import InstallationError
from cpip.install.wheel_archive import record_metadata_internal, validate_member_parts
from cpip.install.wheel_archive_cache import INSTALL_WORKERS, prepare_cached_wheels
from cpip.install.wheel_scripts import (
    entry_point_scripts,
    generate_entry_point_files,
    rewrite_shebang,
)
from cpip.platform.clone import clone_path

if TYPE_CHECKING:
    from cpip.build.metadata import InstalledMetadataDistribution
    from cpip.core.direct_url import DirectUrl
    from cpip.install.target import InstallTarget
    from cpip.install.wheel_archive_cache import (
        CachedWheelArchive,
        InstallCandidate,
        WheelInstallCandidate,
        WheelRequest,
    )
    from cpip.install.wheel_state import InstalledWheelDistribution


class _WheelInstallPlan:
    __slots__ = ("archive", "candidate", "direct_url", "requested", "scripts")

    def __init__(
        self,
        archive: CachedWheelArchive,
        candidate: WheelInstallCandidate,
        *,
        requested: bool,
        direct_url: DirectUrl | None,
        scripts: dict[str, tuple[str, bool]],
    ) -> None:
        self.archive = archive

        self.candidate = candidate

        self.requested = requested

        self.direct_url = direct_url

        self.scripts = scripts


class _DestinationNode:
    """Typed prefix tree for detecting colliding wheel destinations."""

    __slots__ = ("children", "owner")

    def __init__(self) -> None:
        self.children: dict[str, _DestinationNode] = {}

        self.owner: int | None = None


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(path)))


def _internal_comparison_path(path: str) -> str:
    """Normalize a validated target path without resolving every parent."""

    return os.path.normcase(os.path.normpath(path))


def _eligible_target(target: InstallTarget, cache_dir: str) -> str | None:
    root = _normalized_path(target.purelib)

    if os.path.lexists(root) and (not os.path.isdir(root) or os.path.islink(root)):
        return None

    if any(
        _normalized_path(path) != root
        for path in (target.platlib, target.headers, target.data)
    ):
        return None

    expected_scripts = _normalized_path(
        os.path.join(root, "Scripts" if os.name == "nt" else "bin"),
    )

    if _normalized_path(target.scripts) != expected_scripts:
        return None

    cache = _normalized_path(cache_dir)

    try:
        if os.path.commonpath((cache, root)) == root:
            return None

    except ValueError:
        pass

    return root


@functools.lru_cache(maxsize=65536)
def _mapped_parts(relative: str) -> tuple[str, ...]:
    """Where a wheel member lands in the target, as path parts.

    A pure function of the member path, memoized: the same members recur
    across the plan, the staged tree and the RECORD of every install.
    """
    parts = validate_member_parts(relative)

    if not parts:
        raise InstallationError(f"wheel member has an empty path: {relative!r}")

    if not parts[0].endswith(".data"):
        return parts

    if len(parts) < 3 or parts[1] not in {
        "purelib",
        "platlib",
        "scripts",
        "data",
        "headers",
    }:
        raise InstallationError(f"invalid wheel data path: {relative}")

    if parts[1] == "scripts":
        return ("Scripts" if os.name == "nt" else "bin", *parts[2:])

    return parts[2:]


def _normalized_destination(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(os.path.normcase(part) for part in parts)


def _compiled_parts(mapped: tuple[str, ...]) -> tuple[str, ...] | None:
    """Where byte-compiling ``mapped`` lands its ``.pyc``, as path parts, or
    ``None`` when ``_compile_members`` would not compile this member.

    Uses the interpreter's own ``cache_from_source`` so the reserved and
    preflighted path is exactly the one ``compileall`` writes.
    """
    if not mapped[-1].endswith(".py") or mapped[0] in {"bin", "Scripts"}:
        return None

    import importlib.util

    compiled = importlib.util.cache_from_source("/".join(mapped))

    return tuple(compiled.split("/"))


def _reserve_destination(
    trie: _DestinationNode,
    parts: tuple[str, ...],
    owner: int,
    candidate: WheelInstallCandidate,
    *,
    allow_same_owner: bool = False,
) -> None:
    node = trie

    normalized = _normalized_destination(parts)

    for part in normalized:
        if node.owner is not None:
            raise InstallationError(
                f"Cannot install {candidate.canonical_name}: "
                f"duplicate installation destination: {'/'.join(parts)}",
            )

        child = node.children.get(part)

        if child is None:
            child = _DestinationNode()

            node.children[part] = child

        node = child

    terminal = node.owner

    has_children = bool(node.children)

    if (
        terminal is not None and not (allow_same_owner and terminal == owner)
    ) or has_children:
        raise InstallationError(
            f"Cannot install {candidate.canonical_name}: "
            f"duplicate installation destination: {'/'.join(parts)}",
        )

    node.owner = owner


def _build_plans(
    requests: tuple[WheelRequest, ...],
    candidates: tuple[WheelInstallCandidate, ...],
    archives: tuple[CachedWheelArchive, ...],
    *,
    pycompile: bool = False,
) -> tuple[_WheelInstallPlan, ...]:
    trie = _DestinationNode()

    plans: list[_WheelInstallPlan] = []

    for owner, (request, candidate, archive) in enumerate(
        zip(requests, candidates, archives, strict=True),
    ):
        for entry in archive.entries:
            mapped = _mapped_parts(entry[0])

            _reserve_destination(trie, mapped, owner, candidate)

            # A generated .pyc shares the trie so a wheel that also ships it
            # as a member, or a second wheel producing the same path, is
            # rejected here rather than overwritten during finalization.
            if pycompile and (compiled := _compiled_parts(mapped)) is not None:
                _reserve_destination(trie, compiled, owner, candidate)

        scripts = entry_point_scripts(
            os.path.join(archive.tree, archive.dist_info, "entry_points.txt"),
        )

        for name in scripts:
            if os.path.basename(name) != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory",
                )

            for generated in (name, f"{name}-script.py", f"{name}.exe"):
                _reserve_destination(
                    trie,
                    ("Scripts" if os.name == "nt" else "bin", generated),
                    owner,
                    candidate,
                    allow_same_owner=True,
                )

        plans.append(
            _WheelInstallPlan(
                archive,
                candidate,
                requested=request[1],
                direct_url=request[2],
                scripts=scripts,
            ),
        )

    return tuple(plans)


def _merge_move(source: str, destination: str) -> None:
    if not os.path.lexists(source):
        return

    if not os.path.lexists(destination):
        os.rename(source, destination)

        return

    if not (
        os.path.isdir(source)
        and not os.path.islink(source)
        and os.path.isdir(destination)
        and not os.path.islink(destination)
    ):
        raise FileExistsError(destination)

    with os.scandir(source) as entries:
        names = tuple(entry.name for entry in entries)

    for name in names:
        _merge_move(
            os.path.join(source, name),
            os.path.join(destination, name),
        )

    os.rmdir(source)


def _relocate_data(stage: str, archive: CachedWheelArchive) -> None:
    data_roots = {
        parts[0]
        for relative, _, _, _ in archive.entries
        if relative.partition("/")[0].endswith(".data")
        and (parts := validate_member_parts(relative))
    }

    for data_root in data_roots:
        root = os.path.join(stage, data_root)

        for scheme in ("purelib", "platlib", "data", "headers", "scripts"):
            source = os.path.join(root, scheme)

            destination = (
                os.path.join(stage, "Scripts" if os.name == "nt" else "bin")
                if scheme == "scripts"
                else stage
            )

            _merge_move(source, destination)

        if os.path.lexists(root):
            shutil.rmtree(root)


def _write_new_file(path: str, contents: bytes) -> tuple[str, str]:
    """Write ``contents`` as a new regular file; returns its RECORD row's
    hash and size, computed from the bytes in hand rather than read back."""

    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)

        else:
            os.unlink(path)

    except FileNotFoundError:
        pass

    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "wb") as file:
        file.write(contents)

    return record_metadata_internal(contents)


def _rewrite_metadata(
    path: str,
    candidate: WheelInstallCandidate,
) -> tuple[str, str] | None:
    """Normalize METADATA's Name; returns the rewritten file's RECORD hash
    and size, or ``None`` when the file was already normalized."""

    with open(path, "rb") as file:
        contents = file.read()

    lines = contents.decode("utf-8").splitlines(keepends=True)

    rewritten = contents

    for index, line in enumerate(lines):
        if line.lower().startswith("name:"):
            ending = "\n" if line.endswith("\n") else ""

            lines[index] = f"Name: {candidate.name.lower()}{ending}"

            rewritten = "".join(lines).encode("utf-8")

            break

    if rewritten != contents:
        with open(path, "wb") as file:
            file.write(rewritten)

        return record_metadata_internal(rewritten)

    return None


def _file_metadata(path: str) -> tuple[str, str]:
    with open(path, "rb") as file:
        return record_metadata_internal(file.read())


def _compile_members(
    stage: str,
    archive: CachedWheelArchive,
) -> list[tuple[str, str, str]]:
    """Byte-compile the wheel's ``.py`` members in the staged tree and return
    their ``.pyc`` RECORD rows, the way the transactional route records them.

    Scripts under ``bin``/``Scripts`` are not modules and are left alone.
    """

    # Deferred: compileall (and importlib.util) are needed only here.
    import compileall
    import importlib.util

    rows: list[tuple[str, str, str]] = []

    for relative, _, _, _ in archive.entries:
        mapped = _mapped_parts(relative)

        if not mapped[-1].endswith(".py") or mapped[0] in {"bin", "Scripts"}:
            continue

        path = os.path.join(stage, "/".join(mapped))

        if not compileall.compile_file(path, force=True, quiet=1):
            continue

        compiled = importlib.util.cache_from_source(path)

        compiled_relative = os.path.relpath(compiled, stage).replace(os.sep, "/")

        rows.append((compiled_relative, *_file_metadata(compiled)))

    return rows


def _finalize_wheel(
    stage: str,
    plan: _WheelInstallPlan,
    *,
    script_executable: str | None,
    pycompile: bool = False,
) -> None:
    archive = plan.archive

    candidate = plan.candidate

    dist_info = archive.dist_info

    dist_info_root = os.path.join(stage, dist_info)

    metadata_path = os.path.join(dist_info_root, "METADATA")

    metadata_rewritten = _rewrite_metadata(metadata_path, candidate)

    script_members: set[str] = set()

    for relative, _, _, _ in archive.entries:
        # Only a ``<name>.data/scripts/...`` member can be a script; every
        # entry was validated when the archive was extracted.
        if "/scripts/" not in relative:
            continue

        parts = validate_member_parts(relative)

        if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] == "scripts":
            mapped = _mapped_parts(relative)

            path = os.path.join(stage, "/".join(mapped))

            rewrite_shebang(path, script_executable)

            script_members.add("/".join(mapped))

    installer = os.path.join(dist_info_root, "INSTALLER")

    requested = os.path.join(dist_info_root, "REQUESTED")

    direct_url = os.path.join(dist_info_root, "direct_url.json")

    installer_metadata = _write_new_file(installer, b"cpip\n")

    requested_metadata = _write_new_file(requested, b"") if plan.requested else None

    if not plan.requested:
        try:
            os.unlink(requested)

        except FileNotFoundError:
            pass

    direct_url_metadata = (
        _write_new_file(direct_url, plan.direct_url.to_json().encode("utf-8"))
        if plan.direct_url is not None
        else None
    )

    if plan.direct_url is None:
        try:
            os.unlink(direct_url)

        except FileNotFoundError:
            pass

    generated_names = {
        generated
        for name in plan.scripts
        for generated in (name, f"{name}-script.py", f"{name}.exe")
    }

    scripts_root = os.path.join(stage, "Scripts" if os.name == "nt" else "bin")

    for name in generated_names:
        path = os.path.join(scripts_root, name)

        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)

            else:
                os.unlink(path)

        except FileNotFoundError:
            pass

    generated_paths: list[str] = []

    if plan.scripts:
        # Deferred: tempfile only when this route installs.
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".cpip-scripts-", dir=stage) as temp:
            generated = generate_entry_point_files(
                plan.scripts,
                temp,
                script_executable,
            )

            os.makedirs(scripts_root, exist_ok=True)

            for source, _ in generated:
                destination = os.path.join(scripts_root, os.path.basename(source))

                os.rename(source, destination)

                generated_paths.append(destination)

    compiled_rows = _compile_members(stage, archive) if pycompile else ()

    managed = {
        f"{dist_info}/INSTALLER",
        f"{dist_info}/REQUESTED",
        f"{dist_info}/direct_url.json",
    }

    record_relative = f"{dist_info}/RECORD"

    rows: list[tuple[str, str, str]] = []

    for relative, digest, size, _ in archive.entries:
        mapped = _mapped_parts(relative)

        installed_relative = "/".join(mapped)

        if installed_relative in managed:
            continue

        if mapped[0] in {"bin", "Scripts"} and mapped[-1] in generated_names:
            continue

        if installed_relative == record_relative:
            rows.append((installed_relative, "", ""))

            continue

        path = os.path.join(stage, installed_relative)

        if installed_relative == f"{dist_info}/METADATA" and metadata_rewritten:
            digest, size = metadata_rewritten

        elif installed_relative in script_members:
            digest, size = _file_metadata(path)

        rows.append((installed_relative, digest, size))

    rows.append((f"{dist_info}/INSTALLER", *installer_metadata))

    if requested_metadata is not None:
        rows.append((f"{dist_info}/REQUESTED", *requested_metadata))

    if direct_url_metadata is not None:
        rows.append((f"{dist_info}/direct_url.json", *direct_url_metadata))

    for path in generated_paths:
        generated_metadata = _file_metadata(path)

        rows.append(
            (
                "/".join(
                    (
                        "Scripts" if os.name == "nt" else "bin",
                        os.path.basename(path),
                    ),
                ),
                *generated_metadata,
            ),
        )

    rows.extend(compiled_rows)

    rows.sort()

    record = io.StringIO(newline="")

    csv.writer(record).writerows(rows)

    _write_new_file(
        os.path.join(dist_info_root, "RECORD"),
        record.getvalue().encode("utf-8"),
    )


def _plan_destinations(
    root: str, plan: _WheelInstallPlan, *, pycompile: bool = False
) -> set[str]:
    destinations: set[str] = set()

    for relative, _, _, _ in plan.archive.entries:
        mapped = _mapped_parts(relative)

        destinations.add(os.path.join(root, "/".join(mapped)))

        # The preflight must see the generated .pyc too, so an unowned file
        # already at that path declines this route rather than being
        # overwritten in the clone after the check has passed.
        if pycompile and (compiled := _compiled_parts(mapped)) is not None:
            destinations.add(os.path.join(root, "/".join(compiled)))

    scripts_root = os.path.join(root, "Scripts" if os.name == "nt" else "bin")

    for name in plan.scripts:
        destinations.update(
            os.path.join(scripts_root, generated)
            for generated in (name, f"{name}-script.py", f"{name}.exe")
        )

    return destinations


def _stage_path(root: str, stage: str, path: str) -> str | None:
    try:
        relative = os.path.relpath(path, root)

    except ValueError:
        return None

    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None

    return os.path.join(stage, relative)


def _remove_stage_files(stage: str, paths: set[str]) -> None:
    """Remove an owned file batch and prune each parent at most once."""

    parents: set[str] = set()

    for path in sorted(paths, key=lambda item: item.count(os.sep), reverse=True):
        try:
            os.unlink(path)

        except FileNotFoundError:
            pass

        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EISDIR, errno.EPERM} or not (
                os.path.isdir(path) and not os.path.islink(path)
            ):
                raise

            try:
                shutil.rmtree(path)

            except FileNotFoundError:
                pass

        parent = os.path.dirname(path)

        while parent != stage and parent.startswith(stage + os.sep):
            parents.add(parent)

            parent = os.path.dirname(parent)

    for parent in sorted(
        parents,
        key=lambda item: item.count(os.sep),
        reverse=True,
    ):
        try:
            os.rmdir(parent)

        except OSError:
            pass


def install_wheels_from_archive_cache(
    requests: tuple[WheelRequest, ...],
    candidates: tuple[InstallCandidate, ...],
    *,
    target: InstallTarget,
    cache_dir: str,
    script_executable: str | None = None,
    force: bool = False,
    preserve_existing: bool = False,
    report: bool = True,
    pycompile: bool = False,
) -> tuple[InstallCandidate, ...] | None:
    """Install into a self-contained target from unpacked archives.


    ``None`` means that the target or cache cannot use this optimization and

    the caller should retain the legacy transactional installation path.

    """

    root = _eligible_target(target, cache_dir)

    if root is None:
        return None

    try:
        archives = prepare_cached_wheels(candidates, cache_dir)

    except OSError:
        return None

    plans = _build_plans(requests, candidates, archives, pycompile=pycompile)

    parent = os.path.dirname(root)

    os.makedirs(parent, exist_ok=True)

    # Deferred: tempfile only when this route installs.
    import tempfile

    staging_parent = tempfile.mkdtemp(prefix=".cpip-install-", dir=parent)

    stage = os.path.join(staging_parent, "target")

    pool = None

    try:
        root_existed = os.path.isdir(root)

        active_plans = plans

        uninstalling: list[
            InstalledMetadataDistribution | InstalledWheelDistribution
        ] = []

        destinations_by_plan: dict[_WheelInstallPlan, set[str]] = {}

        if root_existed:
            from cpip.build.metadata import InstalledDistributionStore
            from cpip.install.wheel_state import (
                discover_installed_wheels,
                existing_paths,
            )

            clone_path(root, stage)

            names = {plan.candidate.canonical_name for plan in plans}

            existing = discover_installed_wheels((root,), names=names)

            if existing is None:
                existing = {
                    distribution.canonical_name: distribution
                    for distribution in InstalledDistributionStore(paths=[root]).iter(
                        names=names,
                    )
                }

            selected: list[_WheelInstallPlan] = []

            allowed_existing: set[str] = set()

            removals: set[str] = set()

            for plan in plans:
                distribution = existing.get(plan.candidate.canonical_name)

                if (
                    distribution is not None
                    and distribution.version == plan.candidate.version
                    and not force
                    and not preserve_existing
                ):
                    continue

                selected.append(plan)

                if distribution is None:
                    continue

                owned_paths, old_paths = existing_paths(distribution)

                allowed_existing.update(
                    normalized
                    for path in owned_paths
                    if (normalized := _internal_comparison_path(path))
                )

                destinations = _plan_destinations(root, plan, pycompile=pycompile)

                destinations_by_plan[plan] = destinations

                normalized_destinations = {
                    _internal_comparison_path(path) for path in destinations
                }

                removals.update(
                    old_paths
                    if not preserve_existing
                    else {
                        path
                        for path in owned_paths
                        if _internal_comparison_path(path) in normalized_destinations
                    }
                )

                uninstalling.append(distribution)

            active_plans = tuple(selected)

            if not active_plans:
                return candidates

            for plan in active_plans:
                destinations = destinations_by_plan.get(plan)

                if destinations is None:
                    destinations = _plan_destinations(root, plan, pycompile=pycompile)

                for destination in destinations:
                    if (
                        os.path.lexists(destination)
                        and _internal_comparison_path(destination)
                        not in allowed_existing
                    ):
                        return None

            staged_removals: set[str] = set()

            for path in removals:
                staged = _stage_path(root, stage, path)

                if staged is None:
                    return None

                staged_removals.add(staged)

            _remove_stage_files(stage, staged_removals)

        active_archives = tuple(plan.archive for plan in active_plans)

        if len(archives) >= 4 or len(plans) >= 4:
            # Deferred: concurrent.futures drags in logging and more, and the pure-wheel
            # fast path imports this module without ever starting a pool.
            from concurrent.futures import ThreadPoolExecutor

            pool = ThreadPoolExecutor(
                max_workers=min(
                    INSTALL_WORKERS,
                    max(len(active_archives), len(active_plans)),
                ),
            )

        try:
            if len(active_archives) >= 4 and pool is not None:
                tuple(
                    pool.map(
                        lambda archive: clone_path(archive.tree, stage),
                        active_archives,
                    )
                )

            else:
                for archive in active_archives:
                    clone_path(archive.tree, stage)

            for archive in active_archives:
                _relocate_data(stage, archive)

        except FileExistsError as exc:
            raise InstallationError(
                "duplicate installation destination while linking cached wheels",
            ) from exc

        if len(active_plans) >= 4 and pool is not None:

            def finalize(plan: _WheelInstallPlan) -> None:
                _finalize_wheel(
                    stage,
                    plan,
                    script_executable=script_executable,
                    pycompile=pycompile,
                )

            tuple(pool.map(finalize, active_plans))

        else:
            for plan in active_plans:
                _finalize_wheel(
                    stage,
                    plan,
                    script_executable=script_executable,
                    pycompile=pycompile,
                )

        if os.path.lexists(root) != root_existed:
            return None

        if root_existed:
            backup = os.path.join(staging_parent, "previous")

            os.rename(root, backup)

            try:
                os.rename(stage, root)

            except BaseException:
                os.rename(backup, root)

                raise

            shutil.rmtree(backup, ignore_errors=True)

        else:
            os.rename(stage, root)

        if report:
            for distribution in uninstalling:
                print(
                    f"Uninstalling {distribution.raw_name}-{distribution.raw_version}",
                )

                print(
                    f"Successfully uninstalled {distribution.raw_name}-{distribution.raw_version}",
                )

        return candidates

    finally:
        if pool is not None:
            pool.shutdown()

        shutil.rmtree(staging_parent, ignore_errors=True)
