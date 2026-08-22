"""PEP 751 pylock requirement-file support."""

from __future__ import annotations

from typing import Any

import os
import posixpath
import re
import urllib.parse


from cpip.core.errors import InstallationError
from cpip.core.format_control import FormatControl
from cpip.core.packaging import SpecifierSet
from cpip.core.versions import Version
from cpip.core.urls import path_to_url
from cpip.core.utils import CURRENT_PYTHON_VERSION_FULL
from cpip.core.wheel import parse_wheel_filename
from cpip.resolution.files.models import ParsedRequirement


def _toml_module() -> Any:
    """The TOML parser, imported on first use: only a pylock input needs it."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        from cpip._vendor import tomli as tomllib
    return tomllib


def __getattr__(name: str) -> Any:
    if name == "tomllib":
        return _toml_module()
    raise AttributeError(name)


TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.resolution.files.contracts import RequirementSource


HTTP_SCHEMES = frozenset(("http", "https"))

ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".zip")


def is_pylock_reference(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)

    path = parsed.path or value

    return posixpath.basename(path).startswith("pylock") and path.endswith(".toml")


def pylock_location(reference: str, path: str | None) -> str:
    if path is None:
        raise InstallationError("pylock package is missing its path")

    parsed = urllib.parse.urlparse(reference)

    if parsed.scheme in HTTP_SCHEMES:
        return urllib.parse.urljoin(reference, path)

    return path_to_url(os.path.join(os.path.dirname(os.path.realpath(reference)), path))


def parse_pylock(
    reference: str,
    content: str,
    *,
    provider: RequirementSource | None,
) -> list[ParsedRequirement]:
    tomllib = _toml_module()

    try:
        lock = tomllib.loads(content)

        if not isinstance(lock, dict) or lock.get("lock-version") != "1.0":
            raise TypeError("unsupported or missing lock-version")

        packages = lock["packages"]

        if not isinstance(packages, list):
            raise TypeError("packages must be an array")

    except Exception as exc:
        raise InstallationError(f"Invalid pylock file {reference!r}: {exc}") from exc

    lock_requires_python = lock.get("requires-python")

    if lock_requires_python is not None and not SpecifierSet(
        str(lock_requires_python),
    ).contains(Version(CURRENT_PYTHON_VERSION_FULL)):
        raise InstallationError(
            f"Cannot select requirements from pylock file {reference!r}: "
            "no distribution supports this Python version",
        )

    results: list[ParsedRequirement] = []

    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise InstallationError(
                f"Cannot select requirements from pylock file {reference!r}",
            )

        package_name = package["name"]

        requires_python = package.get("requires-python")

        if requires_python is not None and not SpecifierSet(
            str(requires_python),
        ).contains(Version(CURRENT_PYTHON_VERSION_FULL)):
            raise InstallationError(
                f"Cannot select requirements from pylock file {reference!r}: "
                "no distribution supports this Python version",
            )

        try:
            distribution, kind = _select_distribution(package, provider)

        except InstallationError as exc:
            raise InstallationError(
                f"Invalid pylock file {reference!r}: {exc}",
            ) from exc

        hashes = _distribution_hashes(distribution, package_name)

        link: str

        direct = False

        if kind == "directory":
            link = pylock_location(
                reference,
                _distribution_string(distribution, "path"),
            )

            requirement = link

            direct = True

        elif kind == "archive":
            link = pylock_location(
                reference,
                _distribution_string(distribution, "path")
                or _distribution_string(distribution, "url"),
            )

            requirement = f"{package_name} @ {link}"

            direct = True

        elif kind == "vcs":
            link = (
                _distribution_string(distribution, "url")
                or _distribution_string(distribution, "path")
                or ""
            )

            requirement = f"{package_name} @ {distribution['type']}+{link}@{distribution['commit-id']}"

            direct = True

        elif kind == "wheel":
            if provider is not None and "binary" not in (
                provider.format_control or FormatControl()
            ).get_allowed_formats(package_name):
                if not package.get("sdist"):
                    raise InstallationError(
                        f"binaries are not permitted for package {package_name!r} and "
                        f"there is no source distribution for it in {reference!r}",
                    )

                distribution = package["sdist"]

                link = pylock_location(
                    reference,
                    _distribution_string(distribution, "path")
                    or _distribution_string(distribution, "url"),
                )

                hashes = _distribution_hashes(distribution, package_name)

                version = _distribution_string(package, "version") or _sdist_version(
                    _distribution_string(distribution, "name")
                    or posixpath.basename(link),
                    package_name,
                )

                requirement = f"{package_name}=={version}"

            else:
                link = pylock_location(
                    reference,
                    _distribution_string(distribution, "path")
                    or _distribution_string(distribution, "url"),
                )

                parsed = parse_wheel_filename(
                    _distribution_string(distribution, "name")
                    or posixpath.basename(link),
                )

                if parsed is None:
                    raise InstallationError(
                        f"Invalid wheel filename for {package_name!r}",
                    )

                _, version = parsed

                requirement = f"{package_name}=={version}"

        else:
            if provider is not None and "source" not in (
                provider.format_control or FormatControl()
            ).get_allowed_formats(package_name):
                raise InstallationError(
                    f"source distributions are not permitted for package {package_name!r} and "
                    f"there is no compatible wheel for it in {reference!r}",
                )

            link = pylock_location(
                reference,
                _distribution_string(distribution, "path")
                or _distribution_string(distribution, "url"),
            )

            version = _distribution_string(package, "version") or _sdist_version(
                _distribution_string(distribution, "name") or posixpath.basename(link),
                package_name,
            )

            requirement = f"{package_name}=={version}"

        results.append(
            ParsedRequirement(
                requirement=requirement,
                comes_from=reference,
                is_editable=kind == "directory" and bool(distribution.get("editable")),
                options={"hashes": hashes} if hashes else None,
                locked_link=link,
                locked_hashes=hashes,
                locked_direct=direct,
                locked_name=package_name,
            ),
        )

    return results


def _select_distribution(
    package: dict[str, object],
    provider: RequirementSource | None,
) -> tuple[dict[str, object], str]:
    """Select the first usable PEP 751 distribution without packaging.pylock."""

    for key, kind in (
        ("directory", "directory"),
        ("archive", "archive"),
        ("vcs", "vcs"),
    ):
        distribution = package.get(key)

        if isinstance(distribution, dict):
            return distribution, kind  # ty:ignore[invalid-return-type]

    wheels = package.get("wheels")

    if isinstance(wheels, list):
        for distribution in wheels:
            if isinstance(distribution, dict):
                return distribution, "wheel"  # ty:ignore[invalid-return-type]

    sdist = package.get("sdist")

    if isinstance(sdist, dict):
        return sdist, "sdist"  # ty:ignore[invalid-return-type]

    raise InstallationError("Cannot select a distribution from pylock package")


def _distribution_string(distribution: dict[str, object], key: str) -> str | None:
    value = distribution.get(key)

    if value is not None and not isinstance(value, str):
        raise InstallationError(f"Invalid string value for {key!r}")

    return value


def _distribution_hashes(
    distribution: dict[str, object],
    package_name: str,
) -> dict[str, list[str]]:
    raw_hashes = distribution.get("hashes", {})

    if not isinstance(raw_hashes, dict):
        raise InstallationError(f"Invalid hashes for {package_name!r}")

    if not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in raw_hashes.items()
    ):
        raise InstallationError(f"Invalid hashes for {package_name!r}")

    return {name: [value] for name, value in raw_hashes.items()}  # ty:ignore[invalid-return-type]


def _sdist_version(filename: str, package_name: str) -> str:
    stem = filename

    for suffix in ARCHIVE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]

            break

    prefix = package_name.replace("-", "_") + "-"

    if stem.startswith(prefix):
        return stem[len(prefix) :]

    match = re.search(r"-(\d[^-]*)$", stem)

    if match is None:
        raise InstallationError(
            f"Cannot determine version from source archive {filename!r}",
        )

    return match.group(1)
