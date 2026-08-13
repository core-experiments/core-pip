"""Canonical installation destinations.



All filesystem installation code should consume :class:`InstallTarget` rather

than calculating individual scheme paths.

"""

from __future__ import annotations

import os

from cpip.platform.locations.sysconfig import get_scheme
from cpip.platform.scheme import Scheme


class InstallTarget:
    """The complete destination scheme for one installation transaction."""

    __slots__ = (
        "data",
        "headers",
        "platlib",
        "purelib",
        "resolved_roots_internal",
        "scripts",
    )

    def __init__(
        self,
        purelib: str,
        platlib: str,
        headers: str,
        scripts: str,
        data: str,
    ) -> None:
        self.purelib = purelib

        self.platlib = platlib

        self.headers = headers

        self.scripts = scripts

        self.data = data

        self.resolved_roots_internal: dict[str, str] = {}

    @classmethod
    def from_scheme(cls, scheme: Scheme) -> InstallTarget:
        resolved: dict[str, str] = {}

        def resolve(path: str) -> str:
            cached = resolved.get(path)

            if cached is None:
                cached = os.path.realpath(path)

                resolved[path] = cached

            return cached

        return cls(
            purelib=resolve(scheme.purelib),
            platlib=resolve(scheme.platlib),
            headers=resolve(scheme.headers),
            scripts=resolve(scheme.scripts),
            data=resolve(scheme.data),
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
            target_text = os.fspath(target)

            scheme = Scheme(
                platlib=target_text,
                purelib=target_text,
                headers=target_text,
                # Keep target installs self-contained.  Sending scripts to the
                # active interpreter's bin directory makes an isolated target
                # install mutate the caller's environment and can create
                # unrelated-file collisions between packages.
                scripts=os.path.join(
                    target_text,
                    "Scripts" if os.name == "nt" else "bin",
                ),
                data=target_text,
            )

            if root is not None:
                scheme = apply_root(scheme, root)

            return cls.from_scheme(scheme)

        return cls.from_scheme(
            get_scheme(
                name,
                user=user,
                home=home,
                root=root,
                isolated=isolated,
                prefix=prefix,
            ),
        )

    @property
    def library_roots(self) -> tuple[str, str]:
        return self.purelib, self.platlib

    def destination(self, relative: str, *, base: str = "purelib") -> str:
        """Return a validated destination for a wheel-relative path."""

        root = getattr(self, base)

        root_text = os.fspath(root)

        destination_text = os.path.realpath(os.path.join(root_text, relative))

        resolved_root = self.resolved_roots_internal.get(root_text)

        if resolved_root is None:
            resolved_root = os.path.realpath(root_text)

            self.resolved_roots_internal[root_text] = resolved_root

        try:
            if os.path.commonpath((destination_text, resolved_root)) != resolved_root:
                raise ValueError

        except (OSError, ValueError) as exc:
            raise ValueError(f"path escapes installation target: {relative!r}") from exc

        return destination_text


def apply_root(scheme: Scheme, root: str) -> Scheme:
    def relocate(path: str) -> str:
        value = os.fspath(path)

        # ``/target`` is rooted on the current drive on Windows, but it still

        # represents a path relative to the synthetic installation root.

        # Checking the anchor handles both POSIX roots and drive-relative

        # Windows paths consistently.

        drive, tail = os.path.splitdrive(value)

        if (
            drive
            or tail.startswith(os.sep)
            or (os.altsep is not None and tail.startswith(os.altsep))
        ):
            value = tail.lstrip("/\\")

        return os.path.join(os.fspath(root), value)

    return Scheme(
        platlib=relocate(scheme.platlib),
        purelib=relocate(scheme.purelib),
        headers=relocate(scheme.headers),
        scripts=relocate(scheme.scripts),
        data=relocate(scheme.data),
    )
