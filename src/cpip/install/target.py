"""Canonical installation destinations.

All filesystem installation code should consume :class:`InstallTarget` rather
than calculating individual scheme paths.
"""

from __future__ import annotations

import os
from pathlib import Path

from cpip.platform.locations.sysconfig import get_scheme
from cpip.platform.scheme import Scheme


class InstallTarget:
    """The complete destination scheme for one installation transaction."""

    __slots__ = ("purelib", "platlib", "headers", "scripts", "data")

    def __init__(
        self,
        purelib: Path,
        platlib: Path,
        headers: Path,
        scripts: Path,
        data: Path,
    ) -> None:
        self.purelib = purelib
        self.platlib = platlib
        self.headers = headers
        self.scripts = scripts
        self.data = data

    @classmethod
    def from_scheme(cls, scheme: Scheme) -> InstallTarget:
        return cls(
            purelib=Path(scheme.purelib).resolve(strict=False),
            platlib=Path(scheme.platlib).resolve(strict=False),
            headers=Path(scheme.headers).resolve(strict=False),
            scripts=Path(scheme.scripts).resolve(strict=False),
            data=Path(scheme.data).resolve(strict=False),
        )

    @classmethod
    def from_options(
        cls,
        name: str,
        *,
        target: str | None = None,
        user: bool = False,
        home: str | None = None,
        prefix: str | None = None,
        root: str | None = None,
        isolated: bool = False,
    ) -> InstallTarget:
        if target is not None:
            target_path = Path(target)
            scheme = Scheme(
                platlib=os.fspath(target_path),
                purelib=os.fspath(target_path),
                headers=os.fspath(target_path),
                # Keep target installs self-contained.  Sending scripts to the
                # active interpreter's bin directory makes an isolated target
                # install mutate the caller's environment and can create
                # unrelated-file collisions between packages.
                scripts=os.fspath(
                    target_path / ("Scripts" if os.name == "nt" else "bin")
                ),
                data=os.fspath(target_path),
            )
            if root is not None:
                scheme = apply_root(scheme, Path(root))
            return cls.from_scheme(scheme)
        return cls.from_scheme(
            get_scheme(
                name,
                user=user,
                home=home,
                root=root,
                isolated=isolated,
                prefix=prefix,
            )
        )

    @property
    def library_roots(self) -> tuple[Path, Path]:
        return self.purelib, self.platlib

    @property
    def roots(self) -> tuple[Path, ...]:
        return self.library_roots + (self.headers, self.scripts, self.data)

    def destination(self, relative: str, *, base: str = "purelib") -> Path:
        """Return a validated destination for a wheel-relative path."""
        root = getattr(self, base)
        destination = (root / relative).resolve(strict=False)
        root = root.resolve(strict=False)
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"path escapes installation target: {relative!r}") from exc
        return destination


def apply_root(scheme: Scheme, root: Path) -> Scheme:
    def relocate(path: str) -> str:
        value = Path(path)
        # ``/target`` is rooted on the current drive on Windows, but it still
        # represents a path relative to the synthetic installation root.
        # Checking the anchor handles both POSIX roots and drive-relative
        # Windows paths consistently.
        if value.anchor:
            value = Path(*value.parts[1:])
        return os.fspath(root / value)

    return Scheme(
        platlib=relocate(scheme.platlib),
        purelib=relocate(scheme.purelib),
        headers=relocate(scheme.headers),
        scripts=relocate(scheme.scripts),
        data=relocate(scheme.data),
    )
