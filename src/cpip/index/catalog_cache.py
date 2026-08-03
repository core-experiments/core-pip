"""Persistent cache for parsed Simple API catalog entries."""

from __future__ import annotations

import datetime
import marshal
import urllib.parse
from typing import Any, cast

from cpip.index.datetime import parse_iso_datetime
from cpip.index.links import Link
from cpip.index.source_models import MetadataFile

VERSION = 2
PREFIX = "cpip-index-catalog-v2:"


def cache_key(url: str) -> str:
    return PREFIX + url


def load_links(cache: Any, url: str) -> list[Link] | None:
    records = load_records(cache, url)
    return None if records is None else [link_from_record(record) for record in records]


def load_records(cache: Any, url: str) -> list[tuple[object, ...]] | None:
    if cache is None:
        return None
    raw = cache.get(cache_key(url))
    if raw is None:
        return None
    try:
        payload = marshal.loads(raw)
        records = payload[2]
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or payload[0] != "cpip-index-catalog"
            or payload[1] != VERSION
            or not isinstance(records, list)
        ):
            return None
        if not all(isinstance(record, tuple) for record in records):
            return None
        return cast("list[tuple[object, ...]]", records)
    except (EOFError, TypeError, ValueError, KeyError, IndexError):
        return None


def save_links(cache: Any, url: str, links: list[Link]) -> None:
    if cache is None:
        return
    try:
        payload = marshal.dumps(
            ("cpip-index-catalog", VERSION, [link_record(link) for link in links]),
        )
    except (TypeError, ValueError):
        return
    key = cache_key(url)
    cache.set(key, payload)
    cache.set_body(key, b"1")


def link_record(link: Link) -> tuple[object, ...]:
    metadata = link.metadata_file
    upload_time = link.upload_time
    return (
        link.url,
        (
            link.parsed_url_internal.scheme,
            link.parsed_url_internal.netloc,
            link.parsed_url_internal.path,
            link.parsed_url_internal.query,
            link.parsed_url_internal.fragment,
        ),
        link.comes_from,
        link.text,
        dict(link.hashes),
        link.requires_python,
        link.yanked_reason,
        None if metadata is None else dict(metadata.hashes or {}),
        None if upload_time is None else upload_time.isoformat(),
    )


def link_from_record(record: object) -> Link:
    if not isinstance(record, tuple) or len(record) != 9:
        raise ValueError("invalid catalog record")
    (
        url,
        parsed_url,
        source_url,
        text,
        hashes,
        requires_python,
        yanked,
        metadata,
        upload_time,
    ) = record
    if not isinstance(url, str) or not isinstance(text, str):
        raise ValueError("invalid catalog link")
    if (
        not isinstance(parsed_url, tuple)
        or len(parsed_url) != 5
        or not all(isinstance(value, str) for value in parsed_url)
    ):
        raise ValueError("invalid catalog URL")
    if source_url is not None and not isinstance(source_url, str):
        raise ValueError("invalid catalog source")
    if hashes is not None and (
        not isinstance(hashes, dict)
        or not all(isinstance(key, str) for key in hashes)
        or not all(isinstance(value, str) for value in hashes.values())
    ):
        raise ValueError("invalid catalog hashes")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or not all(isinstance(key, str) for key in metadata)
        or not all(isinstance(value, str) for value in metadata.values())
    ):
        raise ValueError("invalid catalog metadata")
    hashes_value = (
        cast("dict[str, object]", hashes) if isinstance(hashes, dict) else None
    )
    metadata_value = (
        cast("dict[str, str]", metadata) if isinstance(metadata, dict) else None
    )
    parsed_upload_time: datetime.datetime | None = None
    if upload_time is not None:
        if not isinstance(upload_time, str):
            raise ValueError("invalid catalog upload time")
        parsed_upload_time = parse_iso_datetime(upload_time)
    return Link.from_cached_record(
        url,
        parsed_url=urllib.parse.SplitResult(*parsed_url),
        source_url=source_url,
        text=text,
        hashes=hashes_value or {},
        requires_python=requires_python if isinstance(requires_python, str) else None,
        yanked_reason=yanked if isinstance(yanked, str) else None,
        metadata_file=(
            MetadataFile(metadata_value) if metadata_value is not None else None
        ),
        upload_time=parsed_upload_time,
    )
