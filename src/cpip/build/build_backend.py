from __future__ import annotations

import contextlib
import base64
import configparser
import csv
import email.parser
import hashlib
import importlib
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    from cpip._vendor import tomli as tomllib
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from dataclasses import dataclass

from cpip.core.errors import BuildError
from cpip.core.packaging import (
    InvalidVersion,
    Version,
    canonicalize_name,
    parse_requirement,
)
from cpip.core.subprocess import call_subprocess

if TYPE_CHECKING:
    from cpip.build.pep517_hooks import BuildBackendHookCaller


class BuildWheelHook(Protocol):
    def __call__(
        self,
        wheel_directory: str,
        *,
        config_settings: dict[str, Any] | None,
        metadata_directory: str | None,
    ) -> str: ...


# ``pkg_resources`` was removed from setuptools 82.  Projects that only have a
# setup.py are still built through setuptools' legacy backend, and a number of
# otherwise installable projects import pkg_resources from setup.py.  Keep the
# legacy fallback on the last setuptools line that provides that API.  This is
# intentionally limited to legacy projects; modern pyproject builds own their
# build requirements and should be free to select a newer setuptools release.
LEGACY_SETUPTOOLS_REQUIREMENT = "setuptools>=40.8.0,<82"


@dataclass(frozen=True)
class BackendSpec:
    """The build backend and requirements declared by a project."""

    name: str
    requirements: tuple[str, ...]
    backend_path: tuple[str, ...]

    @classmethod
    def from_project(cls, source_dir: Path) -> BackendSpec | None:
        source_text = os.fspath(source_dir)
        pyproject = os.path.join(source_text, "pyproject.toml")
        setup_py = os.path.join(source_text, "setup.py")
        if not os.path.isfile(pyproject):
            if os.path.isfile(setup_py):
                return cls(
                    "setuptools.build_meta:__legacy__",
                    (LEGACY_SETUPTOOLS_REQUIREMENT,),
                    (),
                )
            return None

        with open(pyproject, encoding="utf-8") as file:
            data = tomllib.loads(file.read())
        build_system = data.get("build-system")
        if not isinstance(build_system, dict):
            return None
        backend = build_system.get("build-backend", "setuptools.build_meta")
        if not isinstance(backend, str) or backend in {
            "cpip.build.build_backend",
            "uv_build",
        }:
            return None
        requires = build_system.get("requires", [])
        if not isinstance(requires, list) or not all(
            isinstance(item, str) for item in requires
        ):
            raise BuildError(f"Invalid build-system.requires in {pyproject}")
        setup_uses_pkg_resources = False
        if os.path.isfile(setup_py):
            with open(setup_py, encoding="utf-8") as file:
                setup_uses_pkg_resources = "pkg_resources" in file.read()
        if (
            backend.startswith("setuptools.build_meta")
            and os.path.isfile(setup_py)
            and setup_uses_pkg_resources
            and not any(
                canonicalize_name(parse_requirement(item).name) == "setuptools"
                and not parse_requirement(item).specifier.contains(
                    Version("81"), allow_prereleases=True
                )
                for item in requires
            )
        ):
            # setuptools 82 removed pkg_resources, which is still imported by
            # many setup.py files. Preserve explicit requirements that exclude
            # the compatible range, but constrain open-ended requirements.
            requires.append("setuptools<82")
        backend_path = build_system.get("backend-path", [])
        if not isinstance(backend_path, list) or not all(
            isinstance(item, str) for item in backend_path
        ):
            raise BuildError(f"Invalid build-system.backend-path in {pyproject}")
        return cls(backend, tuple(requires), tuple(backend_path))


class BackendRunner:
    """Run hooks in an isolated environment for an external backend."""

    def __init__(
        self,
        source_dir: Path,
        spec: BackendSpec,
        *,
        build_constraints: list[str] | None = None,
        build_isolation: bool = True,
    ) -> None:
        self.source_dir = source_dir
        self.spec = spec
        self.build_constraints = build_constraints
        self.build_isolation = build_isolation

    @contextlib.contextmanager
    def caller(self) -> Iterator[tuple[BuildBackendHookCaller, Path]]:
        from cpip.build.pep517_hooks import BuildBackendHookCaller

        if not self.build_isolation:
            with tempfile.TemporaryDirectory(
                prefix="pip-build-metadata-"
            ) as metadata_dir:
                caller = BuildBackendHookCaller(
                    os.fspath(self.source_dir),
                    self.spec.name,
                    backend_path=list(self.spec.backend_path) or None,
                    python_executable=sys.executable,
                )
                with backend_environment(self.source_dir):
                    yield caller, Path(metadata_dir)
            return

        with tempfile.TemporaryDirectory(prefix="pip-build-env-") as env_dir:
            env_path = Path(env_dir)
            # ``virtualenv`` handles relocated Python distributions (such as
            # uv-managed interpreters) whose stdlib ``venv`` launcher cannot
            # locate its installation prefix.  Keep the stdlib fallback for
            # installations that do not provide virtualenv.
            try:
                import virtualenv
            except ImportError:
                import venv

                builder = venv.EnvBuilder(symlinks=(os.name != "nt"), with_pip=False)
                builder.create(env_path)
                bootstrap_environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("CPIP_") and key != "PYTHONPATH"
                }
                subprocess.run(
                    [
                        os.fspath(
                            env_path
                            / (
                                "bin/python"
                                if os.name != "nt"
                                else "Scripts/python.exe"
                            )
                        ),
                        "-m",
                        "ensurepip",
                        "--upgrade",
                        "--default-pip",
                    ],
                    check=True,
                    cwd=env_path,
                    env=bootstrap_environment,
                    capture_output=True,
                    text=True,
                )
            else:
                virtualenv.cli_run([os.fspath(env_path), "--no-download", "--clear"])
            python = env_path / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            if self.spec.requirements:
                constraint_args = [
                    argument
                    for constraint in self.build_constraints or ()
                    for argument in ("--constraint", constraint)
                ]
                environment = os.environ.copy()
                environment.pop("CPIP_CONSTRAINT", None)
                # CPIP_FIND_LINKS is a shell-style, space-separated option.
                # Splitting it with os.pathsep breaks file:// URLs on macOS
                # (the colon is part of the scheme).
                local_find_links = shlex.split(environment.get("CPIP_FIND_LINKS", ""))
                install_options = [
                    option
                    for link in local_find_links
                    if link
                    for option in ("--find-links", link)
                ]
                # The bootstrap environment may contain setuptools from
                # ensurecpip. Build requirements must still be resolved and
                # installed through the configured index/proxy.
                install_options.insert(0, "--ignore-installed")
                no_index = environment.get("CPIP_NO_INDEX", "").lower()
                if no_index in {"1", "true", "yes", "on"}:
                    install_options.insert(0, "--no-index")
                else:
                    environment.pop("CPIP_NO_INDEX", None)
                if any(
                    requirement.split("[", 1)[0].split(" ", 1)[0].lower()
                    == "setuptools"
                    for requirement in self.spec.requirements
                ):
                    # Old setuptools sdists do not provide
                    # setuptools.build_meta. Prefer the wheel when the
                    # caller made one available in its local find-links.
                    install_options.extend(("--only-binary", "setuptools"))
                try:
                    subprocess.run(
                        [
                            os.fspath(python),
                            "-m",
                            "pip",
                            "install",
                            *install_options,
                            *constraint_args,
                            *self.spec.requirements,
                        ],
                        check=True,
                        cwd=self.source_dir,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError as exc:
                    detail = "\n".join(
                        part for part in (exc.stdout, exc.stderr) if part
                    )
                    # A missing host setuptools installation can be recovered
                    # from, but a failed constrained build must remain a
                    # failure.  Falling back unconditionally would silently
                    # bypass --build-constraint.
                    if (
                        not self.build_constraints
                        and self.spec.name.startswith("setuptools.build_meta")
                        and (
                            "Cannot import 'setuptools.build_meta'" in detail
                            or "No matching distribution found for setuptools" in detail
                        )
                    ):
                        import importlib.util

                        if (
                            importlib.util.find_spec("setuptools.build_meta")
                            is not None
                        ):
                            with tempfile.TemporaryDirectory(
                                prefix="pip-build-metadata-"
                            ) as metadata_dir:
                                caller = BuildBackendHookCaller(
                                    os.fspath(self.source_dir),
                                    self.spec.name,
                                    backend_path=list(self.spec.backend_path) or None,
                                    python_executable=sys.executable,
                                )
                                with backend_environment(self.source_dir):
                                    yield caller, Path(metadata_dir)
                            return
                    raise RuntimeError(detail or str(exc)) from exc
            caller = BuildBackendHookCaller(
                os.fspath(self.source_dir),
                self.spec.name,
                backend_path=list(self.spec.backend_path) or None,
                python_executable=os.fspath(python),
            )
            with backend_environment(self.source_dir):
                yield caller, env_path


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return ProjectBuilder(Path.cwd()).build_wheel(
        Path(wheel_directory), config_settings=config_settings
    )


def get_requires_for_build_wheel(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return []


def get_requires_for_build_sdist(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return []


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    project = ProjectMetadataReader(Path.cwd()).read()
    dist_info = f"{wheel_distribution(project.name)}-{project.version}.dist-info"
    target = Path(metadata_directory) / dist_info
    target.mkdir(parents=True, exist_ok=True)
    (target / "METADATA").write_text(metadata_text(project), encoding="utf-8")
    (target / "WHEEL").write_text(wheel_text_internal(), encoding="utf-8")
    return dist_info


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return ProjectBuilder(Path.cwd()).build_editable(
        Path(wheel_directory), config_settings=config_settings
    )


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    del config_settings
    source_dir = Path.cwd()
    project = ProjectMetadataReader(source_dir).read()
    sdist_name = f"{wheel_distribution(project.name)}-{project.version}.tar.gz"
    sdist_path = Path(sdist_directory) / sdist_name
    sdist_path.parent.mkdir(parents=True, exist_ok=True)
    root_name = sdist_name.removesuffix(".tar.gz")
    with tarfile.open(sdist_path, "w:gz") as archive:
        source_dir_text = os.fspath(source_dir)
        for current, directories, files in os.walk(
            source_dir_text, topdown=True, followlinks=False
        ):
            directories[:] = sorted(name for name in directories if name != ".git")
            for name in sorted(files):
                child = os.path.join(current, name)
                relative = os.path.relpath(child, source_dir_text)
                archive.add(
                    child,
                    arcname=f"{root_name}/{relative.replace(os.sep, '/')}",
                )
    return sdist_name


def get_requires_for_build_editable(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return []


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    return prepare_metadata_for_build_wheel(metadata_directory, config_settings)


class ProjectBuilder:
    """Build a project through its declared backend or pip's fallback backend."""

    def __init__(
        self,
        source_dir: Path,
        *,
        build_constraints: list[str] | None = None,
        build_isolation: bool = True,
    ) -> None:
        self.source_dir = source_dir
        self.build_constraints = build_constraints
        self.build_isolation = build_isolation
        self.backend_spec = BackendSpec.from_project(source_dir)

    def build_wheel(
        self,
        wheel_directory: Path,
        *,
        config_settings: dict[str, Any] | None = None,
    ) -> str:
        backend = self.load_backend_hook("build_wheel")
        if self.backend_spec is not None:
            self.prepare_metadata()
            return self.build_external(wheel_directory, config_settings=config_settings)
        if callable(backend) and backend is not build_wheel:
            with backend_environment(self.source_dir):
                wheel_name = backend(
                    os.fspath(wheel_directory),
                    config_settings=config_settings,
                    metadata_directory=None,
                )
            if not isinstance(wheel_name, str):
                raise BuildError("Build backend did not return a wheel filename")
            return wheel_name
        return self.build_fallback_wheel(wheel_directory, editable=False)

    def build_editable(
        self,
        wheel_directory: Path,
        *,
        config_settings: dict[str, Any] | None = None,
    ) -> str:
        backend = self.load_backend_hook("build_editable")
        if (
            self.backend_spec is not None
            and self.backend_spec.name.startswith("setuptools.build_meta")
            and os.path.isfile(os.path.join(os.fspath(self.source_dir), "setup.py"))
        ):
            return self.build_external(
                wheel_directory, config_settings=config_settings, editable=False
            )
        if self.backend_spec is not None:
            return self.build_external(
                wheel_directory, config_settings=config_settings, editable=True
            )
        if callable(backend) and backend is not build_editable:
            with backend_environment(self.source_dir):
                wheel_name = backend(
                    os.fspath(wheel_directory),
                    config_settings=config_settings,
                    metadata_directory=None,
                )
            if not isinstance(wheel_name, str):
                raise BuildError(
                    "Build backend did not return an editable wheel filename"
                )
            return wheel_name
        return self.build_fallback_wheel(wheel_directory, editable=True)

    def load_backend_hook(self, name: str) -> BuildWheelHook | None:
        backend = load_project_backend(self.source_dir)
        hook = getattr(backend, name, None) if backend is not None else None
        return cast(BuildWheelHook, hook) if callable(hook) else None

    def build_external(
        self,
        wheel_directory: Path,
        *,
        config_settings: dict[str, Any] | None,
        editable: bool = False,
    ) -> str:
        assert self.backend_spec is not None
        from cpip.build.pep517_hooks import HookMissing

        os.makedirs(os.fspath(wheel_directory), exist_ok=True)
        backend_name = self.backend_spec.name
        try:
            with BackendRunner(
                self.source_dir,
                self.backend_spec,
                build_constraints=self.build_constraints,
                build_isolation=self.build_isolation,
            ).caller() as (caller, _):
                with caller.subprocess_runner(call_subprocess):
                    if editable:
                        wheel_name = caller.build_editable(
                            os.fspath(wheel_directory), config_settings=config_settings
                        )
                    else:
                        wheel_name = caller.build_wheel(
                            os.fspath(wheel_directory), config_settings=config_settings
                        )
        except HookMissing as exc:
            if editable:
                raise BuildError(
                    "Cannot build editable "
                    f"{self.source_dir} because the build backend is missing "
                    "the 'build_editable' hook"
                ) from exc
            raise BuildError(
                f"Build backend {backend_name} is missing the 'build_wheel' hook"
            ) from exc
        except Exception as exc:
            raise BuildError(
                f"Failed to build {self.source_dir} with {backend_name}: {exc}"
            ) from exc
        if not isinstance(wheel_name, str):
            raise BuildError(
                f"Build backend {backend_name} did not return a wheel filename"
            )
        return wheel_name

    def build_fallback_wheel(self, wheel_directory: Path, *, editable: bool) -> str:
        project = ProjectMetadataReader(self.source_dir).read()
        os.makedirs(os.fspath(wheel_directory), exist_ok=True)
        distribution = wheel_distribution(project.name)
        wheel_name = f"{distribution}-{project.version}-py3-none-any.whl"
        wheel_path = wheel_directory / wheel_name
        dist_info = f"{distribution}-{project.version}.dist-info"
        records: list[tuple[str, bytes]] = []

        def write_file(archive: zipfile.ZipFile, path: str, data: bytes | str) -> None:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            archive.writestr(path, raw)
            records.append((path, raw))

        with zipfile.ZipFile(
            wheel_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            if editable:
                import_root = (
                    self.source_dir / "src"
                    if os.path.isdir(os.fspath(self.source_dir / "src"))
                    else self.source_dir
                )
                write_file(
                    archive,
                    f"__editable__.{distribution}.pth",
                    str(import_root.resolve()) + "\n",
                )
            else:
                project_files = list(iter_project_files(self.source_dir))
                version_path = version_module_path(project.name, project_files)
                if version_path is not None:
                    project_files = [
                        (path, data)
                        for path, data in project_files
                        if path != version_path
                    ]
                for path, data in project_files:
                    write_file(archive, path, data)
                if version_path is not None:
                    write_file(
                        archive,
                        version_path,
                        f"__version__ = {project.version!r}\n",
                    )
            write_file(archive, f"{dist_info}/METADATA", metadata_text(project))
            write_file(archive, f"{dist_info}/WHEEL", wheel_text_internal())
            entry_points = entry_points_text_internal(project)
            if entry_points:
                write_file(archive, f"{dist_info}/entry_points.txt", entry_points)
            archive.writestr(
                f"{dist_info}/RECORD", record_text_internal(records, dist_info)
            )
        return wheel_name

    def prepare_metadata(self, *, editable: bool = False) -> ProjectMetadata:
        """Read metadata through the project's declared build backend."""
        # Source distributions commonly carry the exact metadata generated at
        # upload time. Reuse it during resolution instead of executing a
        # potentially old or platform-specific setup.py just to discover
        # dependencies. Wheel builds still invoke the declared backend below.
        static_metadata = read_legacy_metadata(self.source_dir)
        if static_metadata is not None and not editable:
            return static_metadata
        if self.backend_spec is None:
            return ProjectMetadataReader(self.source_dir).read()
        backend_name = self.backend_spec.name
        from cpip.build.pep517_hooks import HookMissing

        try:
            with BackendRunner(
                self.source_dir,
                self.backend_spec,
                build_constraints=self.build_constraints,
                build_isolation=self.build_isolation,
            ).caller() as (
                caller,
                env_path,
            ):
                metadata_path = Path(env_path) / "metadata"
                metadata_path.mkdir()
                metadata = None
                with caller.subprocess_runner(call_subprocess):
                    if editable:
                        try:
                            dist_info = caller.prepare_metadata_for_build_editable(
                                os.fspath(metadata_path)
                            )
                        except HookMissing:
                            with tempfile.TemporaryDirectory(
                                prefix="cpip-metadata-editable-"
                            ) as wheel_directory:
                                wheel_name = caller.build_editable(wheel_directory)
                                assert wheel_name is not None
                                wheel_path = Path(wheel_directory) / wheel_name
                                with zipfile.ZipFile(wheel_path) as wheel:
                                    metadata_name = next(
                                        name
                                        for name in wheel.namelist()
                                        if name.endswith(".dist-info/METADATA")
                                    )
                                    metadata = email.parser.BytesParser().parsebytes(
                                        wheel.read(metadata_name)
                                    )
                            dist_info = None
                    else:
                        try:
                            dist_info = caller.prepare_metadata_for_build_wheel(
                                os.fspath(metadata_path)
                            )
                        except HookMissing:
                            with tempfile.TemporaryDirectory(
                                prefix="cpip-metadata-wheel-"
                            ) as wheel_directory:
                                wheel_name = caller.build_wheel(wheel_directory)
                                assert wheel_name is not None
                                wheel_path = Path(wheel_directory) / wheel_name
                                with zipfile.ZipFile(wheel_path) as wheel:
                                    metadata_name = next(
                                        name
                                        for name in wheel.namelist()
                                        if name.endswith(".dist-info/METADATA")
                                    )
                                    metadata = email.parser.BytesParser().parsebytes(
                                        wheel.read(metadata_name)
                                    )
                            dist_info = None
                if not editable and dist_info is None and metadata is None:
                    with tempfile.TemporaryDirectory(
                        prefix="cpip-metadata-wheel-"
                    ) as wheel_directory:
                        wheel_name = caller.build_wheel(wheel_directory)
                        assert wheel_name is not None
                        wheel_path = Path(wheel_directory) / wheel_name
                        with zipfile.ZipFile(wheel_path) as wheel:
                            metadata_name = next(
                                name
                                for name in wheel.namelist()
                                if name.endswith(".dist-info/METADATA")
                            )
                            metadata = email.parser.BytesParser().parsebytes(
                                wheel.read(metadata_name)
                            )
                if dist_info is not None:
                    metadata = email.parser.Parser().parsestr(
                        (metadata_path / dist_info / "METADATA").read_text(
                            encoding="utf-8"
                        )
                    )
                if metadata is None:
                    raise BuildError("Build backend returned no metadata")
        except Exception as exc:
            raise BuildError(
                "Failed to prepare metadata for "
                f"{self.source_dir} with {backend_name}: {exc}"
            ) from exc
        name = metadata.get("Name")
        version = metadata.get("Version")
        if not name or not version:
            raise BuildError(
                f"Build backend {backend_name} returned incomplete metadata"
            )
        return ProjectMetadata(
            name=name,
            version=version,
            summary=metadata.get("Summary"),
            requires_python=metadata.get("Requires-Python"),
            dependencies=tuple(metadata.get_all("Requires-Dist", [])),
            optional_dependencies={},
            scripts={},
            provided_extras=frozenset(metadata.get_all("Provides-Extra", [])),
        )


def prepare_project_metadata(
    source_dir: Path,
    *,
    editable: bool = False,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> ProjectMetadata:
    """Read metadata through the project's declared PEP 517 backend."""
    try:
        return ProjectBuilder(
            source_dir,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        ).prepare_metadata(editable=editable)
    except BuildError as exc:
        if build_isolation and "Cannot import 'setuptools.build_meta'" in str(exc):
            return ProjectBuilder(
                source_dir,
                build_constraints=build_constraints,
                build_isolation=False,
            ).prepare_metadata(editable=editable)
        raise


def load_project_backend(source_dir: Path) -> object | None:
    pyproject = source_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    build_system = data.get("build-system")
    if not isinstance(build_system, dict):
        return None
    backend_name = build_system.get("build-backend")
    if not isinstance(backend_name, str) or backend_name.startswith(
        "setuptools.build_meta"
    ):
        return None
    if backend_name in {"cpip.build.build_backend", "uv_build"}:
        return None
    backend_path = build_system.get("backend-path", [])
    import_paths = backend_paths(source_dir, backend_path)
    module_name, _, object_path = backend_name.partition(":")
    with backend_import_path(import_paths):
        importlib.invalidate_caches()
        sys.modules.pop(module_name, None)
        backend: object = importlib.import_module(module_name)
    for attribute in object_path.split("."):
        if attribute:
            backend = getattr(backend, attribute)
    return backend


def backend_paths(source_dir: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str((source_dir / path).resolve()) for path in paths)


@contextlib.contextmanager
def backend_import_path(paths: tuple[str, ...]) -> Iterator[None]:
    sys.path[:0] = list(paths)
    try:
        yield
    finally:
        del sys.path[: len(paths)]


@contextlib.contextmanager
def backend_environment(source_dir: Path) -> Iterator[None]:
    cwd = Path.cwd()
    source = os.fspath(source_dir)
    old_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = [source]
    if old_pythonpath:
        pythonpath.append(old_pythonpath)
    os.chdir(source_dir)
    os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath)
    try:
        yield
    finally:
        os.chdir(cwd)
        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pythonpath


@dataclass(frozen=True)
class ProjectMetadata:
    name: str
    version: str
    summary: str | None
    requires_python: str | None
    dependencies: tuple[str, ...]
    optional_dependencies: dict[str, tuple[str, ...]]
    scripts: dict[str, str]
    provided_extras: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", str(Version(self.version)))


class ProjectMetadataReader:
    """Read project metadata from a source tree and its legacy fallbacks."""

    def __init__(self, source_dir: Path) -> None:
        self.source_dir = source_dir

    def read(self) -> ProjectMetadata:
        source_dir = self.source_dir
        pyproject = source_dir / "pyproject.toml"
        if os.path.exists(pyproject):
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project")
            if isinstance(project, dict):
                name = project.get("name")
                version = project.get("version")
                if (
                    isinstance(name, str)
                    and name
                    and isinstance(version, str)
                    and version
                ):
                    Version(version)
                    dependencies = project.get("dependencies", [])
                    if not isinstance(dependencies, list) or not all(
                        isinstance(item, str) for item in dependencies
                    ):
                        raise BuildError(
                            f"Cannot build {source_dir}: project.dependencies is invalid"
                        )
                    scripts = project.get("scripts", {})
                    if not isinstance(scripts, dict):
                        scripts = {}
                    summary = (
                        project.get("description")
                        if isinstance(project.get("description"), str)
                        else None
                    )
                    requires_python = (
                        project.get("requires-python")
                        if isinstance(project.get("requires-python"), str)
                        else None
                    )
                    optional_dependencies_raw = project.get("optional-dependencies", {})
                    optional_dependencies: dict[str, tuple[str, ...]] = {}
                    if isinstance(optional_dependencies_raw, dict):
                        for extra, values in optional_dependencies_raw.items():
                            if not isinstance(extra, str) or not isinstance(
                                values, list
                            ):
                                continue
                            items = [
                                str(item) for item in values if isinstance(item, str)
                            ]
                            optional_dependencies[extra] = tuple(items)
                    return ProjectMetadata(
                        name=name,
                        version=version,
                        summary=summary,
                        requires_python=requires_python,
                        dependencies=tuple(dependencies),
                        optional_dependencies=optional_dependencies,
                        scripts={
                            str(key): str(value) for key, value in scripts.items()
                        },
                    )
            setup_py = source_dir / "setup.py"
            if not setup_py.exists():
                metadata = read_legacy_metadata(source_dir)
                if metadata is not None:
                    return metadata
                metadata = read_setup_cfg_metadata(source_dir)
                if metadata is not None:
                    return metadata
                metadata = infer_metadata_from_package_dir(source_dir)
                if metadata is not None:
                    return metadata
                if isinstance(project, dict):
                    if not isinstance(name, str) or not name:
                        raise BuildError(
                            f"Cannot build {source_dir}: missing project.name"
                        )
                    raise BuildError(
                        f"Cannot build {source_dir}: missing project.version"
                    )
                raise BuildError(
                    f"Cannot build {source_dir}: missing [project] metadata"
                )
            setup_cfg_metadata = read_setup_cfg_metadata(source_dir)
            if setup_cfg_metadata is not None:
                return setup_cfg_metadata
            raise BuildError(
                f"Cannot read metadata for {source_dir}: use the project's build backend"
            )
        else:
            setup_py = source_dir / "setup.py"
            if not setup_py.exists():
                metadata = read_legacy_metadata(source_dir)
                if metadata is not None:
                    return metadata
                metadata = read_setup_cfg_metadata(source_dir)
                if metadata is not None:
                    return metadata
                metadata = infer_metadata_from_package_dir(source_dir)
                if metadata is not None:
                    return metadata
                raise BuildError(f"Cannot build {source_dir}: missing pyproject.toml")
            setup_cfg_metadata = read_setup_cfg_metadata(source_dir)
            if setup_cfg_metadata is not None:
                return setup_cfg_metadata
            raise BuildError(
                f"Cannot read metadata for {source_dir}: use the project's build backend"
            )


def read_setup_cfg_metadata(source_dir: Path) -> ProjectMetadata | None:
    setup_cfg = source_dir / "setup.cfg"
    if not setup_cfg.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(setup_cfg, encoding="utf-8")
    except configparser.Error:
        return None
    if not parser.has_section("metadata"):
        return None
    name = parser.get("metadata", "name", fallback="").strip()
    version = parser.get("metadata", "version", fallback="").strip()
    if not name or not version:
        return None
    try:
        Version(version)
    except InvalidVersion:
        return None
    summary = parser.get("metadata", "description", fallback="").strip() or None
    requires_python = (
        parser.get("options", "python_requires", fallback="").strip() or None
    )
    dependencies = setup_cfg_install_requires(parser)
    return ProjectMetadata(
        name=name,
        version=version,
        summary=summary,
        requires_python=requires_python,
        dependencies=dependencies,
        optional_dependencies={},
        scripts={},
    )


def setup_cfg_install_requires(
    parser: configparser.ConfigParser,
) -> tuple[str, ...]:
    if not parser.has_option("options", "install_requires"):
        return ()
    raw = parser.get("options", "install_requires")
    dependencies: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        dependencies.append(stripped)
    return tuple(dependencies)


def infer_metadata_from_package_dir(source_dir: Path) -> ProjectMetadata | None:
    roots = []
    src_root = source_dir / "src"
    if os.path.isdir(os.fspath(src_root)):
        roots.append(src_root)
    roots.append(source_dir)
    for root in roots:
        with os.scandir(os.fspath(root)) as entries:
            package_entries = sorted(entries, key=lambda entry: entry.name)
        for entry in package_entries:
            if not entry.is_dir():
                continue
            init_py = os.path.join(entry.path, "__init__.py")
            if not os.path.isfile(init_py):
                continue
            with open(init_py, encoding="utf-8", errors="replace") as file:
                text = file.read()
            match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
            if match is None:
                continue
            try:
                Version(match.group(1))
            except InvalidVersion:
                continue
            return ProjectMetadata(
                name=entry.name,
                version=match.group(1),
                summary=None,
                requires_python=None,
                dependencies=(),
                optional_dependencies={},
                scripts={},
            )
    return None


def read_legacy_metadata(source_dir: Path) -> ProjectMetadata | None:
    source_text = os.fspath(source_dir)
    with os.scandir(source_text) as entries:
        egg_info_candidates = []
        dist_info_candidates = []
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name.endswith(".egg-info"):
                egg_info_candidates.append(os.path.join(entry.path, "PKG-INFO"))
            elif entry.name.endswith(".dist-info"):
                dist_info_candidates.append(os.path.join(entry.path, "METADATA"))
    candidates = (
        sorted(egg_info_candidates)
        + [
            os.path.join(source_text, "METADATA"),
            os.path.join(source_text, "PKG-INFO"),
        ]
        + sorted(dist_info_candidates)
    )
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        fields: dict[str, list[str]] = {}
        current_key: str | None = None
        with open(candidate, encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
        for line in lines:
            if not line.strip():
                current_key = None
                continue
            if line[:1].isspace() and current_key is not None:
                fields[current_key][-1] += " " + line.strip()
                continue
            if ":" not in line:
                continue
            current_key, value = line.split(":", 1)
            fields.setdefault(current_key, []).append(value.strip())
        name = fields.get("Name", [None])[0]
        version = fields.get("Version", [None])[0]
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
        ):
            continue
        Version(version)
        summary = fields.get("Summary", [None])[0]
        requires_python = fields.get("Requires-Python", [None])[0]
        dependencies = fields.get("Requires-Dist", [])
        if not dependencies and os.path.basename(os.path.dirname(candidate)).endswith(
            ".egg-info"
        ):
            requires_path = os.path.join(os.path.dirname(candidate), "requires.txt")
            if os.path.isfile(requires_path):
                dependencies = _read_legacy_requirements(Path(requires_path))
        return ProjectMetadata(
            name=name,
            version=version,
            summary=summary if isinstance(summary, str) else None,
            requires_python=(
                requires_python if isinstance(requires_python, str) else None
            ),
            dependencies=tuple(dependencies),
            optional_dependencies={},
            scripts={},
        )
    return None


def _read_legacy_requirements(path: Path) -> list[str]:
    """Read setuptools' legacy ``requires.txt`` format."""
    dependencies: list[str] = []
    extra: str | None = None
    with open(os.fspath(path), encoding="utf-8", errors="replace") as file:
        lines = file.read().splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            extra = line[1:-1].strip() or None
            continue
        if line.startswith("-"):
            continue
        dependencies.append(
            f'{line}; extra == "{extra}"' if extra is not None else line
        )
    return dependencies


def infer_name_version_from_path(source_dir: Path) -> tuple[str, str] | None:
    name = source_dir.name
    if "-" not in name:
        return None
    pkg_name, version = name.rsplit("-", 1)
    if not pkg_name or not version:
        return None
    try:
        Version(version)
    except InvalidVersion:
        return None
    return pkg_name, version


def iter_project_files(source_dir: Path) -> Iterable[tuple[str, bytes]]:
    src_root = source_dir / "src"
    if os.path.isdir(os.fspath(src_root)):
        yield from iter_package_files(src_root)
        return

    with os.scandir(os.fspath(source_dir)) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for entry in children:
        if entry.name.startswith("."):
            continue
        child_path = Path(entry.path)
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "__init__.py")):
            yield from iter_package_files(source_dir, root=child_path)
        elif (
            entry.is_file() and entry.name.endswith(".py") and entry.name != "setup.py"
        ):
            with open(entry.path, "rb") as file:
                yield entry.name, file.read()


def version_module_path(
    project_name: str, project_files: list[tuple[str, bytes]]
) -> str | None:
    package_names = sorted(
        {
            path.split("/", 1)[0]
            for path, _ in project_files
            if "/" in path and path.endswith("/__init__.py")
        }
    )
    if not package_names:
        return None
    expected_name = project_name.replace("-", "_")
    package_name = expected_name if expected_name in package_names else package_names[0]
    return f"{package_name}/_version.py"


def iter_package_files(
    base: Path, *, root: Path | None = None
) -> Iterable[tuple[str, bytes]]:
    base_text = os.fspath(base)
    search_root = root or base
    search_root_text = os.fspath(search_root)
    project_root = base.parent if base.name == "src" else base
    project_root_text = os.fspath(project_root)
    for current, directories, files in os.walk(
        search_root_text, topdown=True, followlinks=False
    ):
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".") and name != "__pycache__"
        )
        for name in sorted(files):
            path = os.path.join(current, name)
            if not _is_package_payload_text(path, project_root_text):
                continue
            relative = os.path.relpath(path, base_text)
            with open(path, "rb") as file:
                yield relative.replace(os.sep, "/"), file.read()


def _is_package_payload_text(path: str, project_root: str) -> bool:
    if not os.path.isfile(path):
        return False
    relative_path = os.path.relpath(path, project_root)
    relative_parts = relative_path.split(os.sep)
    if any(part.startswith(".") for part in relative_parts):
        return False
    if "__pycache__" in relative_parts or os.path.splitext(path)[1] in {
        ".pyc",
        ".pyo",
    }:
        return False
    if not os.path.islink(path):
        return True
    try:
        resolved = os.path.realpath(path)
        return os.path.exists(resolved) and os.path.commonpath(
            (resolved, os.path.realpath(project_root))
        ) == os.path.realpath(project_root)
    except (OSError, ValueError):
        return False


def is_package_payload(path: Path, project_root: Path) -> bool:
    return _is_package_payload_text(os.fspath(path), os.fspath(project_root))


def metadata_text(project: ProjectMetadata) -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {project.name}",
        f"Version: {project.version}",
    ]
    if project.summary:
        lines.append(f"Summary: {project.summary}")
    if project.requires_python:
        lines.append(f"Requires-Python: {project.requires_python}")
    for dependency in project.dependencies:
        lines.append(f"Requires-Dist: {dependency}")
    for extra, dependencies in sorted(project.optional_dependencies.items()):
        lines.append(f"Provides-Extra: {extra}")
        for dependency in dependencies:
            if "; " in dependency:
                requirement, marker = dependency.split("; ", 1)
                lines.append(
                    f'Requires-Dist: {requirement}; ({marker}) and extra == "{extra}"'
                )
            else:
                lines.append(f'Requires-Dist: {dependency}; extra == "{extra}"')
    return "\n".join(lines) + "\n"


def wheel_text_internal() -> str:
    return "\n".join(
        [
            "Wheel-Version: 1.0",
            "Generator: pip-core",
            "Root-Is-Purelib: true",
            "Tag: py3-none-any",
            "",
        ]
    )


def entry_points_text_internal(project: ProjectMetadata) -> str:
    scripts = dict(project.scripts)
    if project.name == "pip" and not scripts:
        scripts = {"pip": "cpip.cli.main:main"}
    if not scripts:
        return ""
    lines = ["[console_scripts]"]
    lines.extend(f"{name} = {target}" for name, target in sorted(scripts.items()))
    return "\n".join(lines) + "\n"


def record_text_internal(records: list[tuple[str, bytes]], dist_info: str) -> str:
    rows: list[tuple[str, str, str]] = []
    for path, data in records:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append((path, f"sha256={digest.decode('ascii')}", str(len(data))))
    rows.append((f"{dist_info}/RECORD", "", ""))
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue()


def wheel_distribution(name: str) -> str:
    normalized = canonicalize_name(name).replace("-", "_")
    return re.sub(r"[^A-Za-z0-9_.]+", "_", normalized)
