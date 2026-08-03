"""Static interfaces consumed while parsing requirements files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cpip.core.format_control import FormatControl
    from cpip.core.release_control import ReleaseControl


class RequirementSession(Protocol):
    """Network operations needed by the requirement-file parser."""

    @property
    def auth(self) -> Any: ...

    @property
    def trusted_hosts(self) -> Any: ...

    def get(self, *args: Any, **kwargs: Any) -> Any: ...


class RequirementSource(Protocol):
    """Mutable source options updated by requirement-file directives."""

    find_links: list[str]
    index_urls: list[str]
    no_index: bool

    @property
    def format_control(self) -> FormatControl | None: ...

    @property
    def release_control(self) -> ReleaseControl | None: ...
