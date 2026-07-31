"""Transactional wheel installation.

This module is the migration boundary between wheel preparation and the
filesystem transaction engine. It deliberately does not invoke cpip again.
"""

from __future__ import annotations

import csv
import io
import importlib.util
import os
import stat
import sys
import tempfile
import zipfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterable

from cpip.core.errors import InstallationError
from cpip.core.packaging import canonicalize_name
from cpip.core.wheel import WheelCandidate, parse_wheel, wheel_candidate
from cpip.install.target import InstallTarget
from cpip.install.transaction import InstallTransaction, normalized_internal
from cpip.install.wheel_archive import (
    DestinationCache,
    ResolvedRoots,
    copy_member_with_metadata,
    destination_internal_parts,
    destination_internal_parts_with_text,
    record_metadata_internal,
    validate_member_parts,
    zip_mode,
)
from cpip.install.wheel_state import compiled_files, existing_paths

if TYPE_CHECKING:
    from cpip.core.direct_url import DirectUrl
    from cpip.build.metadata import InstalledMetadataDistribution

DIRECT_CONTENT_LIMIT = 64 * 1024
DIRECT_CONTENT_BATCH_LIMIT = 4 * 1024 * 1024
StagedEntry = tuple[Path, Path, str, int | None]


class _RawWheelInfo:
    __slots__ = ("filename", "file_size", "external_attr")

    def __init__(self, filename: str, file_size: int, external_attr: int) -> None:
        self.filename = filename
        self.file_size = file_size
        self.external_attr = external_attr

    def is_dir(self) -> bool:
        return self.filename.endswith("/")


class _RawWheelArchive:
    __slots__ = ("_archive", "_file", "NameToInfo", "_infos")

    def __init__(self, file: object, archive: object) -> None:
        self._file = file
        self._archive = archive
        self._infos = [
            _RawWheelInfo(
                name,
                member[3],
                getattr(archive, "modes", {}).get(name, 0),
            )
            for name, member in archive.members.items()
        ]
        self.NameToInfo = {info.filename: info for info in self._infos}

    def infolist(self) -> list[_RawWheelInfo]:
        return self._infos

    def namelist(self) -> list[str]:
        return [info.filename for info in self._infos]

    def read(self, member: str | _RawWheelInfo) -> bytes:
        name = member if isinstance(member, str) else member.filename
        return self._archive.read(name)

    def open(self, member: _RawWheelInfo) -> io.BytesIO:
        return io.BytesIO(self.read(member))

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> _RawWheelArchive:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _open_wheel_archive(
    path: str | Path, candidate: WheelCandidate
) -> zipfile.ZipFile | _RawWheelArchive:
    """Open a fast raw archive when its members fit the streaming contract."""
    from cpip.resolution.fast_wheelhouse.archive import (
        WheelArchive,
        WheelhouseUnavailable,
    )

    layout = getattr(candidate, "wheel_layout", None)
    if layout is not None:
        # The resolver layout predates external mode bits; retain ZipInfo for
        # those candidates so executable members keep their original modes.
        return zipfile.ZipFile(path)
    members = None
    if members is not None and any(
        member[0] not in {0, 8} or member[2] > 1024 * 1024
        for member in members.values()
    ):
        return zipfile.ZipFile(path)
    try:
        file = open(path, "rb")
        archive = WheelArchive(file, members=members)
    except (OSError, ValueError, WheelhouseUnavailable):
        try:
            file.close()
        except UnboundLocalError:
            pass
        return zipfile.ZipFile(path)
    if any(
        member[0] not in {0, 8} or member[3] > 1024 * 1024
        for member in archive.members.values()
    ):
        file.close()
        return zipfile.ZipFile(path)
    return _RawWheelArchive(file, archive)


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
    ) -> None:
        self.target = target
        self.pycompile = pycompile
        self.force = force
        self.preserve_existing = preserve_existing
        self.script_executable = script_executable

    def install(
        self,
        path: str | Path,
        *,
        candidate: WheelCandidate | None = None,
        requested: bool = False,
        direct_url: DirectUrl | None = None,
        transaction_sink: list[InstallTransaction] | None = None,
        existing: InstalledMetadataDistribution | None = None,
        lookup_existing: bool = True,
        validated_dist_info: str | None = None,
        destination_cache: DestinationCache | None = None,
        stage_root: Path | None = None,
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
        )

    def validate_batch(
        self,
        paths: Iterable[str | Path],
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
    path: str | Path,
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
    existing: InstalledMetadataDistribution | None = None,
    lookup_existing: bool = True,
    validated_dist_info: str | None = None,
    destination_cache: DestinationCache | None = None,
    stage_root: Path | None = None,
    transaction: InstallTransaction | None = None,
    direct: bool = False,
) -> WheelCandidate:
    if candidate is None:
        candidate = wheel_candidate(path)
    if lookup_existing:
        if _target_has_distribution_metadata(target):
            from cpip.build.metadata import InstalledDistributionStore

            existing = InstalledDistributionStore(
                paths=[os.fspath(root) for root in target.library_roots]
            ).find(candidate.name)
        else:
            existing = None
    if (
        existing is not None
        and existing.version == str(candidate.version)
        and not force
        and not preserve_existing
    ):
        return candidate

    if existing is not None and (existing.version != str(candidate.version) or force):
        print(f"Uninstalling {existing.raw_name}-{existing.raw_version}")
    if direct and transaction is None:
        raise ValueError("direct wheel installation needs a transaction")
    if not direct:
        from cpip.install.wheel_scripts import (
            entry_point_scripts,
            rewrite_shebang,
            script_matches,
            script_text,
            write_windows_script,
        )

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
        stage_root = Path(temporary)
        stage_root_text = os.fspath(stage_root)
        purelib_text = os.fspath(target.purelib)
        purelib_prefix = purelib_text.rstrip(os.sep) + os.sep

        def record_relative_path(destination_text: str) -> str:
            if destination_text.startswith(purelib_prefix):
                return destination_text[len(purelib_prefix) :]
            return os.path.relpath(destination_text, purelib_text)
        staged: list[StagedEntry] = []
        record_destination: Path | None = None
        dist_info: str | None = None
        stage_directories: set[str] = set()
        resolved_directories = (
            destination_cache if destination_cache is not None else {}
        )
        resolved_roots: ResolvedRoots = {}
        record_metadata: dict[str, tuple[str, str]] = {}
        direct_contents: dict[str, bytes] = {}
        direct_metadata: dict[str, tuple[str, str]] = {}
        direct_content_size = 0

        def write_direct(
            destination: Path, contents: bytes, mode: int | None = None
        ) -> None:
            assert transaction is not None
            os.makedirs(os.fspath(destination.parent), exist_ok=True)
            transaction.record_created(destination)
            with open(os.fspath(destination), "wb") as file:
                file.write(contents)
            if mode is not None:
                os.chmod(os.fspath(destination), mode)

        with _open_wheel_archive(path, candidate) as archive:
            if validated_dist_info is None:
                layout = getattr(candidate, "wheel_layout", None)
                if layout is not None:
                    validated_dist_info = layout[0]
                else:
                    validated_dist_info, _ = parse_wheel(
                        archive,
                        os.path.basename(os.fspath(path))[:-4].split("-", 1)[0],
                    )
            wheel_record_metadata: dict[str, tuple[str, str]] = {}
            try:
                record_text = archive.read(f"{validated_dist_info}/RECORD").decode(
                    "utf-8"
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
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative_parts = validate_member_parts(member.filename)
                relative_name = relative_parts[-1] if relative_parts else ""
                if relative_parts and relative_parts[0].endswith(".dist-info"):
                    dist_info = relative_parts[0]
                source = Path(os.path.join(stage_root_text, *relative_parts))
                source_text = os.fspath(source)
                rewrite_metadata = (
                    relative_name == "METADATA" and candidate.name.isalpha()
                )
                script_member = len(relative_parts) >= 2 and relative_parts[-2] == "scripts"
                is_record = relative_name == "RECORD" and bool(relative_parts)
                direct_content = (
                    not rewrite_metadata
                    and not script_member
                    and not is_record
                    and member.file_size <= DIRECT_CONTENT_LIMIT
                    and direct_content_size + member.file_size
                    <= DIRECT_CONTENT_BATCH_LIMIT
                    and (not pycompile or os.path.splitext(relative_name)[1] != ".py")
                    and relative_name != "entry_points.txt"
                )
                destination, destination_text = destination_internal_parts_with_text(
                    target,
                    relative_parts,
                    member.filename,
                    resolved_directories=resolved_directories,
                    resolved_roots=resolved_roots,
                )
                if not direct and not direct_content:
                    source_parent = source.parent
                    source_parent_text = os.fspath(source_parent)
                    if source_parent_text not in stage_directories:
                        os.makedirs(source_parent_text, exist_ok=True)
                        stage_directories.add(source_parent_text)
                if rewrite_metadata or script_member:
                    contents = archive.read(member)
                elif is_record:
                    contents = None
                elif direct_content:
                    contents = archive.read(member)
                    direct_content_size += len(contents)
                else:
                    metadata = wheel_record_metadata.get("/".join(relative_parts))
                    if metadata is not None and metadata[1] != str(member.file_size):
                        metadata = None
                    if direct:
                        assert transaction is not None
                        os.makedirs(os.fspath(destination.parent), exist_ok=True)
                        transaction.record_created(destination)
                    metadata = copy_member_with_metadata(
                        archive,
                        member,
                        destination if direct else source,
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
                    write_direct(destination, contents, zip_mode(member))
                if contents is not None and not direct_content and not direct:
                    with open(os.fspath(source), "wb") as file:
                        file.write(contents)
                if script_member:
                    if direct:
                        raise InstallationError(
                            "direct wheel installation cannot contain scripts"
                        )
                    rewrite_shebang(source, script_executable)
                elif contents is not None:
                    metadata = wheel_record_metadata.get("/".join(relative_parts))
                    if metadata is None or metadata[1] != str(len(contents)):
                        metadata = record_metadata_internal(contents)
                    if direct_content:
                        if direct:
                            write_direct(destination, contents, zip_mode(member))
                        else:
                            direct_contents[destination_text] = contents
                        direct_metadata[destination_text] = metadata
                    else:
                        if direct:
                            direct_metadata[destination_text] = metadata
                        else:
                            record_metadata[source_text] = metadata
                mode = zip_mode(member)
                staged.append((source, destination, destination_text, mode))
                if relative_name == "RECORD" and relative_parts:
                    record_destination = destination

        if dist_info is None or record_destination is None:
            raise InstallationError(f"Wheel {path} has no valid dist-info metadata")
        record_destination_text = os.fspath(record_destination)

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

        dist_info_stage = stage_root / dist_info
        installer_source = dist_info_stage / "INSTALLER"
        installer_destination = target.purelib / dist_info / "INSTALLER"
        installer_contents = b"cpip\n"
        if direct:
            write_direct(installer_destination, installer_contents)
        else:
            direct_contents[os.fspath(installer_destination)] = installer_contents
        direct_metadata[os.fspath(installer_destination)] = record_metadata_internal(
            b"cpip\n"
        )
        staged.append(
            (
                installer_source,
                installer_destination,
                os.fspath(installer_destination),
                None,
            )
        )

        requested_destination = target.purelib / dist_info / "REQUESTED"
        if requested:
            requested_source = dist_info_stage / "REQUESTED"
            if direct:
                write_direct(requested_destination, b"")
            else:
                direct_contents[os.fspath(requested_destination)] = b""
            direct_metadata[os.fspath(requested_destination)] = record_metadata_internal(
                b""
            )
            staged.append(
                (
                    requested_source,
                    requested_destination,
                    os.fspath(requested_destination),
                    None,
                )
            )

        if direct_url is not None:
            direct_url_source = dist_info_stage / "direct_url.json"
            with open(direct_url_source, "w", encoding="utf-8") as file:
                file.write(direct_url.to_json())
            staged.append(
                (
                    direct_url_source,
                    target.purelib / dist_info / "direct_url.json",
                    os.fspath(target.purelib / dist_info / "direct_url.json"),
                    None,
                )
            )

        scripts = (
            {}
            if direct
            else entry_point_scripts(stage_root / dist_info / "entry_points.txt")
        )
        if scripts:
            script_destinations = {
                target.scripts / generated
                for name in scripts
                for generated in (name, f"{name}-script.py", f"{name}.exe")
            }
            staged = [item for item in staged if item[1] not in script_destinations]
        script_stage = stage_root / ".cpip-scripts"
        script_maker_type = None
        if scripts:
            os.makedirs(os.fspath(script_stage), exist_ok=True)
            try:
                from distlib.scripts import ScriptMaker
            except ImportError:
                pass
            else:
                script_maker_type = ScriptMaker
        for name, (target_ref, gui) in scripts.items():
            if os.path.basename(name) != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory"
                )
            if script_maker_type is None:
                if os.name == "nt":
                    source = script_stage / f"{name}.exe"
                    write_windows_script(
                        source,
                        script_text(target_ref, script_executable),
                        gui=gui,
                    )
                else:
                    source = script_stage / name
                    with open(os.fspath(source), "w", encoding="utf-8") as file:
                        file.write(script_text(target_ref, script_executable))
                    os.chmod(
                        source,
                        os.stat(source).st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH,
                    )
            else:
                maker = script_maker_type(None, os.fspath(script_stage))
                maker.clobber = True
                maker.variants = {""}
                if script_executable is not None:
                    maker.executable = script_executable
                maker.make(f"{name} = {target_ref}", options={"gui": gui})
                if os.name == "nt":
                    # Keep the script-text form for callers that inspect the
                    # generated script path. Windows execution uses the EXE.
                    source = script_stage / name
                    with open(os.fspath(source), "w", encoding="utf-8") as file:
                        file.write(script_text(target_ref, script_executable))
                    os.chmod(
                        source,
                        os.stat(source).st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH,
                    )

        if scripts:
            with os.scandir(os.fspath(script_stage)) as entries:
                script_sources = tuple(entries)
            for entry in script_sources:
                source = script_stage / entry.name
                destination = target.scripts / source.name
                staged.append(
                    (source, destination, os.fspath(destination), os.stat(source).st_mode)
                )

        if pycompile:
            staged.extend(compiled_files(stage_root, staged))

        record_rows = []
        for source, destination, destination_text, _ in staged:
            if destination_text == record_destination_text:
                record_rows.append(
                    (record_relative_path(destination_text), "", "")
                )
                continue
            metadata = direct_metadata.get(destination_text)
            if metadata is None:
                metadata = record_metadata.get(os.fspath(source))
            if metadata is None:
                with open(source, "rb") as file:
                    metadata = record_metadata_internal(file.read())
            record_rows.append(
                (
                    record_relative_path(destination_text),
                    metadata[0],
                    metadata[1],
                )
            )
        record_rows.sort()
        record_file = io.StringIO(newline="")
        csv.writer(record_file).writerows(record_rows)
        record_contents = record_file.getvalue().encode("utf-8")
        if direct:
            write_direct(record_destination, record_contents)
        else:
            direct_contents[os.fspath(record_destination)] = record_contents
        direct_metadata[os.fspath(record_destination)] = record_metadata_internal(
            record_contents
        )

        owned_paths, old_paths = existing_paths(existing)
        if preserve_existing and existing is not None:
            old_paths = set()
        old_path_texts = {os.fspath(path) for path in old_paths}
        scripts_text = os.fspath(target.scripts)
        for _, destination, destination_text, _ in staged:
            if (
                not direct
                and os.path.dirname(destination_text) == scripts_text
                and os.path.exists(destination_text)
            ):
                if script_matches(destination, scripts):
                    owned_paths.add(destination)
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
                        destination_text, contents, mode=mode
                    )
                else:
                    active_transaction.add(
                        os.fspath(source), destination_text, mode=mode
                    )
            for old_path in old_path_texts - new_destinations:
                active_transaction.delete(old_path)
            if transaction is None:
                active_transaction.commit(finalize=transaction_sink is None)
        if transaction_sink is not None and transaction is None:
            transaction_sink.append(active_transaction)
        if existing is not None and (
            existing.version != str(candidate.version) or force
        ):
            print(
                f"Successfully uninstalled {existing.raw_name}-{existing.raw_version}"
            )
    return candidate


def validate_wheel_batch(
    paths: Iterable[str | Path],
    *,
    target: InstallTarget,
    validation_cache: dict[str, str] | None = None,
    destination_cache: DestinationCache | None = None,
) -> tuple[WheelCandidate, ...]:
    """Validate a wheel batch before any member of the batch is installed."""
    candidates = tuple(wheel_candidate(path) for path in paths)
    destinations: set[str] = set()
    resolved_roots: ResolvedRoots = {}
    resolved_directories = destination_cache if destination_cache is not None else {}
    for candidate in candidates:
        path = candidate.path
        with zipfile.ZipFile(path) as archive:
            dist_info, _ = parse_wheel(
                archive, os.path.basename(os.fspath(path))[:-4].split("-", 1)[0]
            )
            if validation_cache is not None:
                validation_cache[os.fspath(path)] = dist_info
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative_parts = validate_member_parts(member.filename)
                destination = destination_internal_parts(
                    target,
                    relative_parts,
                    member.filename,
                    resolved_directories=resolved_directories,
                    resolved_roots=resolved_roots,
                )
                destination_text = os.fspath(destination)
                if destination_text in destinations:
                    raise InstallationError(
                        f"Cannot install {canonicalize_name(candidate.name)}: "
                        "multiple wheels target "
                        f"the same path: {destination}"
                    )
                destinations.add(destination_text)
    return candidates


def _direct_batch_preflight(
    requests: tuple[tuple[str | Path, bool, DirectUrl | None], ...],
    candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
) -> DestinationCache | None:
    """Check whether a batch can write final paths without staging files."""
    destinations: set[str] = set()
    resolved_directories: DestinationCache = {}
    resolved_roots: ResolvedRoots = {}
    member_sets: list[tuple[object, ...]] = []
    total_size = 0
    for request, candidate in zip(requests, candidates):
        if request[2] is not None or candidate.wheel_layout is None:
            return None
        _, raw_members, _ = candidate.wheel_layout
        member_sets.append(raw_members)
        total_size += sum(
            raw_member[4]
            for raw_member in raw_members
            if not raw_member[0].endswith("/")
        )
    if total_size <= DIRECT_CONTENT_BATCH_LIMIT:
        return None
    for raw_members in member_sets:
        for raw_member in raw_members:
            name = raw_member[0]
            if name.endswith("/"):
                continue
            try:
                relative_parts = validate_member_parts(name)
            except InstallationError:
                return None
            if (
                (relative_parts[-1] if relative_parts else "")
                in {"INSTALLER", "REQUESTED", "direct_url.json"}
                or (relative_parts[-1] if relative_parts else "") == "entry_points.txt"
                or (len(relative_parts) >= 2 and relative_parts[-2] == "scripts")
            ):
                return None
            destination = destination_internal_parts(
                target,
                relative_parts,
                name,
                resolved_directories=resolved_directories,
                resolved_roots=resolved_roots,
            )
            destination_text = os.fspath(destination)
            if destination_text in destinations or os.path.lexists(destination_text):
                return None
            destinations.add(destination_text)
    return resolved_directories


def _install_wheels_directly(
    requests: tuple[tuple[str | Path, bool, DirectUrl | None], ...],
    candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
    pycompile: bool,
    installer: WheelInstaller,
    destination_cache: DestinationCache,
) -> tuple[WheelCandidate, ...]:
    """Install a preflighted fresh batch directly with transactional rollback."""
    with InstallTransaction() as transaction:
        parallel = 4 <= len(requests) <= 64 and not pycompile

        def install_one(
            index: int,
            request: tuple[str | Path, bool, DirectUrl | None],
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
                    destination_cache=destination_cache,
                    transaction=local_transaction,
                    direct=True,
                )
            except Exception:
                local_transaction.rollback()
                raise
            return index, local_transaction, result

        futures = []
        staged_results: list[tuple[int, InstallTransaction, WheelCandidate]] = []
        try:
            if parallel:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(4, len(requests))) as pool:
                    futures = [
                        pool.submit(install_one, index, request, candidate)
                        for index, (request, candidate) in enumerate(
                            zip(requests, candidates)
                        )
                    ]
                    staged_results = [future.result() for future in futures]
                ordered_results = sorted(staged_results, key=lambda item: item[0])
                for _, local_transaction, _ in ordered_results:
                    transaction.adopt(local_transaction)
                transaction.finish_successfully()
                for _, local_transaction, _ in ordered_results:
                    local_transaction.finalize()
                return tuple(result for _, _, result in ordered_results)

            results = tuple(
                installer.install(
                    path,
                    candidate=candidate,
                    requested=requested,
                    direct_url=direct_url,
                    existing=None,
                    lookup_existing=False,
                    destination_cache=destination_cache,
                    transaction=transaction,
                    direct=True,
                )
                for (path, requested, direct_url), candidate in zip(
                    requests, candidates
                )
            )
            transaction.finish_successfully()
            return results
        except Exception:
            for future in futures:
                if not future.done() or future.cancelled():
                    continue
                try:
                    _, local_transaction, _ = future.result()
                except Exception:
                    continue
                local_transaction.rollback()
            transaction.rollback()
            raise


def install_wheels_transactionally(
    items: Iterable[tuple[str | Path, bool, DirectUrl | None]],
    *,
    target: InstallTarget,
    pycompile: bool = True,
    force: bool = False,
    preserve_existing: bool = False,
    script_executable: str | None = None,
    lookup_existing: bool = True,
    candidates: Iterable[WheelCandidate] | None = None,
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
        else tuple(wheel_candidate(path) for path, _, _ in requests)
    )
    if len(planned_candidates) != len(requests):
        raise ValueError("candidate count does not match wheel request count")
    if lookup_existing and _target_has_distribution_metadata(target):
        from cpip.build.metadata import InstalledDistributionStore

        existing_distributions = {
            distribution.canonical_name: distribution
            for distribution in InstalledDistributionStore(
                paths=[os.fspath(root) for root in target.library_roots]
            ).iter(names={candidate.canonical_name for candidate in planned_candidates})
        }
    else:
        existing_distributions = {}
    direct_destination_cache = None
    if not pycompile and not force and not existing_distributions:
        direct_destination_cache = _direct_batch_preflight(
            requests, planned_candidates, target=target
        )
    if direct_destination_cache is not None:
        return _install_wheels_directly(
            requests,
            planned_candidates,
            target=target,
            pycompile=pycompile,
            installer=installer,
            destination_cache=direct_destination_cache,
        )
    with InstallTransaction() as transaction:
        with tempfile.TemporaryDirectory(prefix="cpip-wheel-batch-") as temporary:
            batch_stage = Path(temporary)
            parallel = (
                len(requests) >= 4
                and len(requests) <= 64
                and not pycompile
                and not existing_distributions
            )
            cache_for_workers = destination_cache
            if parallel:
                from threading import Lock

                class ThreadSafePathCache:
                    def __init__(self) -> None:
                        self.values: DestinationCache = {}
                        self.lock = Lock()

                    def get(
                        self,
                        key: tuple[Path, PurePosixPath],
                    ) -> Path | None:
                        with self.lock:
                            return self.values.get(key)

                    def __setitem__(
                        self,
                        key: tuple[Path, PurePosixPath],
                        value: Path,
                    ) -> None:
                        with self.lock:
                            self.values[key] = value

                cache_for_workers = ThreadSafePathCache()

            def install_one(
                index: int,
                request: tuple[str | Path, bool, DirectUrl | None],
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
                        destination_cache=cache_for_workers,
                        stage_root=batch_stage / str(index),
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
                            max_workers=min(4, len(requests))
                        ) as pool:
                            futures = [
                                pool.submit(install_one, index, request, candidate)
                                for index, (request, candidate) in enumerate(
                                    zip(requests, planned_candidates)
                                )
                            ]
                            staged_results = [future.result() for future in futures]
                        ordered_results = sorted(
                            staged_results, key=lambda item: item[0]
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
                                candidate.canonical_name
                            ),
                            lookup_existing=False,
                            destination_cache=destination_cache,
                            stage_root=batch_stage / str(index),
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


class DistributionUninstaller:
    """Remove installed distributions through their recorded files."""

    def __init__(self, paths: list[str] | None = None) -> None:
        self.paths = paths

    def uninstall(self, name: str) -> bool:
        return uninstall_distribution(name, paths=self.paths)


def uninstall_distribution(
    name: str,
    *,
    paths: list[str] | None = None,
) -> bool:
    """Remove an installed distribution from its RECORD manifest atomically."""
    from cpip.build.metadata import InstalledDistributionStore

    distribution = InstalledDistributionStore(paths=paths).find(name)
    if distribution is None:
        return False
    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            entries = distribution.read_text("RECORD")
        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot uninstall {distribution.raw_name} {distribution.version}: "
                "no RECORD file was found"
            ) from exc
    else:
        entries = None

    root = os.path.realpath(os.fspath(distribution.location))
    root_path = Path(root)
    recorded_paths: set[Path] = set()
    if entries is not None:
        for row in csv.reader(entries.splitlines()):
            if not row or not row[0]:
                continue
            relative = PurePosixPath(row[0])
            if relative.is_absolute():
                continue
            path_text = os.path.join(root, *relative.parts)
            resolved_text = os.path.realpath(path_text)
            # RECORD uses POSIX separators, but an absolute Windows path can
            # be smuggled in as a backslash-containing "relative" entry.
            # Never let a manifest remove files outside the install root.
            if os.name == "nt" and Path(row[0]).is_absolute():
                continue
            if ".." in relative.parts and os.path.basename(
                os.path.dirname(resolved_text)
            ) not in {
                "bin",
                "Scripts",
            }:
                continue
            if ".." in relative.parts:
                path_text = resolved_text
            path = Path(path_text)
            recorded_paths.add(path)
            if os.path.splitext(path_text)[1] == ".py":
                recorded_paths.update(
                    {
                        Path(importlib.util.cache_from_source(path_text)),
                        Path(f"{path_text}c"),
                        Path(f"{path_text[:-3]}.pyo"),
                    }
                )
    elif distribution.info_location and distribution.info_location.endswith(
        ".egg-info"
    ):
        recorded_paths.add(Path(distribution.info_location))
        egg_link_root = Path(distribution.info_location).parent
        entries = distribution.iter_declared_entries()
        for entry in entries:
            relative = PurePosixPath(entry)
            if relative.is_absolute():
                continue
            path = Path(
                os.path.realpath(os.path.join(os.fspath(egg_link_root), *relative.parts))
            )
            try:
                path.relative_to(root_path)
            except ValueError:
                if path.parent.name not in {"bin", "Scripts"}:
                    continue
            recorded_paths.add(path)
        if not entries:
            try:
                top_level = distribution.read_text("top_level.txt")
            except FileNotFoundError:
                top_level = ""
            for name in top_level.splitlines():
                name = name.strip()
                if name and name.isidentifier():
                    recorded_paths.update(
                        {root_path / name, root_path / f"{name}.py"}
                    )
        egg_links = list(egg_link_root.glob("*.egg-link"))
        egg_links.extend(
            egg_link
            for path_entry in sys.path
            for egg_link in Path(path_entry).glob("*.egg-link")
        )
        for egg_link in egg_links:
            if egg_link.stem.casefold() == distribution.raw_name.casefold():
                recorded_paths.add(egg_link)

    existing = {path for path in recorded_paths if os.path.lexists(path)}
    if not existing:
        return False
    transaction = InstallTransaction()
    for path in existing:
        transaction.delete(path)
    transaction.commit()
    return True
