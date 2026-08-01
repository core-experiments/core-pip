"""Safe filesystem path helpers for downloaded artifacts."""

from __future__ import annotations

import os


class PathComponent(str):
    """A filename component that cannot escape its containing directory."""

    @classmethod
    def from_name(cls, name: str, *, required: bool = False) -> PathComponent:
        component = os.path.basename(name)
        if component in ("", os.curdir, os.pardir):
            component = ""
        if required and not component:
            raise ValueError(f"Unexpected file name derived from URL: {name!r}")
        return cls(component)

    def join(self, directory: str) -> str:  # ty: ignore[invalid-method-override]
        return os.path.join(directory, self)
