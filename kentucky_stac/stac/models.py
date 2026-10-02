"""STAC API data models (STAC API 1.0.0 / stac-fastapi responses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_NON_DATA_KEYS = {"thumbnail", "metadata", "xml"}
_NON_DATA_ROLES = {"thumbnail", "metadata"}
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


@dataclass
class Link:
    rel: str = ""
    href: str = ""
    type: Optional[str] = None
    title: Optional[str] = None
    method: Optional[str] = None
    body: Optional[Dict[str, Any]] = None
    merge: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Link":
        return cls(
            rel=d.get("rel") or "",
            href=d.get("href") or "",
            type=d.get("type"),
            title=d.get("title"),
            method=d.get("method"),
            body=d.get("body"),
            merge=bool(d.get("merge", False)),
        )


@dataclass
class Asset:
    href: str = ""
    type: Optional[str] = None
    title: Optional[str] = None
    roles: List[str] = field(default_factory=list)
    file_size: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Asset":
        return cls(
            href=d.get("href") or "",
            type=d.get("type"),
            title=d.get("title"),
            roles=list(d.get("roles") or []),
            file_size=d.get("file:size"),
        )

    @property
    def is_copc(self) -> bool:
        return self.href.lower().endswith(".copc.laz")

    @property
    def is_lidar(self) -> bool:
        return self.href.lower().endswith((".laz", ".las"))


@dataclass
class Item:
    id: str = ""
    collection: Optional[str] = None
    bbox: Optional[List[float]] = None
    geometry: Optional[Dict[str, Any]] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    assets: Dict[str, Asset] = field(default_factory=dict)
    links: List[Link] = field(default_factory=list)
    # Base URL of the STAC API this item came from -- set by the caller after a search, not part
    # of the STAC document. "" means unknown (treated as the built-in catalog).
    source: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Item":
        return cls(
            id=d.get("id") or "",
            collection=d.get("collection"),
            bbox=d.get("bbox"),
            geometry=d.get("geometry"),
            properties=dict(d.get("properties") or {}),
            assets={k: Asset.from_dict(v) for k, v in (d.get("assets") or {}).items() if v},
            links=[Link.from_dict(x) for x in d.get("links") or []],
        )

    @property
    def datetime(self) -> Optional[str]:
        return self.properties.get("datetime") or self.properties.get("start_datetime")

    @property
    def epsg(self) -> Optional[int]:
        return self.properties.get("proj:epsg")

    def data_asset_key(self) -> Optional[str]:
        """The key of the primary data asset (COG/LAZ): "data", else a "visual" composite, else a
        data-role asset, else the first asset that doesn't look like a thumbnail or metadata.

        "visual" comes before the data role because multi-band catalogs (e.g. Sentinel-2 on Earth
        Search) mark every single band -- aerosol, water vapour, red, nir... -- with the data role,
        so the first one in dict order is usually an unrenderable band rather than the true-color
        composite the catalog also offers."""
        if not self.assets:
            return None
        if "data" in self.assets:
            return "data"
        if "visual" in self.assets:
            return "visual"
        for key, a in self.assets.items():
            if "visual" in a.roles:
                return key
        for key, a in self.assets.items():
            if "data" in a.roles:
                return key
        for key, a in self.assets.items():
            if key.lower() in _NON_DATA_KEYS or _NON_DATA_ROLES.intersection(a.roles):
                continue
            return key
        return None

    def data_asset(self) -> Optional[Asset]:
        key = self.data_asset_key()
        return self.assets[key] if key is not None else None

    def thumbnail_asset(self) -> Optional[Asset]:
        if "thumbnail" in self.assets:
            return self.assets["thumbnail"]
        for a in self.assets.values():
            if {"thumbnail", "overview"}.intersection(a.roles):
                return a
        for a in self.assets.values():
            if a.href.lower().endswith(_IMAGE_SUFFIXES):
                return a
        return None

    def lidar_assets(self) -> List[Asset]:
        """Every LiDAR point-cloud asset (COPC or plain LAS/LAZ), identified by file extension
        rather than by asset key, since the catalog's key naming may change."""
        return [a for a in self.assets.values() if a.is_lidar]


@dataclass
class ItemCollection:
    features: List[Item] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    number_matched: Optional[int] = None
    number_returned: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ItemCollection":
        return cls(
            features=[Item.from_dict(f) for f in d.get("features") or []],
            links=[Link.from_dict(x) for x in d.get("links") or []],
            number_matched=d.get("numberMatched"),
            number_returned=d.get("numberReturned"),
        )

    @property
    def next_link(self) -> Optional[Link]:
        for link in self.links:
            if link.rel.lower() == "next":
                return link
        return None


@dataclass
class Collection:
    id: str = ""
    title: Optional[str] = None
    description: Optional[str] = None
    license: Optional[str] = None
    # [minx, miny, maxx, maxy] of the first (overall) spatial extent, if present
    bbox: Optional[List[float]] = None
    # [start, end] of the first temporal interval; either may be None (open-ended)
    interval: Optional[List[Optional[str]]] = None
    keywords: List[str] = field(default_factory=list)
    stac_extensions: List[str] = field(default_factory=list)
    item_asset_keys: List[str] = field(default_factory=list)
    # Base URL of the STAC API this collection came from -- set by the caller, not part of the
    # STAC document. "" means unknown (treated as the built-in catalog).
    source: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Collection":
        extent = d.get("extent") or {}
        bboxes = (extent.get("spatial") or {}).get("bbox") or []
        intervals = (extent.get("temporal") or {}).get("interval") or []
        return cls(
            id=d.get("id") or "",
            title=d.get("title"),
            description=d.get("description"),
            license=d.get("license"),
            bbox=bboxes[0] if bboxes else None,
            interval=intervals[0] if intervals else None,
            keywords=[str(k) for k in d.get("keywords") or [] if k],
            stac_extensions=[str(e) for e in d.get("stac_extensions") or [] if e],
            item_asset_keys=list((d.get("item_assets") or {}).keys()),
        )

    @property
    def title_or_id(self) -> str:
        return self.title.strip() if self.title and self.title.strip() else self.id
