"""Shared parsing for the metadata fields used during resolution."""

from __future__ import annotations

from pip.index.metadata_cache import MetadataHeaders

RESOLUTION_METADATA_HEADERS = frozenset(
    {"name", "version", "requires-dist", "provides-extra", "requires-python"}
)


def parse_metadata_headers(contents: str) -> MetadataHeaders:
    """Parse only the core metadata fields needed by the resolver."""
    separators = (
        offset
        for marker in ("\n\n", "\r\n\r\n")
        if (offset := contents.find(marker)) >= 0
    )
    header_end = min(separators, default=len(contents))
    headers: MetadataHeaders = {}
    current_values: list[str] | None = None
    saw_header = False
    for line in contents[:header_end].splitlines():
        if line[:1].isspace():
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
            current_values = headers.setdefault(normalized_name, [])
            current_values.append(value.lstrip())
        else:
            current_values = None
    return headers
