"""Build a Virtual Point Cloud (.vpc): a JSON index, modeled on STAC, that lets QGIS treat several
point cloud files (COPC or plain LAZ/LAS, and the two can be mixed) as one combined layer without
merging the data on disk. Pure Python -- no QGIS import; QGIS opens the result via its "vpc"
point cloud provider (`QgsPointCloudLayer(path, name, "vpc")`).

`pc:schemas` (the per-dimension attribute list -- X/Y/Z/Intensity/Classification/...) is required,
not optional decoration: found via real user testing that a VPC missing it still opens and reports
a correct point count, but QGIS has no idea what attributes exist to render by, so it silently
falls back to QgsPointCloudExtentRenderer (an outline of the extent, no actual points drawn) instead
of a real point renderer. Confirmed directly: adding pc:schemas changed the renderer QGIS picks from
QgsPointCloudExtentRenderer to QgsPointCloudClassifiedRenderer, and the attribute count from 3 to
the full 18 of a normally-opened tile.

Verified live (with pc:schemas included): opens with QGIS 3.44's vpc provider, reports the combined
point count, CRS, and full attribute set, and renders real points from every included tile (checked
with 3 COPC tiles, and again with COPC mixed with a plain LAZ tile).
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Tuple

from .stac import Item


def build_vpc(entries: Iterable[Tuple[Item, str]], vpc_path: str) -> int:
    """Write a .vpc file combining `entries` (a STAC item paired with its local file path).
    An item with no usable geometry/bbox is skipped. Returns how many tiles were written."""
    features = []
    for item, path in entries:
        if item.geometry is None or item.bbox is None:
            continue
        features.append(
            {
                "type": "Feature",
                "stac_version": "1.0.0",
                "id": item.id,
                "geometry": item.geometry,
                "bbox": list(item.bbox),
                "properties": {
                    "datetime": item.properties.get("datetime"),
                    "pc:count": item.properties.get("pc:count"),
                    "pc:type": item.properties.get("pc:type", "lidar"),
                    "pc:encoding": item.properties.get("pc:encoding"),
                    "pc:schemas": item.properties.get("pc:schemas"),
                    "proj:wkt2": item.properties.get("proj:wkt2"),
                    "proj:bbox": item.properties.get("proj:bbox"),
                },
                "assets": {"data": {"href": path.replace("\\", "/"), "roles": ["data"]}},
                "links": [],
            }
        )
    os.makedirs(os.path.dirname(vpc_path), exist_ok=True)
    with open(vpc_path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    return len(features)
