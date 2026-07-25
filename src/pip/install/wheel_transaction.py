"""Transactional wheel installation.

This module is the migration boundary between wheel preparation and the
filesystem transaction engine. It deliberately does not invoke pip again.
"""

from __future__ import annotations

import base64
import compileall
import csv
import hashlib
import importlib.util
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from pip.build.metadata import (
    InstalledDistributionStore,
    InstalledMetadataDistribution,
)
from pip.core.direct_url import DirectUrl
from pip.core.errors import InstallationError
from pip.core.packaging import canonicalize_name
from pip.core.wheel import WheelCandidate, parse_wheel, wheel_candidate
from pip.install.target import InstallTarget
from pip.install.transaction import InstallTransaction


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
    ) -> WheelCandidate:
        return _install_wheel(
            path,
            target=self.target,
            pycompile=self.pycompile,
            requested=requested,
            force=self.force,
            preserve_existing=self.preserve_existing,
            direct_url=direct_url,
            script_executable=self.script_executable,
            transaction_sink=transaction_sink,
        )

    def validate_batch(self, paths: Iterable[str | Path]) -> tuple[WheelCandidate, ...]:
        return _validate_wheel_batch(paths, target=self.target)


def _install_wheel(
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
) -> WheelCandidate:
    candidate = wheel_candidate(path)
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

    with tempfile.TemporaryDirectory(prefix="pip-wheel-stage-") as temporary:
        stage_root = Path(temporary)
        staged: list[tuple[Path, Path, int | None]] = []
        record_destination: Path | None = None
        record_source: Path | None = None
        dist_info: str | None = None

        with zipfile.ZipFile(path) as archive:
            parse_wheel(archive, Path(path).name[:-4].split("-", 1)[0])
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = _validate_member(member.filename)
                if relative.parts and relative.parts[0].endswith(".dist-info"):
                    dist_info = relative.parts[0]
                source = stage_root / Path(*relative.parts)
                source.parent.mkdir(parents=True, exist_ok=True)
                contents = archive.read(member)
                if relative.name == "METADATA" and candidate.name.isalpha():
                    lines = contents.decode("utf-8").splitlines(keepends=True)
                    for index, line in enumerate(lines):
                        if line.lower().startswith("name:"):
                            ending = "\n" if line.endswith("\n") else ""
                            lines[index] = f"Name: {candidate.name.lower()}{ending}"
                            contents = "".join(lines).encode("utf-8")
                            break
                source.write_bytes(contents)
                if _is_script_member(relative):
                    _rewrite_shebang(source, script_executable)
                destination = _destination(target, relative)
                mode = _zip_mode(member)
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
        installer_source.write_text("pip\n", encoding="utf-8")
        installer_destination = target.purelib / dist_info / "INSTALLER"
        staged.append((installer_source, installer_destination, None))

        requested_destination = target.purelib / dist_info / "REQUESTED"
        if requested:
            requested_source = dist_info_stage / "REQUESTED"
            requested_source.write_text("", encoding="utf-8")
            staged.append((requested_source, requested_destination, None))

        if direct_url is not None:
            direct_url_source = dist_info_stage / "direct_url.json"
            direct_url_source.write_text(direct_url.to_json(), encoding="utf-8")
            staged.append(
                (
                    direct_url_source,
                    target.purelib / dist_info / "direct_url.json",
                    None,
                )
            )

        scripts = _entry_point_scripts(stage_root / dist_info / "entry_points.txt")
        script_destinations = {
            target.scripts / generated
            for name in scripts
            for generated in (name, f"{name}-script.py", f"{name}.exe")
        }
        staged = [item for item in staged if item[1] not in script_destinations]
        for name, target_ref in scripts.items():
            if Path(name).name != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory"
                )
            source = stage_root / ".pip-scripts" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                _script_text(target_ref, script_executable), encoding="utf-8"
            )
            source.chmod(
                source.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            staged.append((source, target.scripts / name, source.stat().st_mode))

        if pycompile:
            staged.extend(_compiled_files(stage_root, staged))

        record_rows = []
        for source, destination, _ in staged:
            if destination == record_destination:
                record_rows.append(
                    (os.path.relpath(destination, target.purelib), "", "")
                )
                continue
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(source.read_bytes()).digest()
            )
            record_rows.append(
                (
                    os.path.relpath(destination, target.purelib),
                    f"sha256={digest.rstrip(b'=').decode('ascii')}",
                    str(source.stat().st_size),
                )
            )
        record_rows.sort()
        with record_source.open("w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows(record_rows)

        owned_paths, old_paths = _existing_paths(existing)
        if preserve_existing and existing is not None:
            old_paths = set()
        for _, destination, _ in staged:
            if destination.parent == target.scripts and destination.exists():
                if _script_matches(destination, scripts):
                    owned_paths.add(destination)
        new_destinations = {destination for _, destination, _ in staged}
        transaction = InstallTransaction(owned_paths=owned_paths)
        for source, destination, mode in staged:
            transaction.add(source, destination, mode=mode)
        for old_path in old_paths - new_destinations:
            transaction.delete(old_path)
        transaction.commit(finalize=transaction_sink is None)
        if transaction_sink is not None:
            transaction_sink.append(transaction)
        if existing is not None and (
            existing.version != str(candidate.version) or force
        ):
            print(
                f"Successfully uninstalled {existing.raw_name}-{existing.raw_version}"
            )
    return candidate


def _validate_wheel_batch(
    paths: Iterable[str | Path],
    *,
    target: InstallTarget,
) -> tuple[WheelCandidate, ...]:
    """Validate a wheel batch before any member of the batch is installed."""
    candidates = tuple(wheel_candidate(path) for path in paths)
    destinations: set[Path] = set()
    for candidate in candidates:
        path = candidate.path
        with zipfile.ZipFile(path) as archive:
            parse_wheel(archive, Path(path).name[:-4].split("-", 1)[0])
            for member in archive.infolist():
                if member.is_dir():
                    continue
                destination = _destination(target, _validate_member(member.filename))
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
    installer.validate_batch(path for path, _, _ in requests)
    transactions: list[InstallTransaction] = []
    try:
        candidates = tuple(
            installer.install(
                path,
                requested=requested,
                direct_url=direct_url,
                transaction_sink=transactions,
            )
            for path, requested, direct_url in requests
        )
    except Exception:
        for transaction in reversed(transactions):
            transaction.rollback()
        raise
    for transaction in transactions:
        transaction.finalize()
    return candidates


class DistributionUninstaller:
    """Remove installed distributions through their recorded files."""

    def __init__(self, paths: list[str] | None = None) -> None:
        self.paths = paths

    def uninstall(self, name: str) -> bool:
        return _uninstall_distribution(name, paths=self.paths)


def _uninstall_distribution(
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


def _validate_member(name: str) -> PurePosixPath:
    if "\\" in name:
        raise InstallationError(f"wheel member uses an invalid separator: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise InstallationError(
            f"wheel member is outside the install destination: {name!r}"
        )
    return relative


def _destination(target: InstallTarget, relative: PurePosixPath) -> Path:
    parts = relative.parts
    if parts and parts[0].endswith(".data"):
        if len(parts) < 3 or parts[1] not in {
            "purelib",
            "platlib",
            "scripts",
            "data",
            "headers",
        }:
            raise InstallationError(f"invalid wheel data path: {relative}")
    for index in range(len(parts) - 1):
        if parts[index].endswith(".data") and index + 1 < len(parts):
            base = {
                "purelib": target.purelib,
                "platlib": target.platlib,
                "scripts": target.scripts,
                "data": target.data,
                "headers": target.headers,
            }.get(parts[index + 1])
            if base is not None:
                return _safe_destination(base, PurePosixPath(*parts[index + 2 :]))
    return _safe_destination(target.purelib, relative)


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    destination = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        destination.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise InstallationError(
            f"wheel member escapes installation root: {relative}"
        ) from exc
    return destination


def _zip_mode(info: zipfile.ZipInfo) -> int | None:
    mode = info.external_attr >> 16
    return mode if mode and stat.S_ISREG(mode) else None


def _is_script_member(relative: PurePosixPath) -> bool:
    return len(relative.parts) >= 2 and relative.parts[-2] == "scripts"


def _rewrite_shebang(path: Path, executable: str | None) -> None:
    contents = path.read_bytes()
    if contents.startswith(b"#!python\n"):
        path.write_bytes(
            f"#!{executable or os.sys.executable}\n".encode()
            + contents[len(b"#!python\n") :]
        )


def _entry_point_scripts(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    active = False
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            active = line[1:-1].strip() in {"console_scripts", "gui_scripts"}
        elif active and "=" in line and not line.startswith("#"):
            name, target = line.split("=", 1)
            result[name.strip()] = target.strip()
    return result


def _script_text(target_ref: str, executable: str | None) -> str:
    module, _, attribute = target_ref.partition(":")
    entry = attribute or "main"
    return (
        f"#!{executable or os.sys.executable}\n"
        "import re\nimport sys\n"
        f"from {module} import {entry}\n\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({entry}())\n"
    )


def _script_matches(path: Path, scripts: dict[str, str]) -> bool:
    target_ref = scripts.get(path.name)
    if target_ref is None:
        return False
    module, _, attribute = target_ref.partition(":")
    entry = attribute or "main"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return f"from {module} import {entry}" in text


def _compiled_files(
    stage_root: Path,
    staged: Iterable[tuple[Path, Path, int | None]],
) -> list[tuple[Path, Path, int | None]]:
    result = []
    for source, destination, _ in staged:
        if source.suffix != ".py":
            continue
        if not compileall.compile_file(os.fspath(source), force=True, quiet=True):
            continue
        cache = Path(importlib.util.cache_from_source(os.fspath(source)))
        if cache.is_file():
            relative = cache.relative_to(stage_root)
            result.append(
                (cache, destination.parent / Path(*relative.parts[-2:]), None)
            )
    return result


def _existing_paths(
    distribution: InstalledMetadataDistribution | None,
) -> tuple[set[Path], set[Path]]:
    if distribution is None:
        return set(), set()
    entries = distribution.iter_declared_entries()
    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            distribution.read_text("RECORD")
        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot replace {distribution.raw_name} {distribution.version}: "
                "no RECORD file was found"
            ) from exc
    paths = {
        (Path(distribution.location) / entry).resolve(strict=False) for entry in entries
    }
    existing = {path for path in paths if path.exists() or path.is_symlink()}
    return existing, existing


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
