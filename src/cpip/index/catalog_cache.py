"""Persistent cache for parsed Simple API catalog entries."""

from __future__ import annotations

import datetime
import hashlib
import marshal
import posixpath
import urllib.parse
from typing import Any, cast

from cpip.core.packaging import Version
from cpip.core.wheel import parse_wheel_file
from cpip.index.datetime import parse_iso_datetime
from cpip.index.directory_index import project_version_from_filename
from cpip.index.links import Link
from cpip.index.source_models import ArtifactKind, MetadataFile

VERSION = 6
PREFIX = "cpip-index-catalog-v6:"
LEGACY_PREFIX = "cpip-index-catalog-v2:"
SUMMARY_VERSION = 3
SUMMARY_PREFIX = "cpip-index-summary-v3:"
LEGACY_SUMMARY_VERSION = 2
LEGACY_SUMMARY_PREFIX = "cpip-index-summary-v2:"
CHOICE_VERSION = 1
CHOICE_PREFIX = "cpip-index-choice-v1:"
SUMMARY_HEADER = b"cpip-index-summary-v3\0"
LEGACY_SUMMARY_HEADER = b"cpip-index-summary-v2\0"
CHOICE_HEADER = b"cpip-index-choice-v1\0"

WHEEL_RECORD = 1
SDIST_RECORD = 2
RECORD_REQUIRES_PYTHON = 3
RECORD_YANKED = 4

CatalogRecord = tuple[object, ...]
CatalogArtifact = tuple[int, CatalogRecord]
CatalogFact = tuple[int, str | None, str | None]
CatalogGroup = tuple[str, str, list[CatalogArtifact], list[CatalogFact]]
CatalogData = tuple[list[CatalogGroup], list[CatalogRecord]]
CatalogSummaryGroup = tuple[str, str, tuple[object, ...], list[CatalogFact]]
CatalogChoice = tuple[CatalogRecord, int, int | None]
CatalogChoices = dict[str, CatalogChoice | None]
CatalogChoiceProfiles = dict[tuple[str, bool, bool], CatalogChoices]
CatalogSummary = tuple[
    str,
    list[CatalogSummaryGroup],
    bool,
    CatalogChoiceProfiles,
]


def cache_key(url: str) -> str:
    return PREFIX + url


def summary_key(url: str) -> str:
    return SUMMARY_PREFIX + url


def choice_key(
    url: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
) -> str:
    return (
        f"{CHOICE_PREFIX}{target_key}:{int(allow_binary)}:{int(allow_source)}:{url}"
    )


def load_links(cache: Any, url: str) -> list[Link] | None:
    records = load_records(cache, url)
    return (
        None
        if records is None
        else [link_from_record(record, source_url=url) for record in records]
    )


def load_records(cache: Any, url: str) -> list[tuple[object, ...]] | None:
    catalog = load_catalog(cache, url)
    if catalog is None:
        return None
    groups, unparsed = catalog
    return [
        *(
            record
            for _name, _version, artifacts, _facts in groups
            for _kind, record in artifacts
        ),
        *unparsed,
    ]


def load_catalog(cache: Any, url: str) -> CatalogData | None:
    """Load compact records grouped by their target-independent release."""
    if cache is None:
        return None
    raw = get_cache_entry(cache, cache_key(url))
    if raw is None:
        migrated = migrate_legacy_catalog(cache, url)
        if migrated is not None:
            save_catalog(cache, url, migrated)
        return migrated
    try:
        payload = marshal.loads(raw)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 4
            or payload[0] != "cpip-index-catalog"
            or payload[1] != VERSION
            or not isinstance(payload[2], list)
            or not isinstance(payload[3], list)
        ):
            return None
        groups = payload[2]
        unparsed = payload[3]
        if not all(valid_group(group) for group in groups) or not all(
            valid_record(record) for record in unparsed
        ):
            return None
        return cast("CatalogData", (groups, unparsed))
    except (EOFError, TypeError, ValueError, KeyError, IndexError):
        return None


def load_summary(cache: Any, url: str) -> CatalogSummary | None:
    """Load the release-only resolver view, compiling it locally if needed."""
    if cache is None:
        return None
    raw = get_cache_entry(cache, summary_key(url))
    if raw is not None:
        summary = decode_summary(raw)
        if summary is not None:
            if not raw.startswith(SUMMARY_HEADER):
                save_summary_value(cache, url, summary)
            return summary
    legacy_raw = get_cache_entry(cache, LEGACY_SUMMARY_PREFIX + url)
    if legacy_raw is not None:
        summary = decode_legacy_summary(legacy_raw)
        if summary is not None:
            save_summary_value(cache, url, summary)
            return summary
    catalog = load_catalog(cache, url)
    if catalog is None:
        return None
    catalog_raw = get_cache_entry(cache, cache_key(url))
    if catalog_raw is None:
        return None
    generation = catalog_generation(catalog_raw)
    save_summary(cache, url, catalog, generation)
    return summary_from_catalog(catalog, generation)


def decode_summary(raw: bytes) -> CatalogSummary | None:
    if raw.startswith(SUMMARY_HEADER):
        payload = decode_checked_payload(raw, SUMMARY_HEADER)
        if (
            not isinstance(payload, tuple)
            or len(payload) not in {3, 4}
            or not isinstance(payload[0], str)
            or not isinstance(payload[1], list)
            or not isinstance(payload[2], bool)
            or (len(payload) == 4 and not isinstance(payload[3], dict))
        ):
            return None
        if len(payload) == 3:
            return cast(
                "CatalogSummary",
                (payload[0], payload[1], payload[2], {}),
            )
        return cast("CatalogSummary", payload)
    try:
        payload = marshal.loads(raw)
    except (EOFError, TypeError, ValueError):
        return None
    if (
        not isinstance(payload, tuple)
        or len(payload) != 5
        or payload[0] != "cpip-index-summary"
        or payload[1] != SUMMARY_VERSION
        or not isinstance(payload[2], str)
        or not isinstance(payload[3], list)
        or not isinstance(payload[4], bool)
        or not all(valid_summary_group(group) for group in payload[3])
    ):
        return None
    return cast(
        "CatalogSummary",
        (payload[2], payload[3], payload[4], {}),
    )


def decode_legacy_summary(raw: bytes) -> CatalogSummary | None:
    if raw.startswith(LEGACY_SUMMARY_HEADER):
        payload = decode_checked_payload(raw, LEGACY_SUMMARY_HEADER)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or not isinstance(payload[0], str)
            or not isinstance(payload[1], list)
            or not isinstance(payload[2], bool)
            or not all(valid_summary_group(group) for group in payload[1])
        ):
            return None
        summary = cast(
            "CatalogSummary",
            (payload[0], payload[1], payload[2], {}),
        )
    else:
        try:
            payload = marshal.loads(raw)
        except (EOFError, TypeError, ValueError):
            return None
        if (
            not isinstance(payload, tuple)
            or len(payload) != 5
            or payload[0] != "cpip-index-summary"
            or payload[1] != LEGACY_SUMMARY_VERSION
            or not isinstance(payload[2], str)
            or not isinstance(payload[3], list)
            or not isinstance(payload[4], bool)
            or not all(valid_summary_group(group) for group in payload[3])
        ):
            return None
        summary = cast(
            "CatalogSummary",
            (payload[2], payload[3], payload[4], {}),
        )
    summary[1].sort(key=summary_group_sort_key)
    return summary


def load_choices(
    cache: Any,
    url: str,
    generation: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
) -> CatalogChoices:
    if cache is None:
        return {}
    raw = get_cache_entry(
        cache,
        choice_key(url, target_key, allow_binary, allow_source),
    )
    if raw is None:
        return {}
    if raw.startswith(CHOICE_HEADER):
        payload = decode_checked_payload(raw, CHOICE_HEADER)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or payload[0] != generation
            or not isinstance(payload[1], dict)
        ):
            return {}
        choices = cast("CatalogChoices", payload[1])
        embed_summary_choices(
            cache,
            url,
            generation,
            target_key,
            allow_binary,
            allow_source,
            choices,
        )
        return choices
    try:
        payload = marshal.loads(raw)
    except (EOFError, TypeError, ValueError):
        return {}
    if (
        not isinstance(payload, tuple)
        or len(payload) != 4
        or payload[0] != "cpip-index-choice"
        or payload[1] != CHOICE_VERSION
        or payload[2] != generation
        or not isinstance(payload[3], dict)
        or not all(
            isinstance(version, str) and valid_choice(choice)
            for version, choice in payload[3].items()
        )
    ):
        return {}
    choices = cast("CatalogChoices", payload[3])
    save_choices(
        cache,
        url,
        generation,
        target_key,
        allow_binary,
        allow_source,
        choices,
    )
    return choices


def save_choices(
    cache: Any,
    url: str,
    generation: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
    choices: CatalogChoices,
) -> None:
    if cache is None:
        return
    try:
        payload = encode_checked_payload(
            CHOICE_HEADER,
            (generation, choices),
        )
    except (TypeError, ValueError):
        return
    set_cache_entry(
        cache,
        choice_key(url, target_key, allow_binary, allow_source),
        payload,
    )
    embed_summary_choices(
        cache,
        url,
        generation,
        target_key,
        allow_binary,
        allow_source,
        choices,
    )


def embed_summary_choices(
    cache: Any,
    url: str,
    generation: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
    choices: CatalogChoices,
) -> None:
    """Co-locate the hot target profile with its generation-scoped summary."""
    summary = load_summary(cache, url)
    if summary is None or summary[0] != generation:
        return
    profile_key = target_key, allow_binary, allow_source
    if summary[3].get(profile_key) == choices:
        return
    profiles = dict(summary[3])
    profiles[profile_key] = choices
    save_summary_value(
        cache,
        url,
        (summary[0], summary[1], summary[2], profiles),
    )


def valid_group(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 4
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and isinstance(value[2], list)
        and isinstance(value[3], list)
        and all(
            isinstance(artifact, tuple)
            and len(artifact) == 2
            and isinstance(artifact[0], int)
            and valid_record(artifact[1])
            for artifact in value[2]
        )
        and all(valid_fact(fact) for fact in value[3])
    )


def valid_summary_group(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 4
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and valid_version_state(value[2])
        and isinstance(value[3], list)
        and all(valid_fact(fact) for fact in value[3])
    )


def valid_record(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 7


def valid_fact(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and isinstance(value[0], int)
        and (value[1] is None or isinstance(value[1], str))
        and (value[2] is None or isinstance(value[2], str))
    )


def valid_choice(value: object) -> bool:
    return value is None or (
        isinstance(value, tuple)
        and len(value) == 3
        and valid_record(value[0])
        and isinstance(value[1], int)
        and (value[2] is None or isinstance(value[2], int))
    )


def valid_version_state(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 8
        and isinstance(value[0], int)
        and isinstance(value[1], tuple)
        and bool(value[1])
        and all(isinstance(part, int) for part in value[1])
        and (
            value[2] is None
            or (
                isinstance(value[2], tuple)
                and len(value[2]) == 2
                and all(isinstance(part, int) for part in value[2])
            )
        )
        and (value[3] is None or isinstance(value[3], int))
        and (value[4] is None or isinstance(value[4], int))
        and (value[5] is None or isinstance(value[5], str))
        and isinstance(value[6], str)
        and isinstance(value[7], tuple)
    )


def save_links(cache: Any, url: str, links: list[Link]) -> None:
    if cache is None:
        return
    grouped: dict[tuple[str, str], list[CatalogArtifact]] = {}
    unparsed: list[CatalogRecord] = []
    for link in links:
        record = link_record(link)
        identity = artifact_identity(link)
        if identity is None:
            unparsed.append(record)
            continue
        kind, name, version = identity
        grouped.setdefault((name, version), []).append((kind, record))
    save_catalog(
        cache,
        url,
        (
            compile_groups(grouped),
            unparsed,
        ),
    )


def compile_groups(
    grouped: dict[tuple[str, str], list[CatalogArtifact]],
) -> list[CatalogGroup]:
    result: list[CatalogGroup] = []
    for (name, version), artifacts in grouped.items():
        result.append(
            (
                name,
                version,
                artifacts,
                release_facts(artifacts),
            ),
        )
    return result


def release_facts(artifacts: list[CatalogArtifact]) -> list[CatalogFact]:
    """Summarize target-independent artifact eligibility for one release."""
    fact_masks: dict[tuple[str | None, str | None], int] = {}
    for kind, record in artifacts:
        requires_python = record[RECORD_REQUIRES_PYTHON]
        yanked = record[RECORD_YANKED]
        fact_key = (
            requires_python if isinstance(requires_python, str) else None,
            yanked if isinstance(yanked, str) else None,
        )
        fact_masks[fact_key] = fact_masks.get(fact_key, 0) | kind
    return [
        (kind_mask, requires_python, yanked)
        for (requires_python, yanked), kind_mask in fact_masks.items()
    ]


def save_catalog(cache: Any, url: str, catalog: CatalogData) -> None:
    try:
        payload = marshal.dumps(
            ("cpip-index-catalog", VERSION, catalog[0], catalog[1]),
        )
    except (TypeError, ValueError):
        return
    generation = catalog_generation(payload)
    set_cache_entry(cache, cache_key(url), payload)
    save_summary(cache, url, catalog, generation)


def catalog_generation(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def summary_from_catalog(
    catalog: CatalogData,
    generation: str,
) -> CatalogSummary:
    groups, unparsed = catalog
    summary_groups = [
        (
            name,
            version,
            Version(version).cache_state_internal(),
            facts,
        )
        for name, version, _artifacts, facts in groups
    ]
    summary_groups.sort(key=summary_group_sort_key)
    return generation, summary_groups, bool(unparsed), {}


def summary_group_sort_key(group: CatalogSummaryGroup) -> Any:
    return group[2][7]


def save_summary(
    cache: Any,
    url: str,
    catalog: CatalogData,
    generation: str,
) -> None:
    summary = summary_from_catalog(catalog, generation)
    save_summary_value(cache, url, summary)


def save_summary_value(
    cache: Any,
    url: str,
    summary: CatalogSummary,
) -> None:
    try:
        payload = encode_checked_payload(
            SUMMARY_HEADER,
            summary,
        )
    except (TypeError, ValueError):
        return
    set_cache_entry(cache, summary_key(url), payload)


def encode_checked_payload(header: bytes, payload: object) -> bytes:
    body = marshal.dumps(payload)  # ty: ignore[invalid-argument-type]
    return header + hashlib.sha256(body).digest() + body


def decode_checked_payload(raw: bytes, header: bytes) -> object | None:
    digest_start = len(header)
    body_start = digest_start + hashlib.sha256().digest_size
    if len(raw) < body_start:
        return None
    body = raw[body_start:]
    if raw[digest_start:body_start] != hashlib.sha256(body).digest():
        return None
    try:
        return marshal.loads(body)
    except (EOFError, TypeError, ValueError):
        return None


def set_cache_entry(cache: Any, key: str, payload: bytes) -> None:
    setter = getattr(cache, "set_atomic", None)
    if setter is not None:
        setter(key, payload)
        return
    cache.set(key, payload)
    cache.set_body(key, b"1")


def get_cache_entry(cache: Any, key: str) -> bytes | None:
    getter = getattr(cache, "get_atomic", None)
    if getter is not None:
        value = getter(key)
        if value is not None:
            return value
    value = cache.get(key)
    if value is not None:
        setter = getattr(cache, "set_atomic", None)
        if setter is not None:
            setter(key, value)
    return value


def migrate_legacy_catalog(cache: Any, url: str) -> CatalogData | None:
    """Compile the v2 parsed-link cache without fetching or reparsing the page."""
    raw = get_cache_entry(cache, LEGACY_PREFIX + url)
    if raw is None:
        return None
    try:
        payload = marshal.loads(raw)
    except (EOFError, TypeError, ValueError):
        return None
    if (
        not isinstance(payload, tuple)
        or len(payload) != 3
        or payload[0] != "cpip-index-catalog"
        or payload[1] != 2
        or not isinstance(payload[2], list)
    ):
        return None
    grouped: dict[tuple[str, str], list[CatalogArtifact]] = {}
    unparsed: list[CatalogRecord] = []
    for legacy_record in payload[2]:
        migrated = migrate_legacy_record(legacy_record)
        if migrated is None:
            continue
        record, filename = migrated
        identity = artifact_identity_from_filename(filename)
        if identity is None:
            unparsed.append(record)
            continue
        kind, name, version = identity
        grouped.setdefault((name, version), []).append((kind, record))
    return (
        compile_groups(grouped),
        unparsed,
    )


def migrate_legacy_record(value: object) -> tuple[CatalogRecord, str] | None:
    if not isinstance(value, tuple) or len(value) != 9:
        return None
    parsed_url = value[1]
    if (
        not isinstance(value[0], str)
        or not isinstance(parsed_url, tuple)
        or len(parsed_url) != 5
        or not isinstance(parsed_url[2], str)
        or not isinstance(value[3], str)
    ):
        return None
    filename = posixpath.basename(urllib.parse.unquote(parsed_url[2]).rstrip("/"))
    return (
        (
            value[0],
            value[3],
            value[4],
            value[5],
            value[6],
            value[7],
            value[8],
        ),
        filename,
    )


def artifact_identity(link: Link) -> tuple[int, str, str] | None:
    """Compile the artifact identity once when its Simple API page changes."""
    filename = posixpath.basename(
        urllib.parse.unquote(link.parsed_url_internal.path).rstrip("/"),
    )
    return artifact_identity_from_filename(filename, kind=link.kind)


def artifact_identity_from_filename(
    filename: str,
    *,
    kind: ArtifactKind | None = None,
) -> tuple[int, str, str] | None:
    if kind is None:
        kind = Link.artifact_kind_from_filename(filename)
    if kind is ArtifactKind.WHEEL:
        parsed = parse_wheel_file(filename)
        if parsed is not None:
            return WHEEL_RECORD, parsed.name, str(parsed.version)
    elif kind is ArtifactKind.SDIST:
        parsed_identity = project_version_from_filename(filename)
        if parsed_identity is not None:
            name, version = parsed_identity
            return SDIST_RECORD, name, str(version)
    return None


def link_record(link: Link) -> tuple[object, ...]:
    metadata = link.metadata_file
    upload_time = link.upload_time
    return (
        link.url,
        link.text,
        dict(link.hashes),
        link.requires_python,
        link.yanked_reason,
        None if metadata is None else dict(metadata.hashes or {}),
        None if upload_time is None else upload_time.isoformat(),
    )


def link_from_record(record: object, *, source_url: str | None = None) -> Link:
    if not isinstance(record, tuple) or len(record) != 7:
        raise ValueError("invalid catalog record")
    (
        url,
        text,
        hashes,
        requires_python,
        yanked,
        metadata,
        upload_time,
    ) = record
    if not isinstance(url, str) or not isinstance(text, str):
        raise ValueError("invalid catalog link")
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
    hashes_value = cast("dict[str, str]", hashes) if isinstance(hashes, dict) else None
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
        parsed_url=urllib.parse.urlsplit(url),
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
