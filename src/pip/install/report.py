"""Generate installation reports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pip.core.packaging import canonicalize_name
from pip.core.urls import url_to_path
from pip.resolution.req_install import file_hashes


@dataclass(frozen=True)
class ReportItem:
    candidate_name: str
    candidate_version: str
    requested: bool
    source_url: str | None
    source_hashes: dict[str, str] | None
    yanked: bool
    is_direct: bool = False
    requested_extras: tuple[str, ...] = ()
    requires_dist: tuple[str, ...] = ()
    editable: bool = False


def write_install_report(path: Path, items: list[ReportItem]) -> None:
    install_entries: list[dict[str, object]] = []
    seen: set[tuple[str, str, bool]] = set()
    for item in sorted(items, key=lambda item: not item.requested):
        key = (
            canonicalize_name(item.candidate_name),
            item.candidate_version,
            item.requested,
        )
        if key in seen:
            continue
        seen.add(key)
        download_info: dict[str, object] = {
            "url": item.source_url or "",
        }
        if item.source_url and item.source_url.startswith("git+"):
            vcs_url, _, commit_id = item.source_url[4:].partition("@")
            download_info["url"] = vcs_url
            download_info["vcs_info"] = {
                "vcs": "git",
                "commit_id": commit_id,
            }
        if item.editable:
            download_info["dir_info"] = {"editable": True}
        hashes = dict(item.source_hashes or {})
        if not hashes and item.source_url and item.source_url.startswith("file://"):
            try:
                hashes = file_hashes(url_to_path(item.source_url))
            except OSError:
                hashes = {}
        if hashes:
            algorithm, digest = next(iter(sorted(hashes.items())))
            download_info["archive_info"] = {
                "hash": f"{algorithm}={digest}",
                "hashes": hashes,
            }
        metadata: dict[str, object] = {
            "name": item.candidate_name,
            "version": item.candidate_version,
        }
        if item.requires_dist:
            metadata["requires_dist"] = list(item.requires_dist)
        entry: dict[str, object] = {
            "metadata": metadata,
            "requested": item.requested,
            "is_direct": item.is_direct,
            "is_yanked": item.yanked,
            "download_info": download_info,
        }
        if item.requested_extras:
            entry["requested_extras"] = list(item.requested_extras)
        install_entries.append(entry)
    report = json.dumps({"version": "1", "install": install_entries})
    if os.fspath(path) == "-":
        print(report)
    else:
        path.write_text(report, encoding="utf-8")
