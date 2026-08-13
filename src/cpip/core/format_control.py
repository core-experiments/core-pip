from __future__ import annotations

from .packaging import canonicalize_name

FORMAT_CONTROL_KINDS = frozenset(
    ("no_binary", "only_binary", "no-binary", "only-binary"),
)
ONLY_BINARY_KINDS = frozenset(("only_binary", "only-binary"))
FORMAT_CONTROL_SENTINELS = frozenset((":all:", ":none:"))


class FormatControl:
    def __init__(
        self,
        no_binary: set[str] | None = None,
        only_binary: set[str] | None = None,
    ) -> None:
        self.no_binary: set[str] = set(no_binary or ())
        self.only_binary: set[str] = set(only_binary or ())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FormatControl):
            return NotImplemented
        return (
            self.no_binary == other.no_binary and self.only_binary == other.only_binary
        )

    def apply(self, kind: str, value: str) -> None:
        if kind not in FORMAT_CONTROL_KINDS:
            raise ValueError(f"unknown format control kind: {kind}")
        only_binary = kind in ONLY_BINARY_KINDS
        entries = [item.strip() for item in value.split(",") if item.strip()]
        if not entries:
            return
        target = self.only_binary if only_binary else self.no_binary
        opposite = self.no_binary if only_binary else self.only_binary
        for entry in entries:
            normalized = (
                canonicalize_name(entry)
                if entry not in FORMAT_CONTROL_SENTINELS
                else entry
            )
            if normalized == ":none:":
                target.clear()
                continue
            if normalized == ":all:":
                opposite.clear()
                target.discard(":none:")
                target.add(":all:")
                continue
            opposite.discard(normalized)
            target.add(normalized)

    def allowed_formats(self, project_name: str) -> tuple[bool, bool]:
        canonical = canonicalize_name(project_name)
        allow_binary = True
        allow_source = True
        if ":all:" in self.only_binary:
            allow_source = False
        if ":all:" in self.no_binary:
            allow_binary = False
        if canonical in self.only_binary:
            allow_binary = True
            allow_source = False
        if canonical in self.no_binary:
            allow_binary = False
            allow_source = True
        return allow_binary, allow_source

    def get_allowed_formats(self, project_name: str) -> frozenset[str]:
        allow_binary, allow_source = self.allowed_formats(project_name)
        result: set[str] = set()
        if allow_binary:
            result.add("binary")
        if allow_source:
            result.add("source")
        return frozenset(result)
