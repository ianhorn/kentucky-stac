"""Knowledge about how the Kentucky From Above catalog is organised (pure Python)."""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .stac import Asset, Collection, Item

# The tiles checked so far are all NAD83 / Kentucky Single Zone (ftUS). Only assumed as a fallback
# for a file whose CRS QGIS can't read.
DEFAULT_CRS = "EPSG:3089"

# Where the catalog's thumbnail images live -- only used as a fallback (see thumbnail_href) for
# LiDAR items, whose own /search response never carries a thumbnail asset at all.
LIDAR_THUMBNAIL_BASE = "https://kyfromabove-stac.s3.us-west-2.amazonaws.com"


def is_lidar_collection(collection: Collection) -> bool:
    """The catalog names its point-cloud collections "laz-phaseN"."""
    return collection.id.lower().startswith("laz")


def split_collections(collections: Iterable[Collection]) -> Tuple[List[Collection], List[Collection]]:
    """Split into (imagery_and_dem, lidar), each sorted by id (so phases stay in order)."""
    ordered = sorted(collections, key=lambda c: c.id)
    lidar = [c for c in ordered if is_lidar_collection(c)]
    other = [c for c in ordered if not is_lidar_collection(c)]
    return other, lidar


def primary_asset(item: Item, lidar: bool) -> Optional[Asset]:
    """The asset a tile is searched/loaded/downloaded for: the point cloud (preferring COPC over
    plain LAZ/LAS) on the LiDAR tab, otherwise the item's data asset (a COG)."""
    if lidar:
        assets = item.lidar_assets()
        return next((a for a in assets if a.is_copc), assets[0] if assets else None)
    return item.data_asset()


def thumbnail_href(item: Item, lidar: bool) -> Optional[str]:
    """The item's thumbnail image URL, or None if it definitely has none.

    The STAC API's own thumbnail asset is reliable for imagery/DEM, but confirmed live: a LiDAR
    item's /search response omits the thumbnail asset entirely under every filtered query shape this
    plugin actually uses (bbox, intersects, ids) -- only a bare, unfiltered "collections+limit"
    listing (useless for a real AOI search) returns it. Since there's no way to get both a spatial
    filter and a thumbnail href in the same response, LiDAR falls back to reconstructing the URL from
    the item id/collection, the same convention the DEM collections' thumbnails already use for
    real ("collections/{collection}/thumbnails/{id}.png"). Phase 1 (plain LAZ, not COPC) genuinely
    has no thumbnail in the catalog at all (confirmed even via the bare/unfiltered query) -- the
    guessed URL for it simply 404s, and the caller (an async image fetch) just leaves that tile
    without a thumbnail, which is the correct, honest outcome rather than a bug to work around.
    """
    asset = item.thumbnail_asset()
    if asset is not None and asset.href:
        return asset.href
    if lidar and item.collection and item.id:
        return f"{LIDAR_THUMBNAIL_BASE}/collections/{item.collection}/thumbnails/{item.id}.png"
    return None


def format_size(size: Optional[int]) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""
