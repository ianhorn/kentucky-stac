"""Stitch selected COG tiles into a virtual mosaic (a GDAL VRT that reads the tiles lazily).

One VRT is built per collection: collections differ in band count, data type and pixel size (and
overlapping phases would otherwise silently paint one year over another).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsTask

from .catalog import primary_asset
from .gdal_setup import setup_gdal
from .layers import Bbox, item_bbox, raster_uri
from .stac import Item


@dataclass(frozen=True)
class MosaicSpec:
    name: str  # layer name
    vrt_path: str
    sources: Tuple[str, ...]
    bbox: Optional[Bbox]  # union of the tiles' catalog footprints, used to verify placement


def _union(boxes: List[Bbox]) -> Optional[Bbox]:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def plan_mosaics(items: Iterable[Item], folder: str, stamp: str) -> Tuple[List[MosaicSpec], List[Item]]:
    """One mosaic spec per collection among `items` that has at least two tiles with a raster asset,
    plus the tiles that were left out (no asset, or the only tile of their collection).
    `stamp` keeps file names unique between runs so an earlier mosaic on the map isn't overwritten."""
    groups: Dict[str, List[Item]] = {}
    left_out: List[Item] = []
    for item in items:
        asset = primary_asset(item, lidar=False)
        if asset is None or not asset.href:
            left_out.append(item)
        else:
            groups.setdefault(item.collection or "tiles", []).append(item)

    specs: List[MosaicSpec] = []
    for collection, tiles in sorted(groups.items()):
        if len(tiles) < 2:
            left_out.extend(tiles)
            continue
        sources = tuple(raster_uri(primary_asset(t, False).href) for t in tiles)
        boxes = [b for b in (item_bbox(t) for t in tiles) if b is not None]
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", collection)
        path = os.path.join(folder, f"{safe}_{len(tiles)}tiles_{stamp}.vrt")
        specs.append(MosaicSpec(f"{collection} mosaic ({len(tiles)} tiles)", path, sources, _union(boxes)))
    return specs, left_out


class BuildMosaicsTask(QgsTask):
    """Build the VRT files on a worker thread (each source is opened over the network).
    `callback(built, failed)` runs on the main thread: `built` is a list of MosaicSpec, `failed` a
    list of (name, error), unless the task was cancelled.

    `clip_wkt`, if given, is a polygon in lon/lat (EPSG:4326) that every mosaic is cropped to (a
    GDAL warp cutline), so the result covers the AOI's actual shape rather than the tiles' full
    rectangular extent. The pixels outside it come back as nodata/masked, not just cropped to a
    bounding box. Building is still lazy -- nothing is downloaded here beyond what BuildVRT already
    reads (each tile's header), and reading the result still pulls pixels from the remote tiles.
    """

    def __init__(
        self,
        specs: List[MosaicSpec],
        callback: Callable[[List[MosaicSpec], List[Tuple[str, str]]], None],
        clip_wkt: Optional[str] = None,
    ):
        super().__init__(f"Building {len(specs)} mosaic{'s' if len(specs) != 1 else ''}")
        self._specs = specs
        self._callback = callback
        self._clip_wkt = clip_wkt
        self.built: List[MosaicSpec] = []
        self.failed: List[Tuple[str, str]] = []

    def run(self) -> bool:
        setup_gdal()
        try:
            from osgeo import gdal
        except ImportError:
            self.failed = [(s.name, "the GDAL Python bindings are not available") for s in self._specs]
            return False
        # Highest resolution wins where tiles differ in pixel size; nodata is taken from the sources.
        options = gdal.BuildVRTOptions(resolution="highest")
        for i, spec in enumerate(self._specs):
            if self.isCanceled():
                return False
            try:
                os.makedirs(os.path.dirname(spec.vrt_path), exist_ok=True)
                if self._clip_wkt:
                    self._build_clipped(gdal, spec)
                else:
                    dataset = gdal.BuildVRT(spec.vrt_path, list(spec.sources), options=options)
                    if dataset is None:
                        self.failed.append((spec.name, gdal.GetLastErrorMsg() or "could not build the VRT"))
                        continue
                    dataset.FlushCache()
                    dataset = None  # closing writes the file
                self.built.append(spec)
            except Exception as e:
                self.failed.append((spec.name, str(e)))
            self.setProgress(100.0 * (i + 1) / len(self._specs))
        return True

    def _build_clipped(self, gdal, spec: MosaicSpec) -> None:
        """Plain mosaic first, then warp that with a cutline -- GDAL's own recommended order, and
        much faster than warping every remote source directly.

        The intermediate is written next to the final VRT and kept, not a temp file that gets
        cleaned up: a warped VRT references its source dataset by path (unlike a plain BuildVRT
        mosaic, whose <SimpleSource> entries point straight at the remote tiles), so deleting the
        intermediate leaves the final VRT completely unreadable -- confirmed directly: QGIS reports
        the layer invalid the moment the intermediate file is gone, not just missing some pixels.
        """
        options = gdal.BuildVRTOptions(resolution="highest")
        root, _ext = os.path.splitext(spec.vrt_path)
        unclipped = f"{root}.unclipped.vrt"
        dataset = gdal.BuildVRT(unclipped, list(spec.sources), options=options)
        if dataset is None:
            raise RuntimeError(gdal.GetLastErrorMsg() or "could not build the VRT")
        native_srs = dataset.GetProjection()
        dataset.FlushCache()
        dataset = None

        clipped = gdal.Warp(
            spec.vrt_path,
            [unclipped],
            format="VRT",
            cutlineWKT=self._clip_wkt,
            cutlineSRS="EPSG:4326",
            cropToCutline=True,
            dstSRS=native_srs or None,
        )
        if clipped is None:
            raise RuntimeError(gdal.GetLastErrorMsg() or "could not clip the mosaic to the area of interest")
        clipped.FlushCache()
        clipped = None

    def finished(self, result: bool) -> None:
        if not self.isCanceled():
            self._callback(self.built, self.failed)
