"""Add search results to the map as layers: COG rasters and COPC point clouds, loaded remotely.

Opening a remote layer takes a couple of seconds (network round-trips), so layers are built on
worker threads and only added to the project on the main thread.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsMapLayer,
    QgsPointCloudLayer,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
)

from . import s3, signing
from .catalog import DEFAULT_CRS, can_add_to_map, primary_asset
from .gdal_setup import setup_gdal
from .stac import Item

GROUP_NAME = "Ky STAC"
MAX_WORKERS = 4


Bbox = Tuple[float, float, float, float]  # minx, miny, maxx, maxy in lon/lat


@dataclass(frozen=True)
class LayerSpec:
    name: str
    uri: str
    provider: str  # "gdal" (raster), "copc"/"pdal" (a single point cloud file) or "vpc" (a combined
    # Virtual Point Cloud index over several local files -- see vpc.py)
    bbox: Optional[Bbox] = None  # where the catalog says the tile is, used to catch a wrong CRS


_payer_set = False


def raster_uri(href: str) -> str:
    if s3.is_s3(href):
        if s3.credentials_available():
            # Signed with the user's AWS credentials (GDAL reads them itself). Requester-pays buckets also
            # need this header, which GDAL only takes as a setting -- harmless for ordinary buckets.
            global _payer_set
            if not _payer_set:
                from osgeo import gdal

                gdal.SetConfigOption("AWS_REQUEST_PAYER", "requester")
                _payer_set = True
            return s3.vsis3_path(href)
    href = signing.fetchable_url(href)  # s3:// -> https (a public bucket); Azure blob -> signed
    # list_dir=no stops GDAL from probing the "directory" around each tile (several extra requests).
    return f"/vsicurl?list_dir=no&url={href.replace(chr(38), '%26')}"  # a SAS token holds &s that would end the url option


def layer_specs(items: Iterable[Item], lidar: bool) -> Tuple[List[LayerSpec], List[Item]]:
    """Layer specs for the tiles that can be streamed, plus the tiles that can't (plain LAZ or
    LAS point clouds, which QGIS can only read once downloaded)."""
    specs: List[LayerSpec] = []
    skipped: List[Item] = []
    for item in items:
        asset = primary_asset(item, lidar)
        bbox = item_bbox(item)
        if asset is None or not asset.href:
            skipped.append(item)
        elif lidar:
            if asset.is_copc:
                specs.append(LayerSpec(item.id, signing.fetchable_url(asset.href), "copc", bbox))
            else:
                skipped.append(item)
        else:
            specs.append(LayerSpec(item.id, raster_uri(asset.href), "gdal", bbox))
    return specs, skipped


def asset_layer_spec(item: Item, key: str) -> Optional[LayerSpec]:
    """A layer spec for one named asset of a tile (any raster asset, or a COPC point cloud), or None if
    that asset can't be shown on the map."""
    asset = item.assets.get(key)
    if asset is None or not can_add_to_map(asset):
        return None
    name = f"{item.id} · {key}"
    bbox = item_bbox(item)
    if asset.is_copc:
        return LayerSpec(name, signing.fetchable_url(asset.href), "copc", bbox)
    return LayerSpec(name, raster_uri(asset.href), "gdal", bbox)


def union_bbox(boxes: List[Bbox]) -> Optional[Bbox]:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def item_bbox(item: Item) -> Optional[Bbox]:
    bbox = item.bbox
    return tuple(float(v) for v in bbox[:4]) if bbox and len(bbox) >= 4 else None


def local_specs(paths: Iterable[str], bboxes: Optional[Dict[str, Optional[Bbox]]] = None) -> List[LayerSpec]:
    """Layer specs for downloaded files, by extension. Plain LAZ/LAS load through the PDAL provider,
    COPC through the COPC provider (both read local files); unknown types are ignored. `bboxes`
    maps a path to the tile's catalog footprint, used to catch a wrong CRS."""
    bboxes = bboxes or {}
    specs: List[LayerSpec] = []
    for path in paths:
        lower = os.path.basename(path).lower()
        if lower.endswith(".copc.laz"):
            provider, stem = "copc", os.path.basename(path)[: -len(".copc.laz")] + ".copc"
        elif lower.endswith((".laz", ".las")):
            provider, stem = "pdal", os.path.splitext(os.path.basename(path))[0]
        elif lower.endswith((".tif", ".tiff")):
            provider, stem = "gdal", os.path.splitext(os.path.basename(path))[0]
        else:
            continue
        specs.append(LayerSpec(stem, path, provider, bboxes.get(path)))
    return specs


def _fresh_crs(crs: QgsCoordinateReferenceSystem) -> QgsCoordinateReferenceSystem:
    """A CRS object created on the calling thread. A layer built on a worker thread carries a CRS
    object created there, and coordinate transforms built from it on the main thread come back
    invalid (in one direction) and also spoil later transforms for the same CRS."""
    fresh = QgsCoordinateReferenceSystem(crs.authid()) if crs.authid() else QgsCoordinateReferenceSystem.fromWkt(crs.toWkt())
    return fresh if fresh.isValid() else crs


def _wgs84_extent(layer: QgsMapLayer) -> Optional[QgsRectangle]:
    """The layer's extent in lon/lat under its current CRS, or None if that can't be computed."""
    try:
        transform = QgsCoordinateTransform(
            _fresh_crs(layer.crs()), QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateTransformContext()
        )
        return transform.transformBoundingBox(layer.extent())
    except Exception:
        return None


def _is_placed(layer: QgsMapLayer, bbox: Bbox) -> bool:
    extent = _wgs84_extent(layer)
    if extent is None or extent.isEmpty():
        return False
    # Tiles are ~0.02 degrees across, so a generous margin still separates Kentucky from Iran.
    expected = QgsRectangle(*bbox).buffered(0.05)
    return expected.contains(extent.center())


def ensure_placed(layer: QgsMapLayer, bbox: Optional[Bbox]) -> bool:
    """Make sure the layer lands where the catalog says the tile is. A file with no CRS of its own
    can pick up the project's CRS (say Web Mercator), which puts a Kentucky tile in Iran. If the
    layer is misplaced and the Kentucky CRS puts it in the right place, switch to that CRS.
    Returns True if the CRS was changed."""
    if bbox is None:
        return False
    if layer.crs().isValid() and _is_placed(layer, bbox):
        return False
    original = layer.crs()
    layer.setCrs(QgsCoordinateReferenceSystem(DEFAULT_CRS))
    if _is_placed(layer, bbox):
        return True
    layer.setCrs(original)  # the fallback doesn't fit either; don't guess
    return False


def _build_layer(spec: LayerSpec) -> Tuple[Optional[QgsMapLayer], Optional[str]]:
    """Runs on a worker thread. Returns (layer, error)."""
    try:
        if spec.provider in ("copc", "pdal", "vpc"):
            layer: QgsMapLayer = QgsPointCloudLayer(spec.uri, spec.name, spec.provider)
        else:
            layer = QgsRasterLayer(spec.uri, spec.name, "gdal")
        if not layer.isValid():
            return None, layer.error().summary() or "could not open"
        # The placement check against the catalog footprint (ensure_placed) runs later on the main
        # thread, once the layer has been added, so no coordinate transforms are built on workers.
        if not layer.crs().isValid():
            layer.setCrs(QgsCoordinateReferenceSystem(DEFAULT_CRS))
        # Layers must belong to the main thread before they are added to the project.
        layer.moveToThread(QgsApplication.instance().thread())
        return layer, None
    except Exception as e:
        return None, str(e)


class AddLayersTask(QgsTask):
    """Build layers in parallel, then add them to the project (main thread) in the
    "Ky STAC" group. `callback(added, failed)` where failed is a list of (name, error)."""

    def __init__(self, specs: List[LayerSpec], callback: Callable[[int, List[Tuple[str, str]]], None]):
        super().__init__(f"Adding {len(specs)} Kentucky STAC layer{'s' if len(specs) != 1 else ''}")
        self._specs = specs
        self._callback = callback
        self._built: List[Tuple[LayerSpec, Optional[QgsMapLayer], Optional[str]]] = []

    def run(self) -> bool:
        setup_gdal()
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for spec, (layer, error) in zip(self._specs, pool.map(_build_layer, self._specs)):
                self._built.append((spec, layer, error))
                done += 1
                self.setProgress(100.0 * done / len(self._specs))
                if self.isCanceled():
                    return False
        return True

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
        added, failed = 0, []
        for spec, layer, error in self._built:
            if layer is None:
                failed.append((spec.name, error or "unknown error"))
                continue
            # Replace the worker-created CRS object with one made on this thread (see _fresh_crs), so
            # nothing that uses the layer later, transforms or map rendering, holds a worker CRS.
            # setCrs() does nothing for an equal CRS, so go through an invalid one to force the swap.
            fresh = _fresh_crs(layer.crs())
            layer.setCrs(QgsCoordinateReferenceSystem())
            layer.setCrs(fresh)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            # QGIS can apply the project CRS to a layer as it is added; verify placement again.
            ensure_placed(layer, spec.bbox)
            added += 1
        self._callback(added, failed)


def already_on_map(specs: List[LayerSpec], project: Optional[QgsProject] = None) -> List[LayerSpec]:
    """The specs whose source is already a layer in the project."""
    project = project or QgsProject.instance()
    sources = {layer.source() for layer in project.mapLayers().values()}
    return [s for s in specs if s.uri in sources]
