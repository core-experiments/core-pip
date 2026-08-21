"""Persistent cache for parsed Simple API catalog entries."""

from __future__ import annotations

import datetime
import hashlib
import marshal
import posixpath
import urllib.parse

from cpip.core.utils import CACHE_VERSION, CACHE_VERSION_TAG
from cpip.core.versions import (
    InvalidVersion,
    VERSION_WIRE_FORMAT,
    Version,
    is_version_wire,
)
from cpip.core.wheel import WheelFile, WheelTag, parse_wheel_file
from cpip.index.datetime import parse_iso_datetime
from cpip.index.directory_index import project_version_from_filename
from cpip.index.links import Link
from cpip.index.source_models import ArtifactKind, MetadataFile

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

# Every key prefix and header carries the cache-wide version, so an entry
# written under another version is never read (and never migrated).
VERSION = CACHE_VERSION
PREFIX = f"cpip-index-catalog-{CACHE_VERSION_TAG}:"
SUMMARY_PREFIX = f"cpip-index-summary-{CACHE_VERSION_TAG}:"
CHOICE_PREFIX = f"cpip-index-choice-{CACHE_VERSION_TAG}:"
SUMMARY_HEADER = f"cpip-index-summary-{CACHE_VERSION_TAG}\0".encode("ascii")
CHOICE_HEADER = f"cpip-index-choice-{CACHE_VERSION_TAG}\0".encode("ascii")

WHEEL_RECORD = 1
SDIST_RECORD = 2
RECORD_REQUIRES_PYTHON = 3
RECORD_YANKED = 4
RECORD_WHEEL_IDENTITY = 7
WHEEL_IDENTITY_NAME = 0
WHEEL_IDENTITY_VERSION = 1
WHEEL_IDENTITY_BUILD_TAG = 2
WHEEL_IDENTITY_TAGS = 3

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

_PENDING_CATALOGS_ATTRIBUTE = "_cpip_pending_catalogs"
_PENDING_CATALOGS_LIMIT = 64


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
    return f"{CHOICE_PREFIX}{target_key}:{int(allow_binary)}:{int(allow_source)}:{url}"


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
    pending = _pending_catalogs(cache)
    if pending is not None:
        catalog = pending.pop(url, None)
        if catalog is not None:
            return catalog
    loaded = _load_catalog_uncached(cache, url)
    return None if loaded is None else loaded[0]


def _pending_catalogs(cache: Any) -> dict[str, CatalogData] | None:
    """Return the per-cache handoff used between summary and catalog loads."""
    try:
        attributes = vars(cache)
    except TypeError:
        return None
    pending = attributes.get(_PENDING_CATALOGS_ATTRIBUTE)
    if pending is None:
        pending = {}
        attributes[_PENDING_CATALOGS_ATTRIBUTE] = pending
    return pending


def _remember_pending_catalog(
    cache: Any,
    url: str,
    catalog: CatalogData,
) -> None:
    pending = _pending_catalogs(cache)
    if pending is None:
        return
    pending[url] = catalog
    while len(pending) > _PENDING_CATALOGS_LIMIT:
        pending.pop(next(iter(pending)))


def _load_catalog_uncached(
    cache: Any,
    url: str,
) -> tuple[CatalogData, bytes | None] | None:
    """Load compact records grouped by their target-independent release."""
    if cache is None:
        return None
    raw = get_cache_entry(cache, cache_key(url))
    if raw is None:
        return None
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
        return (groups, unparsed), raw
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
            return summary
    # No summary under the current key (a first run, or one written by
    # another cpip version under its own key): compile it from the catalog,
    # which is text-only and stable.
    pending = _pending_catalogs(cache)
    catalog = pending.pop(url, None) if pending is not None else None
    catalog_raw: bytes | None = None
    if catalog is None:
        loaded = _load_catalog_uncached(cache, url)
        if loaded is not None:
            catalog, catalog_raw = loaded
    if catalog is None:
        return None
    _remember_pending_catalog(cache, url, catalog)
    if catalog_raw is None:
        catalog_raw = get_cache_entry(cache, cache_key(url))
    if catalog_raw is None:
        return None
    generation = catalog_generation(catalog_raw)
    save_summary(cache, url, catalog, generation)
    return summary_from_catalog(catalog, generation)


def decode_summary(raw: bytes) -> CatalogSummary | None:
    if not raw.startswith(SUMMARY_HEADER):
        return None
    payload = decode_checked_payload(raw, SUMMARY_HEADER)
    if (
        not isinstance(payload, tuple)
        or len(payload) != 5
        or payload[4] != VERSION_WIRE_FORMAT
        or not isinstance(payload[0], str)
        or not isinstance(payload[1], list)
        or not isinstance(payload[2], bool)
        or not valid_choice_profiles(payload[3])
        or not all(valid_summary_group(group) for group in payload[1])
    ):
        return None
    return payload[0], payload[1], payload[2], payload[3]  # ty:ignore[invalid-return-type]


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
    if raw is None or not raw.startswith(CHOICE_HEADER):
        return {}
    payload = decode_checked_payload(raw, CHOICE_HEADER)
    if (
        not isinstance(payload, tuple)
        or len(payload) != 2
        or payload[0] != generation
        or not valid_choices(payload[1])
    ):
        return {}
    choices = payload[1]
    embed_summary_choices(
        cache,
        url,
        generation,
        target_key,
        allow_binary,
        allow_source,
        choices,  # ty:ignore[invalid-argument-type]
    )
    return choices  # ty:ignore[invalid-return-type]


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


def valid_version_text(value: object) -> bool:
    """A version string the summary can compile: a corrupt one is a miss,
    not an exception out of load_summary. Version interns by text, so the
    parse here is the one the summary needs anyway."""
    if not isinstance(value, str):
        return False
    try:
        Version(value)
    except InvalidVersion:
        return False
    return True


def valid_group(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 4
        and isinstance(value[0], str)
        and valid_version_text(value[1])
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
        and is_version_wire(value[2])
        and valid_version_text(value[2][0])  # ty:ignore[not-subscriptable]
        and isinstance(value[3], list)
        and all(valid_fact(fact) for fact in value[3])
    )


def valid_record(value: object) -> bool:
    if not isinstance(value, tuple) or len(value) != 8:
        return False
    identity = value[RECORD_WHEEL_IDENTITY]
    if identity is None:
        return True
    if not isinstance(identity, tuple) or len(identity) != 4:
        return False
    tags = identity[WHEEL_IDENTITY_TAGS]
    if not isinstance(tags, tuple):
        return False
    return (
        isinstance(identity[WHEEL_IDENTITY_NAME], str)
        and isinstance(identity[WHEEL_IDENTITY_VERSION], str)
        and (
            identity[WHEEL_IDENTITY_BUILD_TAG] is None
            or isinstance(identity[WHEEL_IDENTITY_BUILD_TAG], str)
        )
        and all(
            isinstance(tag, tuple)
            and len(tag) == 3
            and all(isinstance(part, str) for part in tag)
            for tag in tags
        )
    )


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


def valid_choices(value: object) -> bool:
    """A version-text -> choice map of the exact shape the provider unpacks;
    anything else is a miss rather than a ValueError deep in resolution."""
    return isinstance(value, dict) and all(
        isinstance(version, str) and valid_choice(choice)
        for version, choice in value.items()
    )


def valid_choice_profiles(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(profile, tuple)
        and len(profile) == 3
        and isinstance(profile[0], str)
        and isinstance(profile[1], bool)
        and isinstance(profile[2], bool)
        and valid_choices(choices)
        for profile, choices in value.items()
    )


def save_links(cache: Any, url: str, links: list[Link]) -> None:
    if cache is None:
        return
    grouped: dict[tuple[str, str], list[CatalogArtifact]] = {}
    unparsed: list[CatalogRecord] = []
    for link in links:
        parsed_wheel = parsed_wheel_from_link(link)
        record = link_record(link, parsed_wheel=parsed_wheel)
        identity = artifact_identity(link, parsed_wheel=parsed_wheel)
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
            Version(version).to_wire(),
            facts,
        )
        for name, version, _artifacts, facts in groups
    ]
    summary_groups.sort(key=summary_group_sort_key)
    return generation, summary_groups, bool(unparsed), {}  # ty:ignore[invalid-return-type]


def summary_group_sort_key(group: CatalogSummaryGroup) -> Any:
    return group[2][2]


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
            (*summary, VERSION_WIRE_FORMAT),
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


def artifact_identity(
    link: Link,
    *,
    parsed_wheel: WheelFile | None = None,
) -> tuple[int, str, str] | None:
    """Compile the artifact identity once when its Simple API page changes."""
    if parsed_wheel is None:
        parsed_wheel = parsed_wheel_from_link(link)
    if parsed_wheel is not None:
        return WHEEL_RECORD, parsed_wheel.name, str(parsed_wheel.version)
    if link.kind is not ArtifactKind.SDIST:
        return None
    filename = posixpath.basename(
        urllib.parse.unquote(link.parsed_url_internal.path).rstrip("/"),
    )
    parsed_identity = project_version_from_filename(filename)
    if parsed_identity is None:
        return None
    name, version = parsed_identity
    return SDIST_RECORD, name, str(version)


def parsed_wheel_from_link(link: Link) -> WheelFile | None:
    """Parse a wheel link's filename exactly once at catalog build time."""
    if link.kind is not ArtifactKind.WHEEL:
        return None
    filename = posixpath.basename(
        urllib.parse.unquote(link.parsed_url_internal.path).rstrip("/"),
    )
    return parse_wheel_file(filename)


def wheel_identity(parsed_wheel: WheelFile | None) -> tuple[object, ...] | None:
    """Marshal-safe parsed identity embedded in a wheel catalog record."""
    if parsed_wheel is None:
        return None
    return (
        parsed_wheel.name,
        str(parsed_wheel.version),
        parsed_wheel.build_tag,
        tuple((tag.interpreter, tag.abi, tag.platform) for tag in parsed_wheel.tags),
    )


def wheel_file_from_identity(
    identity: object,
    *,
    name: str,
    version: Version,
) -> WheelFile | None:
    """Reconstruct a wheel from its cached identity without reparsing a name."""
    if not isinstance(identity, tuple) or len(identity) != 4:
        return None
    identity_name = identity[WHEEL_IDENTITY_NAME]
    version_text = identity[WHEEL_IDENTITY_VERSION]
    build_tag = identity[WHEEL_IDENTITY_BUILD_TAG]
    tag_triples = identity[WHEEL_IDENTITY_TAGS]
    if (
        not isinstance(identity_name, str)
        or not isinstance(version_text, str)
        or (build_tag is not None and not isinstance(build_tag, str))
        or not isinstance(tag_triples, tuple)
    ):
        return None
    if identity_name != name or version_text != str(version):
        return None
    tags: list[WheelTag] = []
    for tag in tag_triples:
        if not isinstance(tag, tuple) or len(tag) != 3:
            continue
        interpreter, abi, platform = tag
        if (
            not isinstance(interpreter, str)
            or not isinstance(abi, str)
            or not isinstance(platform, str)
        ):
            continue
        tags.append(WheelTag(interpreter, abi, platform))
    if not tags:
        return None
    return WheelFile(
        name=identity_name,
        version=version,
        build_tag=build_tag,
        tags=tuple(tags),
    )


def wheel_file_from_record(
    record: tuple[object, ...],
    *,
    name: str,
    version: Version,
) -> WheelFile | None:
    """Reconstruct a wheel from its catalog record's cached identity."""
    return wheel_file_from_identity(
        record[RECORD_WHEEL_IDENTITY],
        name=name,
        version=version,
    )


def link_record(
    link: Link,
    *,
    parsed_wheel: WheelFile | None = None,
) -> tuple[object, ...]:
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
        wheel_identity(parsed_wheel),
    )


def link_from_record(record: object, *, source_url: str | None = None) -> Link:
    if not isinstance(record, tuple) or len(record) != 8:
        raise ValueError("invalid catalog record")
    (
        url,
        text,
        hashes,
        requires_python,
        yanked,
        metadata,
        upload_time,
        _wheel_identity,
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
    hashes_value = hashes if isinstance(hashes, dict) else None
    metadata_value = metadata if isinstance(metadata, dict) else None
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
        hashes=hashes_value or {},  # ty:ignore[invalid-argument-type]
        requires_python=requires_python if isinstance(requires_python, str) else None,
        yanked_reason=yanked if isinstance(yanked, str) else None,
        metadata_file=(
            MetadataFile(metadata_value) if metadata_value is not None else None  # ty:ignore[invalid-argument-type]
        ),
        upload_time=parsed_upload_time,
    )
