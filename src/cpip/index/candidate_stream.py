"""Replayable candidate streams used by index and resolver boundaries."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import overload

from cpip.core.wheel import WheelCandidate


class CandidateStream(Sequence[WheelCandidate]):
    """A replayable sequence that materializes candidates on demand."""

    __slots__ = ("source_internal", "items_internal", "exhausted", "error_internal")

    def __init__(self, source: Iterator[WheelCandidate]) -> None:
        self.source_internal = source
        self.items_internal: list[WheelCandidate] = []
        self.exhausted = False
        self.error_internal: Exception | None = None

    def advance(self) -> bool:
        if self.error_internal is not None:
            raise self.error_internal
        if self.exhausted:
            return False
        try:
            item = next(self.source_internal)
        except StopIteration:
            self.exhausted = True
            return False
        except Exception as exc:
            self.error_internal = exc
            raise
        self.items_internal.append(item)
        return True

    def __iter__(self) -> Iterator[WheelCandidate]:
        if self.exhausted:
            return iter(self.items_internal)
        return self._iter_pending()

    def _iter_pending(self) -> Iterator[WheelCandidate]:
        items = self.items_internal
        yield from items
        if self.error_internal is not None:
            raise self.error_internal
        if self.exhausted:
            return
        source = self.source_internal
        while True:
            try:
                item = next(source)
            except StopIteration:
                self.exhausted = True
                return
            except Exception as exc:
                self.error_internal = exc
                raise
            items.append(item)
            yield item

    def __bool__(self) -> bool:
        return bool(self.items_internal) or self.advance()

    def __len__(self) -> int:
        while self.advance():
            pass
        return len(self.items_internal)

    @overload
    def __getitem__(self, index: int) -> WheelCandidate: ...

    @overload
    def __getitem__(self, index: slice) -> list[WheelCandidate]: ...

    def __getitem__(self, index: int | slice) -> WheelCandidate | list[WheelCandidate]:
        if isinstance(index, slice):
            if (
                index.stop is None
                or (index.start is not None and index.start < 0)
                or index.stop < 0
                or (index.step is not None and index.step < 0)
            ):
                len(self)
            else:
                while len(self.items_internal) < index.stop and self.advance():
                    pass
            return self.items_internal[index]
        if index < 0:
            len(self)
        else:
            while len(self.items_internal) <= index and self.advance():
                pass
        return self.items_internal[index]

    def prefer(
        self,
        keep: Callable[[WheelCandidate], bool],
        *,
        decisive: Callable[[WheelCandidate], bool] | None = None,
    ) -> CandidateStream:
        """Prefer matching candidates, falling back to the full stream if none do."""

        decisive = decisive or keep

        def generate() -> Iterator[WheelCandidate]:
            buffered: list[WheelCandidate] = []
            preference_found = False
            for candidate in self:
                if preference_found:
                    if keep(candidate):
                        yield candidate
                    continue
                buffered.append(candidate)
                if decisive(candidate):
                    preference_found = True
                    for item in buffered:
                        if keep(item):
                            yield item
                    buffered.clear()
            if not preference_found:
                yield from buffered

        return CandidateStream(generate())
