"""Per-resolution mutable state.

The session is intentionally a data owner, not a method-dispatch context.
Search operations receive it explicitly and may not reach back into the
public engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ResolutionSession:
    pending: list[Any] = field(default_factory=list)
    selected: dict[str, Any] = field(default_factory=dict)
    selected_extras: dict[str, frozenset[str]] = field(default_factory=dict)
    satisfied: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, set[str]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    def checkpoint(self) -> tuple[int, tuple[str, ...]]:
        return len(self.pending), tuple(self.selected)
