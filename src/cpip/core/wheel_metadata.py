"""Small, dependency-free helpers for reading wheel core metadata."""

from __future__ import annotations

from collections.abc import Callable, Iterable

MetadataHeaders = dict[str, list[str]]

RESOLUTION_METADATA_HEADERS = frozenset(
    {"name", "version", "requires-dist", "provides-extra", "requires-python"},
)
FAST_METADATA_NAMES = {
    "Name": "name",
    "Version": "version",
    "Requires-Dist": "requires-dist",
    "Provides-Extra": "provides-extra",
    "Requires-Python": "requires-python",
}


def metadata_paths(names: Iterable[str]) -> list[str]:
    """Return top-level wheel METADATA members in stable order."""
    return sorted(
        name
        for name in names
        if name.endswith(".dist-info/METADATA") and name.count("/") == 1
    )


def parse_metadata_member(read: Callable[[str], bytes], member: str) -> MetadataHeaders:
    """Read and parse resolver metadata through an archive-like reader."""
    return parse_metadata_headers(read(member).decode("utf-8"))


def _header_end(contents: str) -> int:
    separators = (
        offset
        for marker in ("\n\n", "\r\n\r\n")
        if (offset := contents.find(marker)) >= 0
    )
    return min(separators, default=len(contents))


def parse_metadata_headers(contents: str) -> MetadataHeaders:
    """Parse only the core metadata fields needed by the resolver."""
    # Split only the header region: splitlines() on the whole document
    # would also split a long Description body into a list of lines the
    # loop below never looks at (it stops at the first blank line, which
    # is exactly where the header region ends).
    header_end = _header_end(contents)
    fast_headers: MetadataHeaders | None = {}
    for line in contents[:header_end].splitlines():
        if not line:
            break
        if line[0].isspace():
            fast_headers = None
            break
        name, separator, value = line.partition(":")
        if not separator:
            fast_headers = None
            break
        normalized_name = FAST_METADATA_NAMES.get(name)
        if normalized_name is None:
            if not name or name[0] not in "NnVvRrPp":
                continue
            if name.casefold() in RESOLUTION_METADATA_HEADERS:
                fast_headers = None
                break
            continue
        values = fast_headers.get(normalized_name)
        if values is None:
            values = []
            fast_headers[normalized_name] = values
        values.append(value.lstrip())
    if fast_headers is not None:
        return fast_headers
    headers: MetadataHeaders = {}
    current_values: list[str] | None = None
    saw_header = False
    for line in contents[:header_end].splitlines():
        if line and line[0].isspace():
            if current_values is None:
                if not saw_header:
                    break
            else:
                current_values[-1] += f"\n{line}"
            continue
        name, separator, value = line.partition(":")
        if not separator:
            break
        saw_header = True
        normalized_name = name.casefold()
        if normalized_name in RESOLUTION_METADATA_HEADERS:
            current_values = headers.get(normalized_name)
            if current_values is None:
                current_values = []
                headers[normalized_name] = current_values
            current_values.append(value.lstrip())
        else:
            current_values = None
    return headers
