from __future__ import annotations

import logging
import os
import sys

from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Protocol

from cpip.core.errors import (
    DiagnosticCpipError,
    InstallationError,
)
from cpip.core.direct_url import ArchiveInfo, DirInfo
from cpip.core.hashes import Hashes
from cpip.index.links import Link
from cpip.core.packaging import (
    Requirement as ParsedRequirement,
    SpecifierSet,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.resolution.requirements.backend import ConfiguredBuildBackend
from cpip.resolution.requirements.paths import looks_like_path

if TYPE_CHECKING:
    import email.message


def file_hashes(path: str | Path) -> dict[str, str]:
    from cpip.core.hashes import file_hashes as compute_file_hashes

    return compute_file_hashes(path)


logger = logging.getLogger(__name__)


class InvalidPyProjectBuildRequires(DiagnosticCpipError):
    reference = "invalid-pyproject-build-system-requires"

    def __init__(
        self,
        *,
        package: str,
        requirement: str,
        error: str,
    ) -> None:
        super().__init__(
            message=f"Getting requirements to build wheel for {package} failed.",
            context=(
                f"The value of `build-system.requires` for {package} contains an invalid "
                f"requirement: {requirement!r} ({error})"
            ),
            hint_stmt="This package has an invalid `build-system.requires` value. It does not comply with PEP 518.",
        )


class MetadataProvider(Protocol):
    """Prepared distribution view required by requirement metadata consumers."""

    @property
    def metadata(self) -> email.message.Message: ...

    @property
    def version(self) -> Version: ...


class VcsInfo:
    __slots__ = ("vcs",)

    def __init__(self, vcs: str) -> None:
        self.vcs = vcs


class DownloadInfo:
    __slots__ = ("url", "archive_info", "dir_info", "vcs_info")

    def __init__(
        self,
        url: str,
        archive_info: ArchiveInfo | None = None,
        dir_info: DirInfo | None = None,
        vcs_info: VcsInfo | None = None,
    ) -> None:
        self.url = url
        self.archive_info = archive_info
        self.dir_info = dir_info
        self.vcs_info = vcs_info


class NoOpBuildEnvironment_internal:
    python_executable = sys.executable

    def __enter__(self) -> NoOpBuildEnvironment_internal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def install_requirements(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def check_requirements(
        self, requirements: Iterable[str]
    ) -> tuple[set[str], set[str]]:
        del requirements
        return set(), set()


class InstallRequirement:
    __slots__ = (
        "req",
        "comes_from",
        "link",
        "marker_internal",
        "editable",
        "isolated",
        "hash_options",
        "constraint",
        "config_settings",
        "user_supplied",
        "permit_editable_wheels",
        "original_link",
        "satisfied_by",
        "extras_override",
        "source_dir",
        "local_file_path",
        "download_info",
        "is_wheel_from_cache",
        "cached_wheel_source_link",
        "metadata_internal",
        "distribution_internal",
        "archive_source_internal",
        "needs_more_preparation",
        "build_env",
        "pyproject_requires",
        "requirements_to_check",
        "metadata_directory",
        "pyproject_data",
        "pep517_backend",
        "should_reinstall",
        "install_succeeded",
    )

    def __init__(
        self,
        req: ParsedRequirement | None,
        comes_from: InstallRequirement | str | None = None,
        link: Link | None = None,
        marker_internal: str | None = None,
        editable: bool = False,
        isolated: bool = False,
        hash_options: dict[str, list[str]] | None = None,
        constraint: bool = False,
        config_settings: dict[str, object] | None = None,
        user_supplied: bool = False,
        permit_editable_wheels: bool = False,
        original_link: Link | None = None,
        satisfied_by: MetadataProvider | None = None,
        extras_override: set[str] | None = None,
        source_dir: str | None = None,
        local_file_path: str | None = None,
        download_info: Any = None,
        is_wheel_from_cache: bool = False,
        cached_wheel_source_link: Link | None = None,
        metadata_internal: email.message.Message | None = None,
        distribution_internal: MetadataProvider | None = None,
        archive_source_internal: Path | None = None,
        needs_more_preparation: bool = False,
        build_env: Any = None,
        pyproject_requires: list[str] | None = None,
        requirements_to_check: list[str] | None = None,
        metadata_directory: str | None = None,
        pyproject_data: dict[str, object] | None = None,
        pep517_backend: ConfiguredBuildBackend | None = None,
        should_reinstall: bool = False,
        install_succeeded: bool | None = None,
    ) -> None:
        self.req = req
        self.comes_from = comes_from
        self.link = link
        self.marker_internal = marker_internal
        self.editable = editable
        self.isolated = isolated
        self.hash_options = hash_options if hash_options is not None else {}
        self.constraint = constraint
        self.config_settings = config_settings
        self.user_supplied = user_supplied
        self.permit_editable_wheels = permit_editable_wheels
        self.original_link = original_link
        self.satisfied_by = satisfied_by
        self.extras_override = extras_override
        self.source_dir = source_dir
        self.local_file_path = local_file_path
        self.download_info = download_info
        self.is_wheel_from_cache = is_wheel_from_cache
        self.cached_wheel_source_link = cached_wheel_source_link
        self.metadata_internal = metadata_internal
        self.distribution_internal = distribution_internal
        self.archive_source_internal = archive_source_internal
        self.needs_more_preparation = needs_more_preparation
        self.build_env = (
            build_env if build_env is not None else NoOpBuildEnvironment_internal()
        )
        self.pyproject_requires = pyproject_requires
        self.requirements_to_check = (
            requirements_to_check if requirements_to_check is not None else []
        )
        self.metadata_directory = metadata_directory
        self.pyproject_data = pyproject_data
        self.pep517_backend = pep517_backend
        self.should_reinstall = should_reinstall
        self.install_succeeded = install_succeeded
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.link is None and self.req is not None and self.req.url is not None:
            self.link = Link(self.req.url)
        if self.local_file_path is None and self.link is not None and self.link.is_file:
            self.local_file_path = self.link.file_path

    @property
    def extras(self) -> set[str]:
        if self.extras_override is not None:
            return set(self.extras_override)
        return set(self.req.extras) if self.req is not None else set()

    @property
    def name(self) -> str | None:
        return self.req.name if self.req is not None else None

    @property
    def specifier(self) -> SpecifierSet:
        if self.req is None:
            raise ValueError("requirement has no parsed requirement")
        return self.req.specifier

    @property
    def markers(self) -> str | None:
        if self.marker_internal is not None:
            return self.marker_internal
        return self.req.marker if self.req is not None else None

    @property
    def is_wheel(self) -> bool:
        return self.link is not None and self.link.filename.endswith(".whl")

    @property
    def supports_pyproject_editable(self) -> bool:
        """Whether this requirement can use the editable build backend."""
        return self.pep517_backend is not None

    @property
    def is_direct(self) -> bool:
        """Whether this requirement was specified with a direct URL."""
        return self.req is not None and self.req.url is not None

    @property
    def is_pinned(self) -> bool:
        """Whether this requirement is constrained to one exact version."""
        if self.req is None:
            raise ValueError("requirement has no parsed requirement")
        specifiers = self.req.specifier.specifiers
        return len(specifiers) == 1 and specifiers[0].operator in {"==", "==="}

    @property
    def has_hash_options(self) -> bool:
        """Whether command-line hash options were supplied."""
        return bool(self.hash_options)

    def hashes(self, trust_internet: bool = True) -> Hashes:
        values = {
            algorithm: list(digests) for algorithm, digests in self.hash_options.items()
        }
        link = self.link if trust_internet else None
        if link is not None and link.hash and link.hash_name:
            values.setdefault(link.hash_name, []).append(link.hash)
        return Hashes(values)

    def is_satisfied_by(self, candidate: object) -> bool:
        if self.req is None:
            return False
        expected = self.req.name
        if expected.startswith("file://") and self.link is not None:
            expected = self.link.filename.split("-", 1)[0]
        return getattr(candidate, "name", None) == expected

    def match_markers(self, extras_requested: Iterable[str] = ()) -> bool:
        return marker_applies(self.markers, extras=extras_requested)

    def ensure_build_location(
        self,
        parent_dir: str,
        *,
        autodelete: bool,
        parallel_builds: bool,
    ) -> str:
        import tempfile

        del autodelete, parallel_builds
        root = os.path.realpath(os.path.dirname(parent_dir))
        return tempfile.mkdtemp("-build", "cpip-", dir=root)

    def ensure_has_source_dir(
        self,
        parent_dir: str,
        autodelete: bool = False,
        parallel_builds: bool = False,
    ) -> None:
        """Allocate the source directory used while preparing this requirement."""
        if self.source_dir is None:
            self.source_dir = self.ensure_build_location(
                parent_dir,
                autodelete=autodelete,
                parallel_builds=parallel_builds,
            )

    def needs_unpacked_archive(self, archive_source: Path) -> None:
        if self.archive_source_internal is not None:
            raise AssertionError("archive source already set")
        self.archive_source_internal = archive_source

    def ensure_pristine_source_checkout(self) -> None:
        """Populate or validate the source directory before preparation."""
        if self.source_dir is None:
            raise InstallationError(f"No source directory for {self}")
        if self.archive_source_internal is not None:
            return
        if os.path.isfile(
            os.path.join(self.source_dir, "pyproject.toml")
        ) or os.path.isfile(os.path.join(self.source_dir, "setup.py")):
            raise InstallationError(
                f"cpip can't proceed with requirement {self!r} because its source "
                f"directory already contains an installable project"
            )

    def set_dist(self, distribution: MetadataProvider) -> None:
        self.distribution_internal = distribution

    def get_dist(self) -> MetadataProvider:
        if self.distribution_internal is None:
            raise AssertionError(f"InstallRequirement {self} has no distribution")
        return self.distribution_internal

    @property
    def metadata(self) -> email.message.Message:
        if self.metadata_internal is None:
            distribution = self.get_dist()
            self.metadata_internal = distribution.metadata
        return self.metadata_internal

    def assert_source_matches_version(self) -> None:
        if self.req is None or self.metadata_internal is None:
            return
        requested = str(self.req.specifier)
        actual = self.metadata_internal.get("version")
        if requested and actual and requested != f"=={actual}":
            logger.warning(
                "Requested %s%s, but installing version %s",
                self.req.name,
                requested,
                actual,
            )

    def warn_on_mismatching_name(self) -> None:
        """Normalize the requirement name to generated distribution metadata."""
        if self.req is None or self.metadata_internal is None:
            return
        metadata_name = self.metadata_internal.get("name")
        if not metadata_name:
            return
        if canonicalize_name(self.req.name) == canonicalize_name(metadata_name):
            return

        logger.warning(
            "Generating metadata for package %s produced metadata for project "
            "name %s. Fix your #egg=%s fragments.",
            self.name,
            canonicalize_name(metadata_name),
            self.name,
        )
        # Keep the source URL and the user's requirement constraints when the
        # project name is discovered from metadata.  Re-parsing only the name
        # turns a directory requirement into an unconstrained requirement and
        # loses the link used to associate it with its source candidate.
        self.req = ParsedRequirement(
            name=canonicalize_name(metadata_name),
            specifier=self.req.specifier,
            extras=self.req.extras,
            url=(
                self.req.url
                or (
                    self.link.url
                    if self.link is not None
                    and (
                        self.link.is_existing_dir
                        or self.link.is_file
                        or self.link.is_vcs
                    )
                    else None
                )
            ),
            marker=self.req.marker,
            raw=self.req.raw,
        )

    def load_pyproject_toml(self) -> dict[str, object]:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
            from cpip._vendor import tomli as tomllib

        if self.source_dir is None:
            raise InstallationError("Install requirement has no source directory")
        source_dir = os.fspath(self.source_dir)
        pyproject = os.path.join(source_dir, "pyproject.toml")
        setup_py = os.path.join(source_dir, "setup.py")
        if os.path.isfile(pyproject):
            with open(pyproject, encoding="utf-8") as file:
                data = tomllib.loads(file.read())
        elif os.path.isfile(setup_py):
            data = {
                "build-system": {
                    # setuptools 82 removed pkg_resources, which is still
                    # imported by many legacy setup.py files.
                    "requires": ["setuptools>=40.8.0,<82"],
                    "build-backend": "setuptools.build_meta:__legacy__",
                }
            }
        else:
            raise InstallationError(
                f"{self} does not appear to be a Python project: neither "
                "'setup.py' nor 'pyproject.toml' found."
            )
        self.pyproject_data = data
        build_system = data.get("build-system")
        if not isinstance(build_system, dict):
            return data
        requires = build_system.get("requires")
        if not isinstance(requires, list):
            return data
        self.pyproject_requires = [str(item) for item in requires]
        parsed_requires: list[ParsedRequirement] = []
        package = str(self)
        for item in requires:
            if not isinstance(item, str):
                raise InvalidPyProjectBuildRequires(
                    package=package,
                    requirement=repr(item),
                    error="build requirements must be strings",
                )
            if looks_like_path(item) or item.startswith(
                ("git+", "hg+", "svn+", "bzr+")
            ):
                raise InvalidPyProjectBuildRequires(
                    package=package,
                    requirement=item,
                    error="direct references and local paths are not allowed",
                )
            try:
                parsed = parse_requirement(item)
            except ValueError as exc:
                raise InvalidPyProjectBuildRequires(
                    package=package,
                    requirement=item,
                    error=str(exc),
                ) from exc
            parsed_requires.append(parsed)
            if parsed.url is not None:
                raise InvalidPyProjectBuildRequires(
                    package=package,
                    requirement=item,
                    error="direct references are not allowed",
                )
        backend = build_system.get("build-backend", "setuptools.build_meta")
        setup_uses_pkg_resources = False
        if os.path.isfile(setup_py):
            with open(setup_py, encoding="utf-8") as file:
                setup_uses_pkg_resources = "pkg_resources" in file.read()
        if (
            isinstance(backend, str)
            and backend.startswith("setuptools.build_meta")
            and os.path.isfile(setup_py)
            and setup_uses_pkg_resources
            and not any(
                canonicalize_name(parsed.name) == "setuptools"
                and not parsed.specifier.contains(Version("81"), allow_prereleases=True)
                for parsed in parsed_requires
            )
        ):
            # setuptools 82 removed pkg_resources, which is still imported by
            # many setup.py files. Keep compatible projects on the last line
            # that provides that API while preserving explicit >=82 requests.
            self.pyproject_requires.append("setuptools<82")
        self.requirements_to_check = []
        return data

    def configure_backend(self, python_executable: str | Path) -> None:
        if self.source_dir is None:
            raise InstallationError("Install requirement has no source directory")
        data = self.pyproject_data or self.load_pyproject_toml()
        build_system = data.get("build-system")
        backend = None
        backend_path: tuple[str, ...] = ()
        if isinstance(build_system, dict):
            raw_backend = build_system.get("build-backend")
            if isinstance(raw_backend, str):
                backend = raw_backend
            raw_backend_path = build_system.get("backend-path", [])
            if isinstance(raw_backend_path, list):
                backend_path = tuple(
                    item for item in raw_backend_path if isinstance(item, str)
                )
        if backend is None:
            backend = "setuptools.build_meta:__legacy__"
        self.pep517_backend = ConfiguredBuildBackend(
            source_dir=Path(self.source_dir),
            backend=backend,
            backend_path=backend_path,
            python_executable=Path(python_executable),
        )

    def editable_sanity_check(self) -> None:
        """Validate that editable preparation has a backend to call."""
        if self.editable and self.pep517_backend is None:
            raise InstallationError(
                f"Project {self} has no configured build backend for editable installation"
            )

    def prepare_metadata(self) -> None:
        """Ask the configured backend to generate the project metadata."""
        import tempfile

        if self.source_dir is None or self.pep517_backend is None:
            raise InstallationError(f"Cannot prepare metadata for {self}")
        metadata_root = Path(tempfile.mkdtemp(prefix="cpip-modern-metadata-"))
        hook = (
            "prepare_metadata_for_build_editable"
            if self.editable and self.permit_editable_wheels
            else "prepare_metadata_for_build_wheel"
        )
        metadata_name = self.pep517_backend.call_hook(
            hook,
            os.fspath(metadata_root),
            self.config_settings,
        )
        self.metadata_directory = os.fspath(metadata_root / str(metadata_name))
        self.warn_on_mismatching_name()
        self.assert_source_matches_version()

    def __str__(self) -> str:
        return (
            str(self.req)
            if self.req is not None
            else str(self.link.url if self.link else "")
        )

    def __repr__(self) -> str:
        return f"<InstallRequirement object: {self} editable={self.editable}>"

    def format_debug(self) -> str:
        names = {
            name
            for cls in type(self).__mro__
            for name in getattr(cls, "__slots__", ())
            if isinstance(name, str) and not name.startswith("__")
        }
        attributes = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in sorted(names)
        )
        return f"<{self.__class__.__name__} object: {{{attributes}}}>"

    def from_path(self) -> str | None:
        """Format the requirement and its source provenance."""
        if self.req is None:
            return None
        result = str(self.req)
        if self.comes_from:
            source = (
                self.comes_from
                if isinstance(self.comes_from, str)
                else self.comes_from.from_path()
            )
            if source:
                result += "->" + source
        return result

    @property
    def unpacked_source_directory(self) -> Path:
        if self.source_dir is None:
            raise ValueError(f"No source directory for {self}")
        subdirectory = self.link.subdirectory_fragment if self.link else None
        return Path(self.source_dir) / (subdirectory or "")

    @property
    def setup_py_path(self) -> Path:
        return self.unpacked_source_directory / "setup.py"

    @property
    def pyproject_toml_path(self) -> Path:
        return self.unpacked_source_directory / "pyproject.toml"


def __getattr__(name: str):
    """Load parser entry points only when a caller requests them."""
    if name in {"install_req_from_editable", "install_req_from_line", "parse_editable"}:
        from cpip.resolution.requirements import parsing

        return getattr(parsing, name)
    raise AttributeError(name)
