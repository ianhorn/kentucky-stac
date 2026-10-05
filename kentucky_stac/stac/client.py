"""STAC API client for the Kentucky From Above catalog (or any STAC API).

Endpoints used: GET /collections, GET|POST /search, GET /collections/{id}/items/{id}. Pagination
follows the "next" link, including POST-style links that carry a body.

HTTP goes through a pluggable transport so the client has no hard dependency on any network stack.
The default uses urllib; the plugin can substitute one built on QgsNetworkAccessManager so QGIS's
proxy and CA settings apply.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

from .models import Collection, Item, ItemCollection, Link

DEFAULT_BASE_URI = "https://spved5ihrl.execute-api.us-west-2.amazonaws.com"
USER_AGENT = "KentuckyStac/0.1"
MAX_LIMIT = 10000

# transport(method, url, body, headers) -> response bytes. Raises StacError on failure.
Transport = Callable[[str, str, Optional[bytes], Dict[str, str]], bytes]

DateLike = Union[str, datetime, None]


class StacError(Exception):
    """A failed STAC request. Includes the response body for 4xx/5xx so errors are readable."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def urllib_transport(
    method: str, url: str, body: Optional[bytes], headers: Dict[str, str], timeout: float = 120
) -> bytes:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise StacError(f"Not an http(s) URL: {url}")  # never file:// or another scheme
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- scheme checked above
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise StacError(f"STAC API {e.code} {e.reason}: {detail}", status=e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise StacError(f"STAC API request failed: {e}") from e


@dataclass
class SearchQuery:
    collections: List[str] = field(default_factory=list)
    # [minx, miny, maxx, maxy] in lon/lat (CRS84)
    bbox: Optional[Sequence[float]] = None
    # GeoJSON geometry (dict, lon/lat CRS84). Takes precedence over bbox and forces POST.
    intersects: Optional[Dict[str, Any]] = None
    start: DateLike = None
    end: DateLike = None
    ids: List[str] = field(default_factory=list)
    text: Optional[str] = None
    limit: int = 50
    # Keep tiles with at most this much cloud cover (percent). Tiles that don't report eo:cloud_cover
    # (SAR, elevation, aerial imagery...) are kept too when cloud_mode is "cql2", but dropped by "query"
    # (the older query extension has no way to say "or missing").
    max_cloud_cover: Optional[float] = None
    cloud_mode: str = "cql2"  # how the cloud filter is sent: "cql2" (filter extension) or "query"
    sortby: Optional[str] = None  # "desc" / "asc" on the item's datetime; None = the server's order


def format_datetime_range(start: DateLike, end: DateLike) -> Optional[str]:
    """STAC datetime interval: "start/end", with ".." for an open end. Strings pass through."""
    if start is None and end is None:
        return None

    def fmt(v: DateLike) -> str:
        if v is None:
            return ".."
        if isinstance(v, str):
            return v
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc)
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    return f"{fmt(start)}/{fmt(end)}"


def query_variants(query: SearchQuery) -> List[SearchQuery]:
    """The query as asked, then simpler forms to retry with against a server that rejects it: a cloud
    filter goes CQL2 -> the older "query" extension (or the reverse), and the sort is dropped (the
    caller sorts the results itself)."""
    if query.max_cloud_cover is None:
        modes = [query.cloud_mode]
    else:
        modes = [query.cloud_mode, "query" if query.cloud_mode == "cql2" else "cql2"]
    sorts = [query.sortby, None] if query.sortby else [None]
    return [replace(query, cloud_mode=mode, sortby=sort) for mode in modes for sort in sorts]


def within_cloud(item: Item, max_cloud_cover: Optional[float]) -> bool:
    """False only for a tile that reports more cloud cover than allowed -- a tile that reports none
    (radar, elevation, aerial imagery) always passes. This is checked on the results themselves because
    some servers accept a cloud filter and silently ignore it (Earth Search does that with CQL2)."""
    if max_cloud_cover is None:
        return True
    value = item.properties.get("eo:cloud_cover")
    return not isinstance(value, (int, float)) or value <= max_cloud_cover


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


class StacClient:
    def __init__(self, base_uri: str = DEFAULT_BASE_URI, transport: Optional[Transport] = None):
        self.base_uri = base_uri
        self._transport = transport or urllib_transport

    @property
    def _root(self) -> str:
        return self.base_uri.rstrip("/")

    def _request(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            # Plain application/json, as the STAC API spec uses for POST /search: stac-server (e.g.
            # Earth Search) silently ignores a geo+json body and returns its whole catalog unfiltered.
            headers["Content-Type"] = "application/json"
        raw = self._transport(method, url, data, headers)
        try:
            return json.loads(raw)
        except ValueError as e:
            raise StacError(f"STAC API returned invalid JSON from {url}") from e

    def collections(self, max_pages: int = 20) -> List[Collection]:
        """Every collection, following rel="next" links -- KyFromAbove returns them all at once,
        but some STAC APIs paginate /collections. max_pages guards against a server whose next
        link never runs out."""
        url: Optional[str] = f"{self._root}/collections"
        found: List[Collection] = []
        seen = set()
        for _ in range(max_pages):
            if not url or url in seen:
                break
            seen.add(url)
            data = self._request("GET", url)
            found.extend(Collection.from_dict(c) for c in data.get("collections") or [])
            url = next((link.get("href") for link in data.get("links") or [] if link.get("rel") == "next"), None)
        return found

    def get_item(self, collection_id: str, item_id: str) -> Item:
        q = urllib.parse.quote
        url = f"{self._root}/collections/{q(collection_id, safe='')}/items/{q(item_id, safe='')}"
        return Item.from_dict(self._request("GET", url))

    def search(self, query: SearchQuery) -> ItemCollection:
        """One page of results. Uses POST /search with a JSON body when an intersects geometry is
        set, otherwise GET /search with query parameters."""
        # Cloud and sort filters only exist in the POST body.
        if query.intersects or query.max_cloud_cover is not None or query.sortby:
            body = self._search_body(query)
            return ItemCollection.from_dict(self._request("POST", f"{self._root}/search", body))
        return ItemCollection.from_dict(self._request("GET", self._search_url(query)))

    def next_page(self, page: ItemCollection, original_query: Optional[SearchQuery] = None) -> Optional[ItemCollection]:
        """Follow a page's "next" link, or return None when there isn't one. POST links carry a
        body (merged into the original search body when the link says so)."""
        link = page.next_link
        if link is None or not link.href:
            return None
        return ItemCollection.from_dict(self._follow(link, original_query))

    def iter_items(self, query: SearchQuery, max_items: Optional[int] = None) -> Iterator[Item]:
        """Yield items across all pages, stopping after max_items if given."""
        count = 0
        page: Optional[ItemCollection] = self.search(query)
        while page is not None:
            for item in page.features:
                yield item
                count += 1
                if max_items is not None and count >= max_items:
                    return
            page = self.next_page(page, query)

    def _follow(self, link: Link, original_query: Optional[SearchQuery]) -> Dict[str, Any]:
        method = (link.method or "GET").upper()
        if method == "POST":
            body = link.body or {}
            if link.merge and original_query is not None:
                body = {**self._search_body(original_query), **body}
            return self._request("POST", link.href, body)
        return self._request("GET", link.href)

    @staticmethod
    def _search_body(q: SearchQuery) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if q.collections:
            body["collections"] = list(q.collections)
        if q.intersects:
            body["intersects"] = q.intersects
        elif q.bbox:
            body["bbox"] = list(q.bbox)
        if q.ids:
            body["ids"] = list(q.ids)
        dt = format_datetime_range(q.start, q.end)
        if dt:
            body["datetime"] = dt
        if q.text and q.text.strip():
            body["q"] = [q.text]
        if q.max_cloud_cover is not None:
            cloud = {"property": "eo:cloud_cover"}
            if q.cloud_mode == "query":
                body["query"] = {"eo:cloud_cover": {"lte": q.max_cloud_cover}}
            else:
                body["filter-lang"] = "cql2-json"
                body["filter"] = {
                    "op": "or",
                    "args": [
                        {"op": "<=", "args": [cloud, q.max_cloud_cover]},
                        {"op": "isNull", "args": [cloud]},
                    ],
                }
        if q.sortby in ("asc", "desc"):
            body["sortby"] = [{"field": "properties.datetime", "direction": q.sortby}]
        body["limit"] = _clamp_limit(q.limit)
        return body

    def _search_url(self, q: SearchQuery) -> str:
        params: List[str] = []
        if q.collections:
            params.append("collections=" + ",".join(q.collections))
        if q.ids:
            params.append("ids=" + ",".join(q.ids))
        if q.bbox and len(q.bbox) >= 4:
            params.append("bbox=" + ",".join(repr(float(v)) for v in q.bbox))
        dt = format_datetime_range(q.start, q.end)
        if dt:
            params.append("datetime=" + urllib.parse.quote(dt, safe=""))
        if q.text and q.text.strip():
            params.append("q=" + urllib.parse.quote(q.text, safe=""))
        params.append(f"limit={_clamp_limit(q.limit)}")
        return f"{self._root}/search?" + "&".join(params)
