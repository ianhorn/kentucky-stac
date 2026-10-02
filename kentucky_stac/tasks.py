"""Background tasks, so network calls never block the QGIS UI."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable, List, Optional, Sequence, Tuple

from qgis.core import QgsTask

from .qgis_transport import qgis_transport
from .sources import STAC_INDEX_URL, ApiSource, CatalogEntry, parse_stac_index
from .stac import Collection, Item, SearchQuery, StacClient


class CollectionsTask(QgsTask):
    """Fetch every source's collections, each independently: one unreachable "bring your own" API
    doesn't stop the built-in catalog (or any other source) from loading. Each collection is tagged
    with its source's base URL. `callback(collections, errors)` runs on the main thread unless the
    task was cancelled; errors is a list of (source name, message)."""

    def __init__(self, sources: Sequence[ApiSource], callback: Callable[[List[Collection], List[Tuple[str, str]]], None]):
        super().__init__("Loading Kentucky STAC collections")
        self._sources = list(sources)
        self._callback = callback
        self.collections: List[Collection] = []
        self.errors: List[Tuple[str, str]] = []

    def run(self) -> bool:
        # Worker thread: no GUI access here.
        for source in self._sources:
            if self.isCanceled():
                return False
            try:
                found = StacClient(source.base_uri, qgis_transport).collections()
                self.collections.extend(replace(c, source=source.base_uri) for c in found)
            except Exception as e:  # report any failure to the UI instead of losing it in the thread
                self.errors.append((source.name, str(e)))
        return True

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        self._callback(self.collections, self.errors)


MAX_RESULTS = 2000
PAGE_SIZE = 500


class SearchTask(QgsTask):
    """Run a STAC search against one or more sources, following pagination up to MAX_RESULTS items
    per source, and merge the results (each item tagged with its source's base URL).

    `groups` is a list of (source base URL, collection ids to search there); `query` supplies the
    rest (geometry, limit). A source that fails is reported in `errors` rather than failing the
    whole search, unless every source failed.
    `callback(items, matched, truncated, error)` runs on the main thread unless cancelled."""

    def __init__(self, groups: Sequence[Tuple[str, List[str]]], query: SearchQuery, callback, source_names=None):
        super().__init__("Searching Kentucky STAC")
        self._groups = list(groups)
        self._query = query
        self._callback = callback
        self._names = source_names or {}
        self.items: List[Item] = []
        self.matched: Optional[int] = None
        self.truncated = False
        self.errors: List[Tuple[str, str]] = []

    def run(self) -> bool:
        # Worker thread: no GUI access here.
        total_matched = 0
        any_matched = False
        for base_uri, collection_ids in self._groups:
            if self.isCanceled():
                return False
            try:
                items, matched, truncated = self._search_one(base_uri, collection_ids)
            except Exception as e:
                self.errors.append((self._names.get(base_uri, base_uri), str(e)))
                continue
            if self.isCanceled():
                return False
            self.items.extend(replace(i, source=base_uri) for i in items)
            self.truncated = self.truncated or truncated
            if matched is not None:
                total_matched += matched
                any_matched = True
        self.matched = total_matched if any_matched else None
        return True

    def _search_one(self, base_uri: str, collection_ids: List[str]) -> Tuple[List[Item], Optional[int], bool]:
        query = replace(self._query, collections=list(collection_ids))
        client = StacClient(base_uri, qgis_transport)
        page = client.search(query)
        matched = page.number_matched
        items = list(page.features)
        truncated = False
        # Stop on an empty page or once numberMatched is reached, too: some servers (stac-server)
        # still offer a next link on the last page.
        while page.next_link is not None and page.features:
            if self.isCanceled() or (matched is not None and len(items) >= matched):
                break
            if len(items) >= MAX_RESULTS:
                truncated = True
                break
            page = client.next_page(page, query)
            if page is None:
                break
            items.extend(page.features)
        return items, matched, truncated

    @property
    def error(self) -> Optional[str]:
        """A fatal error: only when every source failed. Partial failures are in `errors`."""
        if self.errors and len(self.errors) == len(self._groups):
            return "; ".join(f"{name}: {msg}" for name, msg in self.errors)
        return None

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        self._callback(self.items, self.matched, self.truncated, self.error)


class StacIndexTask(QgsTask):
    """Fetch the list of public, searchable STAC APIs from STAC Index.
    `callback(entries, error)` runs on the main thread unless cancelled."""

    def __init__(self, callback: Callable[[List[CatalogEntry], Optional[str]], None]):
        super().__init__("Loading STAC Index catalogs")
        self._callback = callback
        self.entries: List[CatalogEntry] = []
        self.error: Optional[str] = None

    def run(self) -> bool:
        try:
            raw = qgis_transport("GET", STAC_INDEX_URL, None, {"Accept": "application/json"})
            self.entries = parse_stac_index(json.loads(raw))
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        self._callback(self.entries, self.error)
