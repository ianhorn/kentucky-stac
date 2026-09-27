"""Knowledge about how the Kentucky From Above catalog is organised (pure Python)."""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .stac import Asset, Collection, Item


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


def format_size(size: Optional[int]) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""
