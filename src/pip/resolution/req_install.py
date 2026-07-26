from __future__ import annotations

import email.message
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    from pip._vendor import tomli as tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from pip.core.errors import (
    DiagnosticPipError,
    InstallationError,
    InvalidWheelFilename,
)
from pip.core.direct_url import ArchiveInfo, DirectUrl, DirInfo
from pip.core.hashes import Hashes
from pip.index.links import Link
from pip.core.packaging import (
    Requirement as ParsedRequirement,
    SpecifierSet,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)

logger = logging.getLogger(__name__)


class InvalidPyProjectBuildRequires(DiagnosticPipError):
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


@dataclass(frozen=True)
class VcsInfo:
    vcs: str


@dataclass(frozen=True)
class DownloadInfo:
    url: str
    archive_info: ArchiveInfo | None = None
    dir_info: DirInfo | None = None
    vcs_info: VcsInfo | None = None


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


@dataclass
class InstallRequirement:
    req: ParsedRequirement | None
    comes_from: InstallRequirement | str | None = None
    link: Link | None = None
    marker_internal: str | None = None
    editable: bool = False
    isolated: bool = False
    hash_options: dict[str, list[str]] = field(default_factory=dict)
    constraint: bool = False
    config_settings: dict[str, object] | None = None
    user_supplied: bool = False
    permit_editable_wheels: bool = False
    original_link: Link | None = None
    satisfied_by: MetadataProvider | None = None
    extras_override: set[str] | None = None
    source_dir: str | None = None
    local_file_path: str | None = None
    download_info: Any = None
    is_wheel_from_cache: bool = False
    cached_wheel_source_link: Link | None = None
    metadata_internal: email.message.Message | None = None
    distribution_internal: MetadataProvider | None = None
    archive_source_internal: Path | None = None
    needs_more_preparation: bool = False
    build_env: Any = field(default_factory=NoOpBuildEnvironment_internal)
    pyproject_requires: list[str] | None = None
    requirements_to_check: list[str] = field(default_factory=list)
    metadata_directory: str | None = None
    pyproject_data: dict[str, object] | None = None
    pep517_backend: ConfiguredBuildBackend | None = None
    should_reinstall: bool = False
    install_succeeded: bool | None = None

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
        del autodelete, parallel_builds
        root = os.path.realpath(os.path.dirname(parent_dir))
        return tempfile.mkdtemp("-build", "pip-", dir=root)

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
                f"pip can't proceed with requirement {self!r} because its source "
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
        if self.source_dir is None:
            raise InstallationError("Install requirement has no source directory")
        pyproject = Path(self.source_dir) / "pyproject.toml"
        setup_py = Path(self.source_dir) / "setup.py"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        elif setup_py.is_file():
            data = {
                "build-system": {
                    "requires": ["setuptools>=40.8.0"],
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
            if parsed.url is not None:
                raise InvalidPyProjectBuildRequires(
                    package=package,
                    requirement=item,
                    error="direct references are not allowed",
                )
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
        if self.source_dir is None or self.pep517_backend is None:
            raise InstallationError(f"Cannot prepare metadata for {self}")
        metadata_root = Path(tempfile.mkdtemp(prefix="pip-modern-metadata-"))
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
        attributes = ", ".join(
            f"{name}={value!r}" for name, value in sorted(vars(self).items())
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


class ConfiguredBuildBackend:
    def __init__(
        self,
        *,
        source_dir: Path,
        backend: str,
        backend_path: tuple[str, ...],
        python_executable: Path,
    ) -> None:
        self.source_dir = source_dir
        self.backend = backend
        self.backend_path = backend_path
        self.python_executable = python_executable

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def build_wheel(
        self,
        wheel_directory: str,
        config_settings: dict[str, object] | None = None,
        metadata_directory: str | None = None,
    ) -> object:
        return self.call_hook(
            "build_wheel",
            wheel_directory,
            config_settings,
            metadata_directory,
        )

    def call_hook(self, hook: str, *args: object) -> object:
        payload = {
            "backend": self.backend,
            "hook": hook,
            "args": args,
        }
        env = os.environ.copy()
        pythonpath = [
            os.fspath((self.source_dir / path).resolve()) for path in self.backend_path
        ]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath.extend(existing.split(os.pathsep))
        if pythonpath:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        process = subprocess.run(
            [os.fspath(self.python_executable), "-c", BACKEND_CALLER],
            cwd=self.source_dir,
            env=env,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            details = process.stderr.strip() or process.stdout.strip()
            raise InstallationError(details or f"Build backend hook failed: {hook}")
        output = process.stdout.strip().splitlines()
        if not output:
            return None
        return json.loads(output[-1])["result"]


BACKEND_CALLER = r"""
import importlib
import json
import sys

payload = json.loads(sys.stdin.read())
backend = payload["backend"]
module_name, _, object_path = backend.partition(":")
target = importlib.import_module(module_name)
if object_path:
    for part in object_path.split("."):
        target = getattr(target, part)
hook = getattr(target, payload["hook"])
result = hook(*payload["args"])
print(json.dumps({"result": result}))
"""


def install_req_from_line(
    line: str,
    *,
    comes_from: InstallRequirement | str | None = None,
    constraint: bool = False,
    isolated: bool = False,
    user_supplied: bool = False,
    hash_options: dict[str, list[str]] | None = None,
    config_settings: dict[str, object] | None = None,
    permit_editable_wheels: bool = False,
) -> InstallRequirement:
    text = line.strip()
    path_extras: frozenset[str] = frozenset()
    path_text = text
    if "[" in text and text.endswith("]") and " @ " not in text:
        maybe_path, extras_text = text[:-1].split("[", 1)
        extras = frozenset(
            item.strip() for item in extras_text.split(",") if item.strip()
        )
        if extras and (
            looks_like_path(maybe_path)
            or os.path.isdir(maybe_path)
            or os.path.exists(maybe_path)
        ):
            path_text = maybe_path
            path_extras = extras
    if "@" in text and "://" in text:
        try:
            parsed = parse_requirement(text)
        except ValueError:
            pass
        else:
            marker = parsed.marker
            if marker is not None:
                parsed = ParsedRequirement(
                    name=parsed.name,
                    specifier=parsed.specifier,
                    extras=parsed.extras,
                    url=parsed.url,
                    marker=None,
                    raw=parsed.raw,
                )
            if parsed.url is not None:
                return InstallRequirement(
                    parsed,
                    comes_from=comes_from,
                    link=Link(parsed.url),
                    marker_internal=marker,
                    isolated=isolated,
                    user_supplied=user_supplied,
                    hash_options=hash_options or {},
                    constraint=constraint,
                    config_settings=config_settings,
                    permit_editable_wheels=permit_editable_wheels,
                )
    if "://" in text:
        marker_index = text.find("; ")
        if marker_index == -1:
            url, marker = text, None
        else:
            prefix = text[:marker_index]
            parsed_url = urllib.parse.urlparse(prefix)
            if not parsed_url.scheme:
                url, marker = text, None
            else:
                url, marker = prefix, text[marker_index + 2 :].strip()
        parsed = parse_requirement(url)
        return InstallRequirement(
            parsed,
            comes_from=comes_from,
            link=Link(url),
            marker_internal=marker,
            isolated=isolated,
            user_supplied=user_supplied,
            hash_options=hash_options or {},
            constraint=constraint,
            config_settings=config_settings,
            permit_editable_wheels=permit_editable_wheels,
        )
    if looks_like_path(path_text):
        url = get_url_from_path(path_text, path_text)
        if url is None:
            raise InstallationError(
                f"Invalid requirement: {text!r}. It looks like a path."
            )
        if Path(path_text).is_file() and path_text.endswith(".txt"):
            raise InstallationError(
                f"Invalid requirement: {text!r}. It looks like a path. The path does exist. "
                "The argument appears to be a requirements file. If that is the case, use the '-r' flag to install"
            )
        parsed = parse_requirement(url)
        if Path(path_text).suffix.lower() == ".whl":
            wheel_parts = Path(path_text).name[:-4].split("-")
            if len(wheel_parts) >= 2:
                wheel_requirement = parse_requirement(
                    f"{wheel_parts[0]}=={wheel_parts[1]}"
                )
                parsed = ParsedRequirement(
                    name=wheel_requirement.name,
                    specifier=wheel_requirement.specifier,
                    extras=path_extras,
                    url=url,
                    marker=parsed.marker,
                    raw=text,
                )
        if path_extras:
            parsed = ParsedRequirement(
                name=parsed.name,
                specifier=parsed.specifier,
                extras=path_extras,
                url=parsed.url,
                marker=parsed.marker,
                raw=text,
            )
        return InstallRequirement(
            parsed,
            comes_from=comes_from,
            link=Link(url),
            isolated=isolated,
            user_supplied=user_supplied,
            hash_options=hash_options or {},
            constraint=constraint,
            config_settings=config_settings,
            permit_editable_wheels=permit_editable_wheels,
        )
    if text.endswith(".whl") and "@" not in text and "://" not in text:
        parts = text[:-4].split("-")
        if len(parts) < 5:
            raise InvalidWheelFilename(text)
        parsed = parse_requirement(f"{parts[0]}=={parts[1]}")
        return InstallRequirement(
            parsed,
            comes_from=comes_from,
            link=Link(text),
            isolated=isolated,
            user_supplied=user_supplied,
            hash_options=hash_options or {},
            constraint=constraint,
            config_settings=config_settings,
            permit_editable_wheels=permit_editable_wheels,
        )
    try:
        parsed = parse_requirement(text)
    except ValueError as exc:
        message = f"Invalid requirement: {text!r}"
        if "=" in text and "==" not in text:
            message += ". = is not a valid operator. Did you mean == ?"
        raise InstallationError(message) from exc
    if parsed.marker:
        quote: str | None = None
        for char in parsed.marker:
            if char in {"'", '"'}:
                quote = None if quote == char else char
            elif char == ";" and quote is None:
                raise InstallationError(f"Invalid requirement: {text!r}")
    marker = parsed.marker
    if marker is not None:
        parsed = ParsedRequirement(
            name=parsed.name,
            specifier=parsed.specifier,
            extras=parsed.extras,
            url=parsed.url,
            marker=None,
            raw=parsed.raw,
        )
    return InstallRequirement(
        parsed,
        comes_from=comes_from,
        link=(
            Link(parsed.url) if parsed.url else Link(text) if "://" in text else None
        ),
        marker_internal=marker,
        isolated=isolated,
        user_supplied=user_supplied,
        hash_options=hash_options or {},
        constraint=constraint,
        config_settings=config_settings,
        permit_editable_wheels=permit_editable_wheels,
    )


def install_req_from_editable(
    value: str,
    *,
    comes_from: InstallRequirement | str | None = None,
    isolated: bool = False,
    user_supplied: bool = False,
    constraint: bool = False,
    permit_editable_wheels: bool = False,
    hash_options: dict[str, list[str]] | None = None,
    config_settings: dict[str, object] | None = None,
) -> InstallRequirement:
    name, url, extras = parse_editable(value)
    marker: str | None = None
    if name is not None and " ; " in name:
        name, marker = name.split(" ; ", 1)
    if name is None:
        parsed = parse_requirement(
            f"editable-placeholder[{','.join(sorted(extras))}] @ {url}"
            if extras
            else f"editable-placeholder @ {url}"
        )
    else:
        parsed = parse_requirement(
            f"{name}[{','.join(sorted(extras))}] @ {url}"
            if extras
            else f"{name} @ {url}"
        )
    return InstallRequirement(
        parsed,
        comes_from=comes_from,
        link=Link(url),
        marker_internal=marker or parsed.marker,
        editable=True,
        isolated=isolated,
        user_supplied=user_supplied,
        constraint=constraint,
        hash_options=hash_options or {},
        permit_editable_wheels=permit_editable_wheels,
        config_settings=config_settings,
    )


def parse_editable(value: str) -> tuple[str | None, str, set[str]]:
    stripped = value.strip()
    if " @ " in stripped:
        parsed = parse_requirement(stripped)
        requirement_text = parsed.name
        if parsed.marker:
            requirement_text += f" ; {parsed.marker}"
        return requirement_text, parsed.url or "", set(parsed.extras)
    if "#egg=" in stripped:
        base, fragment = stripped.split("#", 1)
        fragment_values = urllib.parse.parse_qs(fragment, keep_blank_values=True)
        egg = fragment_values.get("egg", [""])[0]
        remaining_fragment = urllib.parse.urlencode(
            [
                (key, value)
                for key, values in fragment_values.items()
                if key != "egg"
                for value in values
            ]
        )
        url = base + (f"#{remaining_fragment}" if remaining_fragment else "")
        url = normalize_file_url_reference(stripped) or stripped
        if "[" in egg and egg.endswith("]"):
            name, extras_text = egg[:-1].split("[", 1)
            return (
                name,
                url,
                {item.strip() for item in extras_text.split(",") if item.strip()},
            )
        return egg, url, set()
    extras: set[str] = set()
    path_part = stripped
    if "[" in stripped and stripped.endswith("]"):
        path_part, extras_text = stripped[:-1].split("[", 1)
        extras = {item.strip() for item in extras_text.split(",") if item.strip()}
    if (
        looks_like_path(path_part)
        or os.path.isdir(path_part)
        or os.path.exists(path_part)
    ):
        normalized = normalize_file_url_reference(path_part)
        if normalized is not None:
            return None, normalized, extras
        return (
            None,
            Path(os.path.abspath(path_part)).resolve(strict=False).as_uri(),
            extras,
        )
    return None, stripped, extras


def looks_like_path(value: str) -> bool:
    return (
        value.startswith((".", "/", "~"))
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
        or "://" in value
        or " @ " in value
    )


def get_url_from_path(path: str, name: str) -> str | None:
    parsed = urllib.parse.urlparse(path)
    if parsed.scheme == "file":
        local_path = path_from_file_url(parsed)
        if local_path.is_file():
            return file_url_with_fragment(local_path, parsed.fragment)
        if local_path.is_dir():
            setup_py = local_path / "setup.py"
            pyproject = local_path / "pyproject.toml"
            if not setup_py.is_file() and not pyproject.is_file():
                raise InstallationError(
                    "Neither 'setup.py' nor 'pyproject.toml' found."
                )
            return file_url_with_fragment(local_path, parsed.fragment)
        return None
    if " @ " in path or "@git+" in path or "://" in path and not Path(path).exists():
        return None
    if os.path.isfile(path):
        return Path(path).resolve(strict=False).as_uri()
    if os.path.isdir(path):
        setup_py = os.path.join(path, "setup.py")
        pyproject = os.path.join(path, "pyproject.toml")
        if not os.path.isfile(setup_py) and not os.path.isfile(pyproject):
            raise InstallationError("Neither 'setup.py' nor 'pyproject.toml' found.")
        return Path(path).resolve(strict=False).as_uri()
    return None


def normalize_file_url_reference(value: str) -> str | None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "file":
        return None
    return file_url_with_fragment(path_from_file_url(parsed), parsed.fragment)


def path_from_file_url(parsed: urllib.parse.ParseResult) -> Path:
    path = Path(urllib.request.url2pathname(parsed.path))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def file_url_with_fragment(path: Path, fragment: str) -> str:
    url = path.resolve(strict=False).as_uri()
    return f"{url}#{fragment}" if fragment else url


def file_hashes(path: str | Path) -> dict[str, str]:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        buffer = bytearray(max(1, min(size, 1024 * 1024)))
        view = memoryview(buffer)
        while read := stream.readinto(buffer):
            digest.update(view[:read])
    return {"sha256": digest.hexdigest()}
