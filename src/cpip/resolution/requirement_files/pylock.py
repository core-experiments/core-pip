"""PEP 751 pylock requirement-file support."""

from __future__ import annotations

import os
import posixpath
import urllib.parse
from typing import TYPE_CHECKING

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    from cpip._vendor import tomli as tomllib

from cpip.core.errors import InstallationError
from cpip.core.urls import path_to_url
from cpip.resolution.requirement_files.models import ParsedRequirement

if TYPE_CHECKING:
    from cpip.index.provider import CandidateProvider

HTTP_SCHEMES = frozenset(("http", "https"))


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
    provider: CandidateProvider | None,
) -> list[ParsedRequirement]:
    from packaging import pylock
    from packaging.utils import parse_sdist_filename, parse_wheel_filename

    from cpip.core.format_control import FormatControl

    try:
        lock = pylock.Pylock.from_dict(tomllib.loads(content))
    except Exception as exc:
        raise InstallationError(f"Invalid pylock file {reference!r}: {exc}") from exc
    try:
        selected = list(lock.select())
    except Exception as exc:
        raise InstallationError(
            f"Cannot select requirements from pylock file {reference!r}: {exc}"
        ) from exc
    results: list[ParsedRequirement] = []
    for package, distribution in selected:
        raw_hashes = getattr(distribution, "hashes", {})
        hashes = {name: [value] for name, value in raw_hashes.items()}
        link: str
        direct = False
        if isinstance(distribution, pylock.PackageDirectory):
            link = pylock_location(reference, distribution.path)
            requirement = link
            direct = True
        elif isinstance(distribution, pylock.PackageArchive):
            link = pylock_location(reference, distribution.path or distribution.url)
            requirement = f"{package.name} @ {link}"
            direct = True
        elif isinstance(distribution, pylock.PackageVcs):
            link = distribution.url or distribution.path or ""
            requirement = (
                f"{package.name} @ {distribution.type}+{link}@{distribution.commit_id}"
            )
            direct = True
        elif isinstance(distribution, pylock.PackageWheel):
            if provider is not None and "binary" not in (
                provider.format_control or FormatControl()
            ).get_allowed_formats(package.name):
                if package.sdist is None:
                    raise InstallationError(
                        f"binaries are not permitted for package {package.name!r} and "
                        f"there is no source distribution for it in {reference!r}"
                    )
                distribution = package.sdist
                link = pylock_location(reference, distribution.path or distribution.url)
                hashes = {name: [value] for name, value in distribution.hashes.items()}
            else:
                link = pylock_location(reference, distribution.path or distribution.url)
            _, version, _, _ = parse_wheel_filename(
                distribution.name or posixpath.basename(link)
            )
            requirement = f"{package.name}=={version}"
        else:
            if provider is not None and "source" not in (
                provider.format_control or FormatControl()
            ).get_allowed_formats(package.name):
                raise InstallationError(
                    f"source distributions are not permitted for package {package.name!r} and "
                    f"there is no compatible wheel for it in {reference!r}"
                )
            link = pylock_location(reference, distribution.path or distribution.url)
            _, version = parse_sdist_filename(distribution.name or posixpath.basename(link))
            requirement = f"{package.name}=={version}"
        results.append(
            ParsedRequirement(
                requirement=requirement,
                comes_from=reference,
                is_editable=isinstance(distribution, pylock.PackageDirectory)
                and bool(distribution.editable),
                options={"hashes": hashes} if hashes else None,
                locked_link=link,
                locked_hashes=hashes,
                locked_direct=direct,
                locked_name=package.name,
            )
        )
    return results
