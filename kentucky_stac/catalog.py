"""Knowledge about how the Kentucky From Above catalog is organised (pure Python)."""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .stac import Collection


def is_lidar_collection(collection: Collection) -> bool:
    """The catalog names its point-cloud collections "laz-phaseN"."""
    return collection.id.lower().startswith("laz")


def split_collections(collections: Iterable[Collection]) -> Tuple[List[Collection], List[Collection]]:
    """Split into (imagery_and_dem, lidar), each sorted by id (so phases stay in order)."""
    ordered = sorted(collections, key=lambda c: c.id)
    lidar = [c for c in ordered if is_lidar_collection(c)]
    other = [c for c in ordered if not is_lidar_collection(c)]
    return other, lidar
