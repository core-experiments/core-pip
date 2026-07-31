"""Transactional wheel installation.

This module is the migration boundary between wheel preparation and the
filesystem transaction engine. It deliberately does not invoke cpip again.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import stat
import sys
import tempfile
import zipfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Iterable

from cpip.build.metadata import (
    InstalledDistributionStore,
    InstalledMetadataDistribution,
)
from cpip.core.direct_url import DirectUrl
from cpip.core.errors import InstallationError
from cpip.core.packaging import canonicalize_name
from cpip.core.wheel import WheelCandidate, parse_wheel, wheel_candidate
from cpip.install.target import InstallTarget
from cpip.install.transaction import InstallTransaction, normalized_internal
from cpip.install.wheel_archive import (
    copy_member_with_metadata,
    destination_internal,
    is_script_member,
    record_metadata_internal,
    validate_member,
    zip_mode,
)
from cpip.install.wheel_scripts import (
    entry_point_scripts,
    rewrite_shebang,
    script_matches,
    script_text,
    write_windows_script,
)
from cpip.install.wheel_state import compiled_files, existing_paths


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
        requested: bool = False,
        direct_url: DirectUrl | None = None,
        transaction_sink: list[InstallTransaction] | None = None,
        existing: InstalledMetadataDistribution | None = None,
        lookup_existing: bool = True,
        validated_dist_info: str | None = None,
        destination_cache: dict[tuple[Path, PurePosixPath], Path] | None = None,
        stage_root: Path | None = None,
        transaction: InstallTransaction | None = None,
    ) -> WheelCandidate:
        return install_wheel_internal(
            path,
            target=self.target,
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
        )

    def validate_batch(
        self,
        paths: Iterable[str | Path],
        *,
        validation_cache: dict[str, str] | None = None,
        destination_cache: dict[tuple[Path, PurePosixPath], Path] | None = None,
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
    destination_cache: dict[tuple[Path, PurePosixPath], Path] | None = None,
    stage_root: Path | None = None,
    transaction: InstallTransaction | None = None,
) -> WheelCandidate:
    candidate = wheel_candidate(path)
    if lookup_existing:
        existing = InstalledDistributionStore(
            paths=[os.fspath(root) for root in target.library_roots]
        ).find(candidate.name)
    if (
        existing is not None
        and existing.version == str(candidate.version)
        and not force
        and not preserve_existing
    ):
        return candidate

    if existing is not None and (existing.version != str(candidate.version) or force):
        print(f"Uninstalling {existing.raw_name}-{existing.raw_version}")

    stage_context = (
        tempfile.TemporaryDirectory(prefix="cpip-wheel-stage-")
        if stage_root is None
        else nullcontext(stage_root)
    )
    with stage_context as temporary:
        stage_root = Path(temporary)
        staged: list[tuple[Path, Path, int | None]] = []
        record_destination: Path | None = None
        record_source: Path | None = None
        dist_info: str | None = None
        resolved_directories = (
            destination_cache if destination_cache is not None else {}
        )
        record_metadata: dict[Path, tuple[str, str]] = {}

        with zipfile.ZipFile(path) as archive:
            if validated_dist_info is None:
                validated_dist_info, _ = parse_wheel(
                    archive, Path(path).name[:-4].split("-", 1)[0]
                )
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = validate_member(member.filename)
                if relative.parts and relative.parts[0].endswith(".dist-info"):
                    dist_info = relative.parts[0]
                source = stage_root / Path(*relative.parts)
                source.parent.mkdir(parents=True, exist_ok=True)
                rewrite_metadata = (
                    relative.name == "METADATA" and candidate.name.isalpha()
                )
                if rewrite_metadata or is_script_member(relative):
                    contents = archive.read(member)
                else:
                    record_metadata[source] = copy_member_with_metadata(
                        archive, member, source
                    )
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
                if contents is not None:
                    with open(source, "wb") as file:
                        file.write(contents)
                if is_script_member(relative):
                    rewrite_shebang(source, script_executable)
                elif contents is not None:
                    record_metadata[source] = record_metadata_internal(contents)
                destination = destination_internal(
                    target, relative, resolved_directories=resolved_directories
                )
                mode = zip_mode(member)
                staged.append((source, destination, mode))
                if relative.name == "RECORD" and relative.parts:
                    record_destination = destination
                    record_source = source

        if dist_info is None or record_destination is None or record_source is None:
            raise InstallationError(f"Wheel {path} has no valid dist-info metadata")

        managed_metadata = {
            target.purelib / dist_info / "INSTALLER",
            target.purelib / dist_info / "REQUESTED",
            target.purelib / dist_info / "direct_url.json",
        }
        staged = [
            item
            for item in staged
            if item[1] not in managed_metadata or item[1] == record_destination
        ]

        dist_info_stage = stage_root / dist_info
        installer_source = dist_info_stage / "INSTALLER"
        with open(installer_source, "wb") as file:
            file.write(b"cpip\n")
        installer_destination = target.purelib / dist_info / "INSTALLER"
        staged.append((installer_source, installer_destination, None))

        requested_destination = target.purelib / dist_info / "REQUESTED"
        if requested:
            requested_source = dist_info_stage / "REQUESTED"
            with open(requested_source, "w", encoding="utf-8"):
                pass
            staged.append((requested_source, requested_destination, None))

        if direct_url is not None:
            direct_url_source = dist_info_stage / "direct_url.json"
            with open(direct_url_source, "w", encoding="utf-8") as file:
                file.write(direct_url.to_json())
            staged.append(
                (
                    direct_url_source,
                    target.purelib / dist_info / "direct_url.json",
                    None,
                )
            )

        scripts = entry_point_scripts(stage_root / dist_info / "entry_points.txt")
        script_destinations = {
            target.scripts / generated
            for name in scripts
            for generated in (name, f"{name}-script.py", f"{name}.exe")
        }
        staged = [item for item in staged if item[1] not in script_destinations]
        script_stage = stage_root / ".cpip-scripts"
        script_stage.mkdir(parents=True, exist_ok=True)
        for name, (target_ref, gui) in scripts.items():
            if Path(name).name != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory"
                )
            try:
                from distlib.scripts import ScriptMaker
            except ImportError:
                if os.name == "nt":
                    source = script_stage / f"{name}.exe"
                    write_windows_script(
                        source,
                        script_text(target_ref, script_executable),
                        gui=gui,
                    )
                else:
                    source = script_stage / name
                    with open(source, "w", encoding="utf-8") as file:
                        file.write(script_text(target_ref, script_executable))
                    os.chmod(
                        source,
                        os.stat(source).st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH,
                    )
            else:
                maker = ScriptMaker(None, os.fspath(script_stage))
                maker.clobber = True
                maker.variants = {""}
                if script_executable is not None:
                    maker.executable = script_executable
                maker.make(f"{name} = {target_ref}", options={"gui": gui})
                if os.name == "nt":
                    # Keep the script-text form for callers that inspect the
                    # generated script path. Windows execution uses the EXE.
                    source = script_stage / name
                    with open(source, "w", encoding="utf-8") as file:
                        file.write(script_text(target_ref, script_executable))
                    os.chmod(
                        source,
                        os.stat(source).st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH,
                    )

        for source in script_stage.iterdir():
            staged.append(
                (source, target.scripts / source.name, os.stat(source).st_mode)
            )

        if pycompile:
            staged.extend(compiled_files(stage_root, staged))

        record_rows = []
        for source, destination, _ in staged:
            if destination == record_destination:
                record_rows.append(
                    (os.path.relpath(destination, target.purelib), "", "")
                )
                continue
            metadata = record_metadata.get(source)
            if metadata is None:
                with open(source, "rb") as file:
                    metadata = record_metadata_internal(file.read())
            record_rows.append(
                (
                    os.path.relpath(destination, target.purelib),
                    metadata[0],
                    metadata[1],
                )
            )
        record_rows.sort()
        with open(record_source, "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows(record_rows)

        owned_paths, old_paths = existing_paths(existing)
        if preserve_existing and existing is not None:
            old_paths = set()
        for _, destination, _ in staged:
            if destination.parent == target.scripts and destination.exists():
                if script_matches(destination, scripts):
                    owned_paths.add(destination)
        new_destinations = {destination for _, destination, _ in staged}
        active_transaction = transaction or InstallTransaction(owned_paths=owned_paths)
        if transaction is not None:
            transaction.owned.update(normalized_internal(path) for path in owned_paths)
        for source, destination, mode in staged:
            active_transaction.add(source, destination, mode=mode)
        for old_path in old_paths - new_destinations:
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
    destination_cache: dict[tuple[Path, PurePosixPath], Path] | None = None,
) -> tuple[WheelCandidate, ...]:
    """Validate a wheel batch before any member of the batch is installed."""
    candidates = tuple(wheel_candidate(path) for path in paths)
    destinations: set[Path] = set()
    for candidate in candidates:
        path = candidate.path
        with zipfile.ZipFile(path) as archive:
            dist_info, _ = parse_wheel(archive, Path(path).name[:-4].split("-", 1)[0])
            if validation_cache is not None:
                validation_cache[os.fspath(path)] = dist_info
            for member in archive.infolist():
                if member.is_dir():
                    continue
                destination = destination_internal(
                    target,
                    validate_member(member.filename),
                    resolved_directories=(
                        destination_cache if destination_cache is not None else {}
                    ),
                )
                if destination in destinations:
                    raise InstallationError(
                        f"Cannot install {canonicalize_name(candidate.name)}: "
                        "multiple wheels target "
                        f"the same path: {destination}"
                    )
                destinations.add(destination)
    return candidates


def install_wheels_transactionally(
    items: Iterable[tuple[str | Path, bool, DirectUrl | None]],
    *,
    target: InstallTarget,
    pycompile: bool = True,
    force: bool = False,
    preserve_existing: bool = False,
    script_executable: str | None = None,
    lookup_existing: bool = True,
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
    destination_cache: dict[tuple[Path, PurePosixPath], Path] = {}
    existing_distributions = (
        {
            distribution.canonical_name: distribution
            for distribution in InstalledDistributionStore(
                paths=[os.fspath(root) for root in target.library_roots]
            ).iter()
        }
        if lookup_existing
        else {}
    )
    planned_candidates = tuple(wheel_candidate(path) for path, _, _ in requests)
    with InstallTransaction() as transaction:
        with tempfile.TemporaryDirectory(prefix="cpip-wheel-batch-") as temporary:
            batch_stage = Path(temporary)
            try:
                candidates = tuple(
                    installer.install(
                        path,
                        requested=requested,
                        direct_url=direct_url,
                        existing=existing_distributions.get(candidate.canonical_name),
                        lookup_existing=False,
                        destination_cache=destination_cache,
                        stage_root=batch_stage / str(index),
                        transaction=transaction,
                    )
                    for index, ((path, requested, direct_url), candidate) in enumerate(
                        zip(requests, planned_candidates)
                    )
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

    root = Path(distribution.location).resolve(strict=False)
    recorded_paths: set[Path] = set()
    if entries is not None:
        for row in csv.reader(entries.splitlines()):
            if not row or not row[0]:
                continue
            relative = PurePosixPath(row[0])
            if relative.is_absolute():
                continue
            path = root / Path(*relative.parts)
            resolved = path.resolve(strict=False)
            # RECORD uses POSIX separators, but an absolute Windows path can
            # be smuggled in as a backslash-containing "relative" entry.
            # Never let a manifest remove files outside the install root.
            if os.name == "nt" and Path(row[0]).is_absolute():
                continue
            if ".." in relative.parts and resolved.parent.name not in {
                "bin",
                "Scripts",
            }:
                continue
            if ".." in relative.parts:
                path = resolved
            recorded_paths.add(path)
            if path.suffix == ".py":
                recorded_paths.update(
                    {
                        Path(importlib.util.cache_from_source(os.fspath(path))),
                        path.with_suffix(".pyc"),
                        path.with_suffix(".pyo"),
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
            path = (egg_link_root / Path(*relative.parts)).resolve(strict=False)
            try:
                path.relative_to(root)
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
                    recorded_paths.update({root / name, root / f"{name}.py"})
        egg_links = list(egg_link_root.glob("*.egg-link"))
        egg_links.extend(
            egg_link
            for path_entry in sys.path
            for egg_link in Path(path_entry).glob("*.egg-link")
        )
        for egg_link in egg_links:
            if egg_link.stem.casefold() == distribution.raw_name.casefold():
                recorded_paths.add(egg_link)

    existing = {path for path in recorded_paths if path.exists() or path.is_symlink()}
    if not existing:
        return False
    transaction = InstallTransaction()
    for path in existing:
        transaction.delete(path)
    transaction.commit()
    return True
