"""Which STAC APIs the dock searches: the built-in KyFromAbove catalog, plus any "bring your own"
API the user adds -- typed in by hand, or picked from STAC Index (https://stacindex.org).

Ported from the ArcGIS Pro add-in (kyfromabove-ext: StacApiSource.cs, StacIndexClient.cs,
AddApiSourceDialog). Pure Python -- no QGIS import -- so the parsing/filtering is unit-testable;
fetching and persistence (QgsSettings) live in the QGIS-side callers.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, Iterable, List, Optional

from .stac import DEFAULT_BASE_URI

STAC_INDEX_URL = "https://stacindex.org/api/catalogs"
# KyFromAbove's own titiler -- the "revert to the original" choice in the tile server dialog.
ORIGINAL_TITILER_URL = "https://6hp4guqpwe.execute-api.us-west-2.amazonaws.com/"
BUILTIN_NAME = "KyFromAbove"
_SUMMARY_MAX = 240


@dataclass(frozen=True)
class ApiSource:
    name: str
    base_uri: str
    is_default: bool = False
    # Optional titiler-pgstac server backed by this API's catalog, for "Add as server mosaic".
    # The built-in source ignores it (it has its own tile server, see server_layers.tiler_url).
    tiler_url: str = ""


DEFAULT_SOURCE = ApiSource(BUILTIN_NAME, DEFAULT_BASE_URI, is_default=True)


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def is_default_uri(base_uri: str) -> bool:
    """True for the built-in catalog -- also for "" (an item/collection whose source was never
    recorded), so Kentucky-only behavior keeps working for anything predating multi-source."""
    uri = normalize_url(base_uri)
    return not uri or uri.lower() == normalize_url(DEFAULT_BASE_URI).lower()


def is_valid_api_url(url: str) -> bool:
    """A plausible http(s) URL -- the check for a URL typed in by hand."""
    parsed = urllib.parse.urlparse(normalize_url(url))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_usable_base_url(url: str) -> bool:
    """Whether a STAC Index entry's URL can be a search root: this plugin talks to {base}/collections
    and {base}/search, so static catalogs mislabeled as APIs (".json"), openEO endpoints (a different
    API) and URLs carrying a query string or API key are dropped -- same rules as the add-in."""
    if not is_valid_api_url(url):
        return False
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.query:
        return False
    if parsed.path.lower().endswith(".json"):
        return False
    if "openeo" in url.lower():
        return False
    return True


@dataclass(frozen=True)
class CatalogEntry:
    """One pickable STAC API in the "add a source" dialog."""

    title: str
    name: str  # what the dialog's Name box is filled with
    url: str
    detail: str
    is_builtin: bool = False


BUILTIN_ENTRY = CatalogEntry(
    "KyFromAbove (Kentucky's default catalog)",
    BUILTIN_NAME,
    DEFAULT_BASE_URI,
    f"{DEFAULT_BASE_URI}\n\nKentucky Aerial Photography and Elevation Data (KYAPED), 2010 - present: "
    "leaf-off orthoimagery, LiDAR point clouds, and LiDAR-derived DEMs.",
    is_builtin=True,
)


def parse_stac_index(data: Any) -> List[CatalogEntry]:
    """Public, searchable STAC APIs from a STAC Index /api/catalogs response, sorted by title, with
    the built-in KyFromAbove entry always first (so it's the way back after switching APIs, even if
    STAC Index lists nothing usable). Private/protected catalogs are left out: there's no way to
    supply credentials."""
    entries: List[CatalogEntry] = []
    for el in data if isinstance(data, list) else []:
        if not isinstance(el, dict):
            continue
        if el.get("isApi") is not True or el.get("isPrivate") is True or el.get("access") != "public":
            continue
        title = (el.get("title") or "").strip()
        url = (el.get("url") or "").strip()
        if not title or not is_usable_base_url(url):
            continue
        base = normalize_url(url)
        if is_default_uri(base):
            continue  # BUILTIN_ENTRY covers it
        summary = (el.get("summary") or "").strip()
        if len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX].rstrip() + "..."
        detail = f"{url}\n\n{summary}" if summary else url
        entries.append(CatalogEntry(title, title, base, detail))
    entries.sort(key=lambda e: e.title.casefold())
    return [BUILTIN_ENTRY] + entries


def serialize_sources(sources: Iterable[ApiSource]) -> str:
    return json.dumps([{"name": s.name, "base_uri": s.base_uri, "tiler_url": s.tiler_url} for s in sources])


def deserialize_sources(text: Optional[str]) -> List[ApiSource]:
    """The saved source list, or just the built-in catalog if nothing (valid) was saved. The
    built-in entry is recognized by URL rather than trusted from the saved data, so it can't be
    duplicated or lose its is_default flag."""
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        raw = None
    sources: List[ApiSource] = []
    seen = set()
    for el in raw if isinstance(raw, list) else []:
        if not isinstance(el, dict):
            continue
        uri = normalize_url(str(el.get("base_uri") or ""))
        if not is_valid_api_url(uri) or uri.lower() in seen:
            continue
        seen.add(uri.lower())
        if is_default_uri(uri):
            sources.append(DEFAULT_SOURCE)
        else:
            name = str(el.get("name") or "").strip() or uri
            tiler = normalize_url(str(el.get("tiler_url") or ""))
            sources.append(ApiSource(name, uri, tiler_url=tiler if is_valid_api_url(tiler) else ""))
    return sources or [DEFAULT_SOURCE]


def add_source(sources: List[ApiSource], name: str, url: str, replace: bool, tiler_url: str = "") -> List[ApiSource]:
    """The source list after adding (or, with replace, switching to) the API at url. Picking the
    built-in catalog's URL yields DEFAULT_SOURCE itself, not a user-named copy."""
    uri = normalize_url(url)
    tiler = normalize_url(tiler_url)
    new = (
        DEFAULT_SOURCE
        if is_default_uri(uri)
        else ApiSource((name or "").strip() or uri, uri, tiler_url=tiler if is_valid_api_url(tiler) else "")
    )
    if replace:
        return [new]
    if any(normalize_url(s.base_uri).lower() == uri.lower() for s in sources):
        return list(sources)
    return list(sources) + [new]


def source_name(sources: Iterable[ApiSource], base_uri: str) -> str:
    uri = normalize_url(base_uri).lower()
    for s in sources:
        if normalize_url(s.base_uri).lower() == uri:
            return s.name
    return BUILTIN_NAME if is_default_uri(base_uri) else base_uri


def with_tiler_url(sources: Iterable[ApiSource], base_uri: str, tiler_url: str) -> List[ApiSource]:
    """The source list with one source's tile server URL set (or cleared, with ""). The built-in
    source is never changed."""
    uri = normalize_url(base_uri).lower()
    tiler = normalize_url(tiler_url)
    return [
        replace(s, tiler_url=tiler) if not s.is_default and normalize_url(s.base_uri).lower() == uri else s
        for s in sources
    ]


def tiler_for(sources: Iterable[ApiSource], base_uri: str) -> str:
    """The tile server URL configured for a source, or "" if none."""
    uri = normalize_url(base_uri).lower()
    for s in sources:
        if not s.is_default and normalize_url(s.base_uri).lower() == uri:
            return s.tiler_url
    return ""
