"""Canonical installation destinations.

All filesystem installation code should consume :class:`InstallTarget` rather
than calculating individual scheme paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pip.platform.locations.sysconfig import get_scheme
from pip.core.metadata import default_scripts_path
from pip.platform.scheme import Scheme


@dataclass(frozen=True, slots=True)
class InstallTarget:
    """The complete destination scheme for one installation transaction."""

    purelib: Path
    platlib: Path
    headers: Path
    scripts: Path
    data: Path

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
                # Preserve pip's target-mode behavior: package/data files are
                # relocated, while console scripts use the active interpreter's
                # scripts directory.
                scripts=os.fspath(default_scripts_path()),
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
        if value.is_absolute():
            value = Path(*value.parts[1:])
        return os.fspath(root / value)

    return Scheme(
        platlib=relocate(scheme.platlib),
        purelib=relocate(scheme.purelib),
        headers=relocate(scheme.headers),
        scripts=relocate(scheme.scripts),
        data=relocate(scheme.data),
    )
