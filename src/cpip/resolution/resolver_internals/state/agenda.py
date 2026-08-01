"""Rollback-friendly pending requirement agenda."""

from __future__ import annotations

from bisect import bisect_left, insort
from collections.abc import Iterable, Iterator

from cpip.core.packaging import Requirement
from cpip.resolution.resolver_internals.state.domains import (
    RequirementStateKey,
    requirement_state_key,
)


class AgendaEntry:
    __slots__ = ("requirement", "previous", "next", "order", "active")

    def __init__(
        self,
        requirement: Requirement,
        previous: int | None,
        next: int | None,
        order: int,
        active: bool = True,
    ) -> None:
        self.requirement = requirement
        self.previous = previous
        self.next = next
        self.order = order
        self.active = active


class AgendaMutation:
    __slots__ = (
        "kind",
        "entry",
        "previous",
        "next",
        "old_head",
        "old_tail",
        "old_front_order",
    )

    def __init__(
        self,
        kind: str,
        entry: int,
        previous: int | None,
        next: int | None,
        old_head: int | None,
        old_tail: int | None,
        old_front_order: int,
    ) -> None:
        self.kind = kind
        self.entry = entry
        self.previous = previous
        self.next = next
        self.old_head = old_head
        self.old_tail = old_tail
        self.old_front_order = old_front_order


class PendingAgenda:
    """Rollback-friendly ordered requirements pending a resolver decision."""

    __slots__ = (
        "by_name",
        "entries_internal",
        "front_order",
        "head_internal",
        "key_cache",
        "length_internal",
        "state_keys",
        "tail_internal",
        "undo",
    )

    def __init__(self, requirements: Iterable[Requirement] = ()) -> None:
        self.entries_internal: list[AgendaEntry] = []
        self.head_internal: int | None = None
        self.tail_internal: int | None = None
        self.front_order = -1
        self.length_internal = 0
        self.undo: list[AgendaMutation] = []
        self.by_name: dict[str, set[int]] = {}
        self.key_cache: dict[int, RequirementStateKey] = {}
        self.state_keys: list[RequirementStateKey] = []
        self.append_initial(requirements)

    def key_internal(self, requirement: Requirement) -> RequirementStateKey:
        key = self.key_cache.get(id(requirement))
        if key is None:
            key = requirement_state_key(requirement)
            self.key_cache[id(requirement)] = key
        return key

    def state_key(self) -> tuple[RequirementStateKey, ...]:
        return tuple(self.state_keys)

    def remove_state_key(self, requirement: Requirement) -> None:
        key = self.key_internal(requirement)
        index = bisect_left(self.state_keys, key)
        assert self.state_keys[index] == key
        self.state_keys.pop(index)

    def add_state_key(self, requirement: Requirement) -> None:
        insort(self.state_keys, self.key_internal(requirement))

    def append_initial(self, requirements: Iterable[Requirement]) -> None:
        for order, requirement in enumerate(requirements):
            entry_id = len(self.entries_internal)
            self.entries_internal.append(
                AgendaEntry(requirement, self.tail_internal, None, order)
            )
            self.add_state_key(requirement)
            self.by_name.setdefault(requirement.canonical_name, set()).add(entry_id)
            if self.tail_internal is None:
                self.head_internal = entry_id
            else:
                self.entries_internal[self.tail_internal].next = entry_id
            self.tail_internal = entry_id
            self.length_internal += 1

    def __bool__(self) -> bool:
        return bool(self.length_internal)

    def __len__(self) -> int:
        return self.length_internal

    def __iter__(self) -> Iterator[Requirement]:
        entry_id = self.head_internal
        while entry_id is not None:
            entry = self.entries_internal[entry_id]
            yield entry.requirement
            entry_id = entry.next

    def iter_entries(self) -> Iterator[tuple[int, Requirement]]:
        entry_id = self.head_internal
        while entry_id is not None:
            entry = self.entries_internal[entry_id]
            yield entry_id, entry.requirement
            entry_id = entry.next

    def first(self) -> tuple[int, Requirement]:
        assert self.head_internal is not None
        return self.head_internal, self.entries_internal[self.head_internal].requirement

    def checkpoint(self) -> int:
        return len(self.undo)

    def remove(self, entry_id: int) -> None:
        entry = self.entries_internal[entry_id]
        assert entry.active
        self.undo.append(
            AgendaMutation(
                "remove",
                entry_id,
                entry.previous,
                entry.next,
                self.head_internal,
                self.tail_internal,
                self.front_order,
            )
        )
        if entry.previous is None:
            self.head_internal = entry.next
        else:
            self.entries_internal[entry.previous].next = entry.next
        if entry.next is None:
            self.tail_internal = entry.previous
        else:
            self.entries_internal[entry.next].previous = entry.previous
        entry.active = False
        self.remove_state_key(entry.requirement)
        name = entry.requirement.canonical_name
        self.by_name[name].remove(entry_id)
        if not self.by_name[name]:
            self.by_name.pop(name)
        self.length_internal -= 1

    def prepend(self, requirements: Iterable[Requirement]) -> None:
        items = tuple(requirements)
        if not items:
            return
        old_head = self.head_internal
        old_tail = self.tail_internal
        old_front_order = self.front_order
        first_id = len(self.entries_internal)
        previous: int | None = None
        first_order = self.front_order - len(items) + 1
        for offset, requirement in enumerate(items):
            entry_id = len(self.entries_internal)
            self.entries_internal.append(
                AgendaEntry(requirement, previous, None, first_order + offset)
            )
            self.add_state_key(requirement)
            self.by_name.setdefault(requirement.canonical_name, set()).add(entry_id)
            if previous is not None:
                self.entries_internal[previous].next = entry_id
            previous = entry_id
        last_id = len(self.entries_internal) - 1
        self.entries_internal[last_id].next = old_head
        if old_head is not None:
            self.entries_internal[old_head].previous = last_id
        else:
            self.tail_internal = last_id
        self.head_internal = first_id
        self.front_order = first_order - 1
        self.length_internal += len(items)
        self.undo.append(
            AgendaMutation(
                "prepend", first_id, None, None, old_head, old_tail, old_front_order
            )
        )

    def rollback(self, checkpoint: int) -> None:
        while len(self.undo) > checkpoint:
            mutation = self.undo.pop()
            if mutation.kind == "remove":
                entry = self.entries_internal[mutation.entry]
                entry.previous = mutation.previous
                entry.next = mutation.next
                entry.active = True
                self.add_state_key(entry.requirement)
                self.by_name.setdefault(entry.requirement.canonical_name, set()).add(
                    mutation.entry
                )
                self.head_internal = mutation.old_head
                self.tail_internal = mutation.old_tail
                if mutation.previous is not None:
                    self.entries_internal[mutation.previous].next = mutation.entry
                if mutation.next is not None:
                    self.entries_internal[mutation.next].previous = mutation.entry
                self.length_internal += 1
                continue
            removed = len(self.entries_internal) - mutation.entry
            for entry_id in range(mutation.entry, len(self.entries_internal)):
                requirement = self.entries_internal[entry_id].requirement
                self.remove_state_key(requirement)
                name = requirement.canonical_name
                self.by_name[name].remove(entry_id)
                if not self.by_name[name]:
                    self.by_name.pop(name)
            del self.entries_internal[mutation.entry :]
            self.head_internal = mutation.old_head
            self.tail_internal = mutation.old_tail
            self.front_order = mutation.old_front_order
            if mutation.old_head is not None:
                self.entries_internal[mutation.old_head].previous = None
            if mutation.old_tail is not None:
                self.entries_internal[mutation.old_tail].next = None
            self.length_internal -= removed
