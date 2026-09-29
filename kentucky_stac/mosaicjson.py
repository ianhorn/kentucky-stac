"""Export selected imagery/DEM tiles as a MosaicJSON file (https://github.com/developmentseed/mosaicjson-spec)
-- a portable index that titiler/rio-tiler/cogeo-mosaic can read directly, with no GDAL VRT build and
no server round-trip. Pure metadata: every field comes from the STAC items' own properties (bbox,
proj:bbox/proj:shape/proj:epsg) -- no network access or file opens needed, unlike the VRT mosaic
(mosaic.py), which has to open each remote COG's header via GDAL to build it.

Ground sample distance needs a units-of-CRS-to-meters conversion, which needs QGIS's CRS database --
kept out of this module (pure Python, unit-testable without a QgsApplication, matching catalog.py's
own convention) by taking it as an injected `gsd_meters_for` callback instead of importing qgis.core
directly. `quadkey_zoom` is deliberately omitted from the written document: the spec defaults it to
`minzoom` when absent, which is exactly what this module computes tiles at.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .catalog import primary_asset
from .stac import Item

MOSAICJSON_VERSION = "0.0.2"
Bbox = Tuple[float, float, float, float]
_EARTH_CIRCUMFERENCE_M = 2 * math.pi * 6378137.0


def _union_bbox(boxes: List[Bbox]) -> Optional[Bbox]:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _zoom_for_resolution(gsd_m: float, latitude_deg: float) -> int:
    """The zoom level whose Web Mercator pixel size best matches a ground resolution of gsd_m
    meters at the given latitude (Web Mercator scale shrinks by cos(latitude) moving away from the
    equator)."""
    meters_per_pixel_at_zoom0 = (_EARTH_CIRCUMFERENCE_M / 256.0) * math.cos(math.radians(latitude_deg))
    zoom = math.log2(meters_per_pixel_at_zoom0 / gsd_m)
    return max(0, min(24, round(zoom)))


def _zoom_for_extent(bounds: Bbox) -> int:
    """The largest zoom at which the whole bounds still fits inside a single tile."""
    minx, miny, maxx, maxy = bounds
    span = max(maxx - minx, maxy - miny, 1e-9)
    return max(0, min(24, math.floor(math.log2(360.0 / span))))


def _tile_xy(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    """Standard Web Mercator XYZ tile indices -- the same scheme quadkeys are built from."""
    lat = max(min(lat, 85.05112878), -85.05112878)  # Web Mercator's defined latitude range
    lat_rad = math.radians(lat)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _quadkey(x: int, y: int, zoom: int) -> str:
    digits = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def _quadkeys_for_bbox(bbox: Bbox, zoom: int) -> List[str]:
    """Every quadkey at `zoom` that `bbox` (lon/lat) overlaps."""
    minx, miny, maxx, maxy = bbox
    xa, ya = _tile_xy(minx, miny, zoom)
    xb, yb = _tile_xy(maxx, maxy, zoom)
    xs = range(min(xa, xb), max(xa, xb) + 1)
    ys = range(min(ya, yb), max(ya, yb) + 1)
    return [_quadkey(x, y, zoom) for x in xs for y in ys]


def native_gsd(bbox: Optional[List[float]], shape: Optional[List[float]]) -> Optional[float]:
    """Ground sample distance in the item's own native CRS units (not yet meters) -- proj:bbox's
    span divided by proj:shape, per-axis average. None if either property is missing/degenerate."""
    if not bbox or not shape or len(bbox) < 4 or len(shape) < 2:
        return None
    height, width = shape[0], shape[1]
    if not width or not height:
        return None
    gsd_x = (bbox[2] - bbox[0]) / width
    gsd_y = (bbox[3] - bbox[1]) / height
    return (gsd_x + gsd_y) / 2


@dataclass(frozen=True)
class MosaicJsonSpec:
    name: str  # mosaic name, used in the written document too
    json_path: str
    tiles: Tuple[Item, ...]


def plan_mosaicjson(items: Iterable[Item], folder: str, stamp: str) -> Tuple[List[MosaicJsonSpec], List[Item]]:
    """One spec per collection among `items` that has at least two tiles with a raster asset, plus
    the tiles left out (no asset, or the only tile of their collection) -- same grouping as the VRT
    mosaic (mosaic.py's plan_mosaics), so a mixed selection still gets one mosaic per collection."""
    groups: Dict[str, List[Item]] = {}
    left_out: List[Item] = []
    for item in items:
        asset = primary_asset(item, lidar=False)
        if asset is None or not asset.href:
            left_out.append(item)
        else:
            groups.setdefault(item.collection or "tiles", []).append(item)

    specs: List[MosaicJsonSpec] = []
    for collection, tiles in sorted(groups.items()):
        if len(tiles) < 2:
            left_out.extend(tiles)
            continue
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", collection)
        path = os.path.join(folder, f"{safe}_{len(tiles)}tiles_{stamp}.json")
        specs.append(MosaicJsonSpec(f"{collection} mosaic ({len(tiles)} tiles)", path, tuple(tiles)))
    return specs, left_out


def build_mosaicjson(spec: MosaicJsonSpec, gsd_meters_for: Callable[[Item], Optional[float]]) -> Optional[dict]:
    """The MosaicJSON document for one spec, or None if none of its tiles has a usable bbox."""
    entries = []
    for item in spec.tiles:
        asset = primary_asset(item, lidar=False)
        if asset is None or not asset.href or not item.bbox or len(item.bbox) < 4:
            continue
        entries.append((tuple(float(v) for v in item.bbox[:4]), asset.href, gsd_meters_for(item)))
    if not entries:
        return None

    bounds = _union_bbox([e[0] for e in entries])
    minzoom = _zoom_for_extent(bounds)
    center_lat = (bounds[1] + bounds[3]) / 2
    gsds = [e[2] for e in entries if e[2]]
    maxzoom = max((_zoom_for_resolution(g, center_lat) for g in gsds), default=minzoom + 8)
    maxzoom = max(maxzoom, minzoom)

    tiles: Dict[str, List[str]] = {}
    for bbox, href, _ in entries:
        for quadkey in _quadkeys_for_bbox(bbox, minzoom):
            tiles.setdefault(quadkey, []).append(href)

    return {
        "mosaicjson": MOSAICJSON_VERSION,
        "name": spec.name,
        "version": "1.0.0",
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "bounds": list(bounds),
        "center": [(bounds[0] + bounds[2]) / 2, center_lat, minzoom],
        "tiles": tiles,
    }


def write_mosaicjson(spec: MosaicJsonSpec, gsd_meters_for: Callable[[Item], Optional[float]]) -> bool:
    """Build and write spec's MosaicJSON file. Returns False (writes nothing) if none of its tiles
    had a usable bbox."""
    doc = build_mosaicjson(spec, gsd_meters_for)
    if doc is None:
        return False
    os.makedirs(os.path.dirname(spec.json_path), exist_ok=True)
    with open(spec.json_path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return True
