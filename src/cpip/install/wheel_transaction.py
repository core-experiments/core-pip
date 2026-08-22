"""Transactional wheel installation.

This module is the migration boundary between wheel preparation and the
filesystem transaction engine. It deliberately does not invoke cpip again.
"""

from __future__ import annotations

import csv
import io
import os
import stat
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import nullcontext
from threading import Lock

from cpip.core.errors import InstallationError
from cpip.core.names import canonicalize_name
from cpip.core.wheel import (
    WheelCandidate,
    validate_wheel,
    validate_wheel_with_metadata,
    wheel_candidate,
    wheel_candidate_from_path,
)
from cpip.install.target import InstallTarget
from cpip.install.transaction import InstallTransaction, normalized_internal
from cpip.install.wheel_archive import (
    DestinationCache,
    ResolvedRoots,
    copy_member_with_metadata,
    MemberPaths,
    destination_internal_parts_text,
    record_metadata_internal,
    validate_member_parts,
    zip_mode,
)
from cpip.install.wheel_archive_cache import CachedWheelArchive
from cpip.install.wheel_archive_installer import install_wheels_from_archive_cache
from cpip.install.wheel_archive_runtime import CachedWheelInfo, open_wheel_archive
from cpip.install.wheel_scripts import (
    entry_point_scripts,
    rewrite_shebang,
    script_matches,
    script_text,
    write_windows_script,
)
from cpip.install.wheel_state import (
    InstalledTargetInventory,
    compiled_files,
    existing_paths,
)
from cpip.install.wheel_transaction_direct import (
    DIRECT_CONTENT_BATCH_LIMIT,
    direct_batch_preflight,
    install_wheels_directly,
)
from cpip.platform.clone import clone_path

TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.build.metadata import InstalledMetadataDistribution
    from cpip.core.direct_url import DirectUrl
    from cpip.install.wheel_state import InstalledWheelDistribution

    ExistingDistribution = InstalledMetadataDistribution | InstalledWheelDistribution

DIRECT_CONTENT_LIMIT = 64 * 1024
StagedEntry = tuple[str, str, str, int | None]


class ThreadSafePathCache:
    """Lock-guarded destination-path cache shared by parallel install workers."""

    def __init__(self) -> None:
        self.values: DestinationCache = {}
        self.lock = Lock()

    def get(self, key: tuple[str, str]) -> str | None:
        with self.lock:
            return self.values.get(key)

    def __setitem__(self, key: tuple[str, str], value: str) -> None:
        with self.lock:
            self.values[key] = value


def _target_has_distribution_metadata(target: InstallTarget) -> bool:
    """Check for installed metadata before importing metadata discovery code."""
    for root in target.library_roots:
        try:
            with os.scandir(root) as entries:
                if any(
                    entry.name.endswith((".dist-info", ".egg-info", ".egg-link"))
                    for entry in entries
                ):
                    return True
        except OSError:
            # An unreadable or transient root must use the authoritative
            # metadata scanner so installation does not silently skip an old
            # distribution.
            return True
    return False


class WheelInstaller:
    """Install wheels into one target using filesystem transactions."""

    def __init__(
        self,
        target: InstallTarget,
        *,
        pycompile: bool = True,
        force: bool = False,
        preserve_existing: bool = False,
        script_executable: str | None = None,
        target_inventory: InstalledTargetInventory | None = None,
    ) -> None:
        self.target = target
        self.pycompile = pycompile
        self.force = force
        self.preserve_existing = preserve_existing
        self.script_executable = script_executable
        self.target_inventory = target_inventory

    def install(
        self,
        path: str,
        *,
        candidate: WheelCandidate | None = None,
        requested: bool = False,
        direct_url: DirectUrl | None = None,
        transaction_sink: list[InstallTransaction] | None = None,
        existing: ExistingDistribution | None = None,
        lookup_existing: bool = True,
        validated_dist_info: str | None = None,
        destination_cache: DestinationCache | None = None,
        stage_root: str | None = None,
        transaction: InstallTransaction | None = None,
        direct: bool = False,
    ) -> WheelCandidate:
        return install_wheel_internal(
            path,
            target=self.target,
            candidate=candidate,
            pycompile=self.pycompile,
            requested=requested,
            force=self.force,
            preserve_existing=self.preserve_existing,
            direct_url=direct_url,
            script_executable=self.script_executable,
            transaction_sink=transaction_sink,
            existing=existing,
            lookup_existing=lookup_existing,
            validated_dist_info=validated_dist_info,
            destination_cache=destination_cache,
            stage_root=stage_root,
            transaction=transaction,
            direct=direct,
            target_inventory=self.target_inventory,
        )

    def validate_batch(
        self,
        paths: Iterable[str],
        *,
        validation_cache: dict[str, str] | None = None,
        destination_cache: DestinationCache | None = None,
    ) -> tuple[WheelCandidate, ...]:
        return validate_wheel_batch(
            paths,
            target=self.target,
            validation_cache=validation_cache,
            destination_cache=destination_cache,
        )


def install_wheel_internal(
    path: str,
    *,
    target: InstallTarget,
    candidate: WheelCandidate | None = None,
    pycompile: bool = True,
    requested: bool = False,
    force: bool = False,
    preserve_existing: bool = False,
    direct_url: DirectUrl | None = None,
    script_executable: str | None = None,
    transaction_sink: list[InstallTransaction] | None = None,
    existing: ExistingDistribution | None = None,
    lookup_existing: bool = True,
    validated_dist_info: str | None = None,
    destination_cache: DestinationCache | None = None,
    stage_root: str | None = None,
    transaction: InstallTransaction | None = None,
    direct: bool = False,
    target_inventory: InstalledTargetInventory | None = None,
) -> WheelCandidate:
    if candidate is None:
        # No pre-resolved candidate to reuse a cached layout from -- the
        # archive opened here is closed before extraction runs, so keeping
        # its layout would only misdirect open_wheel_archive() into the
        # slower zipfile.ZipFile fallback for no benefit.
        candidate = wheel_candidate_from_path(path, include_layout=False)
    if lookup_existing:
        if target_inventory is not None:
            existing = target_inventory.find(candidate.canonical_name)
        elif _target_has_distribution_metadata(target):
            from cpip.build.metadata import InstalledDistributionStore

            existing = InstalledDistributionStore(
                paths=[os.fspath(root) for root in target.library_roots],
            ).find(candidate.name)
        else:
            existing = None
    # A legacy (non-PEP 440) installed version is None and so never "the
    # same version": it is replaced.
    same_version = existing is not None and existing.version == candidate.version
    if same_version and not force and not preserve_existing:
        return candidate

    if existing is not None and (not same_version or force):
        print(f"Uninstalling {existing.raw_name}-{existing.raw_version}")
    if direct and transaction is None:
        raise ValueError("direct wheel installation needs a transaction")

    stage_context = (
        nullcontext(target.purelib)
        if direct
        else (
            tempfile.TemporaryDirectory(prefix="cpip-wheel-stage-")
            if stage_root is None
            else nullcontext(stage_root)
        )
    )
    with stage_context as temporary:
        stage_root_text = temporary
        purelib_text = target.purelib
        purelib_prefix = purelib_text.rstrip(os.sep) + os.sep

        def record_relative_path(destination_text: str) -> str:
            if destination_text.startswith(purelib_prefix):
                return destination_text[len(purelib_prefix) :]
            return os.path.relpath(destination_text, purelib_text)

        staged: list[StagedEntry] = []
        record_destination: str | None = None
        dist_info: str | None = None
        stage_directories: set[str] = set()
        resolved_directories = (
            destination_cache if destination_cache is not None else {}
        )
        resolved_roots: ResolvedRoots = target.resolved_roots_internal
        record_metadata: dict[str, tuple[str, str]] = {}
        direct_contents: dict[str, bytes] = {}
        direct_metadata: dict[str, tuple[str, str]] = {}
        direct_content_size = 0
        clone_sources: set[str] = set()

        def write_direct(
            destination: str,
            contents: bytes,
            mode: int | None = None,
        ) -> None:
            assert transaction is not None
            destination_text = destination
            os.makedirs(os.path.dirname(destination_text), exist_ok=True)
            transaction.record_created(destination)
            with open(destination_text, "wb") as file:
                file.write(contents)
            if mode is not None:
                os.chmod(destination, mode)

        with open_wheel_archive(path, candidate) as archive:
            if validated_dist_info is None:
                layout = getattr(candidate, "wheel_layout", None)
                if isinstance(layout, CachedWheelArchive):
                    validated_dist_info = layout.dist_info
                elif layout is not None:
                    validated_dist_info = layout[0]
                else:
                    validated_dist_info = validate_wheel(
                        archive,  # ty:ignore[invalid-argument-type]
                        os.path.basename(path)[:-4].split("-", 1)[0],
                    )
            wheel_record_metadata: dict[str, tuple[str, str]] = {}
            try:
                record_text = archive.read(f"{validated_dist_info}/RECORD").decode(
                    "utf-8",
                )
            except (KeyError, UnicodeDecodeError):
                pass
            else:
                for row in csv.reader(io.StringIO(record_text)):
                    if (
                        len(row) >= 3
                        and row[1].startswith("sha256=")
                        and row[2].isdigit()
                    ):
                        wheel_record_metadata[row[0]] = (row[1], row[2])
            member_paths = MemberPaths(
                target,
                stage_root_text,
                resolved_directories=resolved_directories,
                resolved_roots=resolved_roots,
            )
            for member in archive.infolist():
                if member.is_dir():
                    continue
                (
                    relative_parts,
                    source_text,
                    destination_text,
                    record_key,
                ) = member_paths.resolve(member.filename)
                relative_name = relative_parts[-1] if relative_parts else ""
                if relative_parts and relative_parts[0].endswith(".dist-info"):
                    dist_info = relative_parts[0]
                rewrite_metadata = (
                    relative_name == "METADATA" and candidate.name.isalpha()
                )
                script_member = (
                    len(relative_parts) >= 2 and relative_parts[-2] == "scripts"
                )
                is_record = relative_name == "RECORD" and bool(relative_parts)
                direct_content = (
                    getattr(member, "source_path", None) is None
                    and not rewrite_metadata
                    and not script_member
                    and not is_record
                    and member.file_size <= DIRECT_CONTENT_LIMIT
                    and direct_content_size + member.file_size
                    <= DIRECT_CONTENT_BATCH_LIMIT
                    and (not pycompile or os.path.splitext(relative_name)[1] != ".py")
                    and relative_name != "entry_points.txt"
                )
                if not direct and not direct_content:
                    source_parent_text = os.path.dirname(source_text)
                    if source_parent_text not in stage_directories:
                        os.makedirs(source_parent_text, exist_ok=True)
                        stage_directories.add(source_parent_text)
                if rewrite_metadata or script_member:
                    contents = archive.read(member)  # ty:ignore[invalid-argument-type]
                elif is_record:
                    contents = None
                elif direct_content:
                    contents = archive.read(member)  # ty:ignore[invalid-argument-type]
                    direct_content_size += len(contents)
                else:
                    metadata = wheel_record_metadata.get(record_key)
                    if metadata is not None and metadata[1] != str(member.file_size):
                        metadata = None
                    if direct:
                        assert transaction is not None
                        os.makedirs(os.path.dirname(destination_text), exist_ok=True)
                        transaction.record_created(destination_text)
                    cached_member = (
                        member if isinstance(member, CachedWheelInfo) else None
                    )
                    if (
                        cached_member is not None
                        and not direct
                        and relative_name != "entry_points.txt"
                        and (
                            not pycompile or os.path.splitext(relative_name)[1] != ".py"
                        )
                    ):
                        source_text = cached_member.source_path
                        clone_sources.add(source_text)
                        metadata = cached_member.record_metadata
                    elif cached_member is not None and not direct:
                        clone_path(cached_member.source_path, source_text)
                        metadata = cached_member.record_metadata
                    else:
                        metadata = copy_member_with_metadata(
                            archive,  # ty:ignore[invalid-argument-type]
                            member,  # ty:ignore[invalid-argument-type]
                            destination_text if direct else source_text,
                            metadata=metadata,
                        )
                    if direct:
                        direct_metadata[destination_text] = metadata
                    else:
                        record_metadata[source_text] = metadata
                    contents = None
                if rewrite_metadata:
                    assert contents is not None
                    lines = contents.decode("utf-8").splitlines(keepends=True)
                    for index, line in enumerate(lines):
                        if line.lower().startswith("name:"):
                            ending = "\n" if line.endswith("\n") else ""
                            lines[index] = f"Name: {candidate.name.lower()}{ending}"
                            contents = "".join(lines).encode("utf-8")
                            break
                if direct and contents is not None and not direct_content:
                    write_direct(destination_text, contents, zip_mode(member))  # ty:ignore[invalid-argument-type]
                if contents is not None and not direct_content and not direct:
                    with open(source_text, "wb") as file:
                        file.write(contents)
                if script_member:
                    if direct:
                        raise InstallationError(
                            "direct wheel installation cannot contain scripts",
                        )
                    rewrite_shebang(source_text, script_executable)
                elif contents is not None:
                    metadata = wheel_record_metadata.get(record_key)
                    if metadata is None or metadata[1] != str(len(contents)):
                        metadata = record_metadata_internal(contents)
                    if direct_content:
                        if direct:
                            write_direct(destination_text, contents, zip_mode(member))  # ty:ignore[invalid-argument-type]
                        else:
                            direct_contents[destination_text] = contents
                        direct_metadata[destination_text] = metadata
                    elif direct:
                        direct_metadata[destination_text] = metadata
                    else:
                        record_metadata[source_text] = metadata
                mode = zip_mode(member)  # ty:ignore[invalid-argument-type]
                staged.append((source_text, destination_text, destination_text, mode))
                if relative_name == "RECORD" and relative_parts:
                    record_destination = destination_text

        if dist_info is None or record_destination is None:
            raise InstallationError(f"Wheel {path} has no valid dist-info metadata")
        record_destination_text = record_destination

        managed_metadata = {
            os.path.join(purelib_text, dist_info, "INSTALLER"),
            os.path.join(purelib_text, dist_info, "REQUESTED"),
            os.path.join(purelib_text, dist_info, "direct_url.json"),
        }
        staged = [
            item
            for item in staged
            if item[2] not in managed_metadata or item[2] == record_destination_text
        ]
        staged_destinations = {destination_text for _, _, destination_text, _ in staged}
        for destination in tuple(direct_contents):
            if destination not in staged_destinations:
                direct_contents.pop(destination, None)
                direct_metadata.pop(destination, None)

        dist_info_stage = os.path.join(stage_root_text, dist_info)
        installer_source = os.path.join(dist_info_stage, "INSTALLER")
        installer_destination = os.path.join(target.purelib, dist_info, "INSTALLER")
        installer_contents = b"cpip\n"
        if direct:
            write_direct(installer_destination, installer_contents)
        else:
            direct_contents[installer_destination] = installer_contents
        direct_metadata[installer_destination] = record_metadata_internal(
            b"cpip\n",
        )
        staged.append(
            (
                installer_source,
                installer_destination,
                installer_destination,
                None,
            ),
        )

        requested_destination = os.path.join(target.purelib, dist_info, "REQUESTED")
        if requested:
            requested_source = os.path.join(dist_info_stage, "REQUESTED")
            if direct:
                write_direct(requested_destination, b"")
            else:
                direct_contents[requested_destination] = b""
            direct_metadata[requested_destination] = record_metadata_internal(b"")
            staged.append(
                (
                    requested_source,
                    requested_destination,
                    requested_destination,
                    None,
                ),
            )

        if direct_url is not None:
            direct_url_source = os.path.join(dist_info_stage, "direct_url.json")
            with open(direct_url_source, "w", encoding="utf-8") as file:
                file.write(direct_url.to_json())
            staged.append(
                (
                    direct_url_source,
                    os.path.join(target.purelib, dist_info, "direct_url.json"),
                    os.path.join(target.purelib, dist_info, "direct_url.json"),
                    None,
                ),
            )

        scripts = (
            {}
            if direct
            else entry_point_scripts(
                os.path.join(stage_root_text, dist_info, "entry_points.txt"),
            )
        )
        if scripts:
            script_destinations = {
                os.path.join(target.scripts, generated)
                for name in scripts
                for generated in (name, f"{name}-script.py", f"{name}.exe")
            }
            staged = [item for item in staged if item[1] not in script_destinations]
        script_stage = os.path.join(stage_root_text, ".cpip-scripts")
        script_maker_type = None
        script_modes: dict[str, int] = {}
        if scripts:
            os.makedirs(script_stage, exist_ok=True)
            try:
                from distlib.scripts import ScriptMaker
            except ImportError:
                pass
            else:
                script_maker_type = ScriptMaker
        for name, (target_ref, gui) in scripts.items():
            if os.path.basename(name) != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory",
                )
            if script_maker_type is None:
                if os.name == "nt":
                    source = os.path.join(script_stage, f"{name}.exe")
                    write_windows_script(
                        source,
                        script_text(target_ref, script_executable),
                        gui=gui,
                    )
                else:
                    source = os.path.join(script_stage, name)
                    with open(source, "w", encoding="utf-8") as file:
                        file.write(script_text(target_ref, script_executable))
                        file.flush()
                        mode = (
                            os.fstat(file.fileno()).st_mode
                            | stat.S_IXUSR
                            | stat.S_IXGRP
                            | stat.S_IXOTH
                        )
                    os.chmod(source, mode)
                    script_modes[source] = mode
            else:
                maker = script_maker_type(None, script_stage)
                maker.clobber = True
                maker.variants = {""}
                if script_executable is not None:
                    maker.executable = script_executable
                maker.make(f"{name} = {target_ref}", options={"gui": gui})
                if os.name == "nt":
                    # Keep the script-text form for callers that inspect the
                    # generated script path. Windows execution uses the EXE.
                    source = os.path.join(script_stage, name)
                    with open(source, "w", encoding="utf-8") as file:
                        file.write(script_text(target_ref, script_executable))
                        file.flush()
                        mode = (
                            os.fstat(file.fileno()).st_mode
                            | stat.S_IXUSR
                            | stat.S_IXGRP
                            | stat.S_IXOTH
                        )
                    os.chmod(source, mode)
                    script_modes[source] = mode

        if scripts:
            with os.scandir(script_stage) as entries:
                script_sources = tuple(entries)
            for entry in script_sources:
                source = os.path.join(script_stage, entry.name)
                destination = os.path.join(target.scripts, os.path.basename(source))
                mode = script_modes.get(source)
                if mode is None:
                    mode = os.stat(source).st_mode
                staged.append(
                    (
                        source,
                        destination,
                        destination,
                        mode,
                    ),
                )

        if pycompile:
            staged.extend(compiled_files(stage_root_text, staged))

        record_rows = []
        for source, destination, destination_text, _ in staged:
            if destination_text == record_destination_text:
                record_rows.append((record_relative_path(destination_text), "", ""))
                continue
            metadata = direct_metadata.get(destination_text)
            if metadata is None:
                metadata = record_metadata.get(source)
            if metadata is None:
                with open(source, "rb") as file:
                    metadata = record_metadata_internal(file.read())
            record_rows.append(
                (
                    record_relative_path(destination_text),
                    metadata[0],
                    metadata[1],
                ),
            )
        record_rows.sort()
        record_file = io.StringIO(newline="")
        csv.writer(record_file).writerows(record_rows)
        record_contents = record_file.getvalue().encode("utf-8")
        if direct:
            write_direct(record_destination, record_contents)
        else:
            direct_contents[record_destination] = record_contents
        direct_metadata[record_destination] = record_metadata_internal(
            record_contents,
        )

        owned_paths, old_paths = existing_paths(existing)
        if preserve_existing and existing is not None:
            old_paths = set()
        old_path_texts = set(old_paths)
        scripts_text = os.fspath(target.scripts)
        for _, destination, destination_text, _ in staged:
            if (
                not direct and os.path.dirname(destination_text) == scripts_text
            ) and script_matches(destination, scripts):
                owned_paths.add(destination_text)
        new_destinations = {destination_text for _, _, destination_text, _ in staged}
        active_transaction = transaction or InstallTransaction(owned_paths=owned_paths)
        if direct and active_transaction is not transaction:
            raise ValueError("direct wheel installation needs the shared transaction")
        if transaction is not None:
            transaction.owned.update(normalized_internal(path) for path in owned_paths)
        if not direct:
            for source, destination, destination_text, mode in staged:
                contents = direct_contents.get(destination_text)
                if contents is not None:
                    active_transaction.add_contents(
                        destination_text,
                        contents,
                        mode=mode,
                    )
                else:
                    operation = (
                        active_transaction.add_clone
                        if source in clone_sources
                        else active_transaction.add
                    )
                    operation(source, destination_text, mode=mode)
            for old_path in old_path_texts - new_destinations:
                active_transaction.delete(old_path)
            if transaction is None:
                active_transaction.commit(finalize=transaction_sink is None)
        if transaction_sink is not None and transaction is None:
            transaction_sink.append(active_transaction)
        if existing is not None and (not same_version or force):
            print(
                f"Successfully uninstalled {existing.raw_name}-{existing.raw_version}",
            )
    return candidate


def validate_wheel_batch(
    paths: Iterable[str],
    *,
    target: InstallTarget,
    validation_cache: dict[str, str] | None = None,
    destination_cache: DestinationCache | None = None,
) -> tuple[WheelCandidate, ...]:
    """Validate a wheel batch before any member of the batch is installed."""
    candidates: list[WheelCandidate] = []
    destinations: set[str] = set()
    resolved_roots: ResolvedRoots = target.resolved_roots_internal
    resolved_directories = destination_cache if destination_cache is not None else {}
    for path in paths:
        # One archive open covers both the wheel_candidate() metadata read
        # and the member-destination validation below, instead of opening
        # the same file twice and sending the first open through
        # wheel_candidate's slow, email-based fallback for lack of a
        # dist-info directory to hand it.
        with (
            open(path, "rb", buffering=32768) as stream,
            zipfile.ZipFile(stream) as archive,
        ):
            dist_info, wheel_metadata_text = validate_wheel_with_metadata(
                archive,
                os.path.basename(path)[:-4].split("-", 1)[0],
            )
            candidate = wheel_candidate(
                path,
                archive=archive,
                dist_info_dir=dist_info,
                wheel_metadata_text=wheel_metadata_text,
                include_layout=False,
            )
            candidates.append(candidate)
            if validation_cache is not None:
                validation_cache[path] = dist_info
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative_parts = validate_member_parts(member.filename)
                destination = destination_internal_parts_text(
                    target,
                    relative_parts,
                    member.filename,
                    resolved_directories=resolved_directories,
                    resolved_roots=resolved_roots,
                )
                destination_text = destination
                if destination_text in destinations:
                    raise InstallationError(
                        f"Cannot install {canonicalize_name(candidate.name)}: "
                        "multiple wheels target "
                        f"the same path: {destination}",
                    )
                destinations.add(destination_text)
    return tuple(candidates)


def install_wheels_transactionally(
    items: Iterable[tuple[str, bool, DirectUrl | None]],
    *,
    target: InstallTarget,
    pycompile: bool = True,
    force: bool = False,
    preserve_existing: bool = False,
    script_executable: str | None = None,
    lookup_existing: bool = True,
    candidates: Iterable[WheelCandidate] | None = None,
    cache_dir: str | None = None,
) -> tuple[WheelCandidate, ...]:
    """Install a wheel batch with rollback across every wheel in the batch."""
    requests = tuple(items)
    installer = WheelInstaller(
        target,
        pycompile=pycompile,
        force=force,
        preserve_existing=preserve_existing,
        script_executable=script_executable,
    )
    destination_cache: DestinationCache = {}
    planned_candidates = (
        tuple(candidates)
        if candidates is not None
        else tuple(
            wheel_candidate_from_path(path, include_layout=False)
            for path, _, _ in requests
        )
    )
    if len(planned_candidates) != len(requests):
        raise ValueError("candidate count does not match wheel request count")
    if cache_dir is not None and all(
        candidate.source_kind in {None, "wheel"} for candidate in planned_candidates
    ):
        # The clone route compiles bytecode in the staged tree itself, so a
        # default install (compilation on) takes it too.
        cached_result = install_wheels_from_archive_cache(
            requests,
            planned_candidates,
            target=target,
            cache_dir=cache_dir,
            script_executable=script_executable,
            force=force,
            preserve_existing=preserve_existing,
            pycompile=pycompile,
        )
        if cached_result is not None:
            return cached_result
    target_inventory = (
        InstalledTargetInventory.from_target(
            target,
            names={candidate.canonical_name for candidate in planned_candidates},
        )
        if lookup_existing
        else None
    )
    existing_distributions = (
        {} if target_inventory is None else target_inventory.distributions
    )
    installer.target_inventory = target_inventory
    direct_destination_cache = None
    if not pycompile and not force and not existing_distributions:
        direct_destination_cache = direct_batch_preflight(
            requests,
            planned_candidates,
            target=target,
        )
    if direct_destination_cache is not None:
        return install_wheels_directly(
            requests,
            planned_candidates,
            target=target,
            pycompile=pycompile,
            installer=installer,
            destination_cache=direct_destination_cache,
        )
    with InstallTransaction() as transaction:
        with tempfile.TemporaryDirectory(prefix="cpip-wheel-batch-") as temporary:
            batch_stage = temporary
            parallel = (
                len(requests) >= 4
                and len(requests) <= 64
                and not pycompile
                and not existing_distributions
            )
            cache_for_workers = destination_cache
            if parallel:
                cache_for_workers = ThreadSafePathCache()

            def install_one(
                index: int,
                request: tuple[str, bool, DirectUrl | None],
                candidate: WheelCandidate,
            ) -> tuple[int, InstallTransaction, WheelCandidate]:
                local_transaction = InstallTransaction()
                try:
                    result = installer.install(
                        request[0],
                        candidate=candidate,
                        requested=request[1],
                        direct_url=request[2],
                        existing=None,
                        lookup_existing=False,
                        destination_cache=cache_for_workers,  # ty:ignore[invalid-argument-type]
                        stage_root=os.path.join(batch_stage, str(index)),
                        transaction=local_transaction,
                    )
                except Exception:
                    local_transaction.rollback()
                    raise
                return index, local_transaction, result

            try:
                if parallel:
                    from concurrent.futures import ThreadPoolExecutor

                    futures = []
                    staged_results = []
                    try:
                        with ThreadPoolExecutor(
                            max_workers=min(4, len(requests)),
                        ) as pool:
                            futures = [
                                pool.submit(install_one, index, request, candidate)
                                for index, (request, candidate) in enumerate(
                                    zip(requests, planned_candidates),
                                )
                            ]
                            staged_results = [future.result() for future in futures]
                        ordered_results = sorted(
                            staged_results,
                            key=lambda item: item[0],
                        )
                        for _, local_transaction, _ in ordered_results:
                            transaction.adopt(local_transaction)
                        for _, local_transaction, _ in ordered_results:
                            local_transaction.finalize()
                    except Exception:
                        for future in futures:
                            if not future.done() or future.cancelled():
                                continue
                            try:
                                _, local_transaction, _ = future.result()
                            except Exception:
                                continue
                            local_transaction.rollback()
                        raise
                    candidates = tuple(result for _, _, result in ordered_results)
                else:
                    candidates = tuple(
                        installer.install(
                            path,
                            candidate=candidate,
                            requested=requested,
                            direct_url=direct_url,
                            existing=existing_distributions.get(
                                candidate.canonical_name,
                            ),
                            lookup_existing=False,
                            destination_cache=destination_cache,
                            stage_root=os.path.join(batch_stage, str(index)),
                            transaction=transaction,
                        )
                        for index, (
                            (path, requested, direct_url),
                            candidate,
                        ) in enumerate(zip(requests, planned_candidates))
                    )
            except Exception:
                transaction.rollback()
                raise
            transaction.commit()
    return candidates
