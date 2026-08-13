from __future__ import annotations

from .errors import CommandError
from .packaging import canonicalize_name

RELEASE_CONTROL_KINDS = frozenset(("all_releases", "only_final"))
RELEASE_CONTROL_SENTINELS = frozenset((":all:", ":none:"))


class ReleaseControl:
    __slots__ = ("all_releases", "only_final")

    def __init__(
        self,
        all_releases: set[str] | None = None,
        only_final: set[str] | None = None,
    ) -> None:
        self.all_releases = all_releases if all_releases is not None else set()
        self.only_final = only_final if only_final is not None else set()

    def apply(self, kind: str, value: str) -> None:
        if kind not in RELEASE_CONTROL_KINDS:
            raise ValueError(f"unknown release control kind: {kind}")
        if value.startswith("-"):
            raise CommandError(
                "--all-releases / --only-final option requires 1 argument.",
            )
        entries = [item.strip() for item in value.split(",") if item.strip()]
        if not entries:
            return
        target = self.all_releases if kind == "all_releases" else self.only_final
        opposite = self.only_final if kind == "all_releases" else self.all_releases
        for entry in entries:
            normalized = (
                canonicalize_name(entry)
                if entry not in RELEASE_CONTROL_SENTINELS
                else entry
            )
            if normalized == ":none:":
                target.clear()
                continue
            if normalized == ":all:":
                target.discard(":none:")
                opposite.discard(":all:")
                target.add(":all:")
                continue
            opposite.discard(normalized)
            target.add(normalized)

    def allows_prereleases(self, project_name: str) -> bool | None:
        canonical = canonicalize_name(project_name)
        if canonical in self.all_releases:
            return True
        if canonical in self.only_final:
            return False
        if ":all:" in self.only_final:
            return False
        if ":all:" in self.all_releases:
            return True
        return None
