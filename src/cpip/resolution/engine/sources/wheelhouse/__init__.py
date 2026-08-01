"""Wheelhouse candidate source and its catalog/metadata implementation."""

from __future__ import annotations

from typing import Any


class WheelhouseCandidateSource:
    """Catalog and metadata boundary consumed by the resolution engine."""

    def __init__(self, find_links: list[str], cache_dir: str | None = None) -> None:
        self.find_links = tuple(find_links)
        self.cache_dir = cache_dir
        self.catalog_path: str | None = None
        self.records: dict[str, Any] | None = None

    def load(self) -> dict[str, Any] | None:
        from cpip.resolution.engine.sources.wheelhouse.catalog import (
            load_catalog,
            save_catalog,
            scan_catalog,
        )

        catalog_path, records = load_catalog(self.cache_dir, list(self.find_links))
        self.catalog_path = catalog_path
        if records is None:
            records = scan_catalog(list(self.find_links))
            if records is None:
                return None
            save_catalog(catalog_path, list(self.find_links), records)
        self.records = records
        return records

    def indexes(self) -> Any:
        from cpip.resolution.engine.sources.wheelhouse.catalog import (
            build_catalog_indexes,
        )

        if self.records is None:
            self.load()
        return None if self.records is None else build_catalog_indexes(self.records)

    @staticmethod
    def load_candidate(*args: Any, **kwargs: Any) -> Any:
        from cpip.resolution.engine.sources.wheelhouse.metadata import load_candidate

        return load_candidate(*args, **kwargs)


__all__ = ["WheelhouseCandidateSource"]
