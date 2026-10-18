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

from . import auth, s3, signing
from .catalog import DEFAULT_CRS, bbox_2d, can_add_to_map, primary_asset
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
    # Builds the layer itself (on the worker thread) instead of opening `uri`: band combinations and
    # indices, which need several remote files opened and a style applied (see band_layers.py).
    factory: Optional[Callable[[], Optional[QgsMapLayer]]] = None


_payer_set = False


def raster_uri(href: str, authcfg: str = "") -> str:
    """A GDAL path for a remote raster. With `authcfg` (a QGIS authentication configuration) GDAL is given that
    login for this file's host or bucket first -- call it on the main thread."""
    if authcfg:
        auth.prepare_asset(href, authcfg)
    if s3.is_s3(href):
        if authcfg:
            return s3.vsis3_path(href)  # the configuration's own AWS keys were just set for this bucket
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
    if "?" in href or authcfg:
        # A signed URL (an Azure SAS token) carries percent-escapes, which GDAL's url= option decodes: %2B
        # turned into a bare + and the signature failed with a 403. The plain path form leaves it alone.
        return f"/vsicurl/{href}"
    # list_dir=no stops GDAL from probing the "directory" around each tile (several extra requests).
    return f"/vsicurl?list_dir=no&url={href.replace(chr(38), '%26')}"


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
            specs.append(LayerSpec(item.id, raster_uri(asset.href, auth.cfg_for_source(item.source)), "gdal", bbox))
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
    return LayerSpec(name, raster_uri(asset.href, auth.cfg_for_source(item.source)), "gdal", bbox)


def union_bbox(boxes: List[Bbox]) -> Optional[Bbox]:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def item_bbox(item: Item) -> Optional[Bbox]:
    bbox = item.bbox
    return bbox_2d(bbox)


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


_EPSG_PROPERTY = "kentucky_stac/crs_epsg"  # the raster's horizontal EPSG code, worked out by GDAL on the worker
_WKT_PROPERTY = "kentucky_stac/crs_wkt"  # the CRS's WKT, read on the worker thread while the CRS is still alive


def _fresh_crs(crs: QgsCoordinateReferenceSystem, wkt: str = "") -> QgsCoordinateReferenceSystem:
    """A CRS object created on the calling thread. A layer built on a worker thread carries a CRS
    object created there, and coordinate transforms built from it on the main thread come back
    invalid (in one direction) and also spoil later transforms for the same CRS. Once that worker thread
    has ended the object can be empty (toWkt() returns ""), so `wkt` -- read on the worker -- is preferred."""
    fresh = (
        QgsCoordinateReferenceSystem(crs.authid())
        if crs.authid()
        else QgsCoordinateReferenceSystem.fromWkt(wkt or crs.toWkt())
    )
    return fresh if fresh.isValid() else crs


def flat_crs(crs: QgsCoordinateReferenceSystem) -> QgsCoordinateReferenceSystem:
    """The horizontal part of a compound CRS (e.g. "NAD83 / UTM zone 12N + NAVD88 height"), or `crs` itself.

    A raster is drawn in 2D, but QGIS transforming a compound CRS can fail (the vertical datum needs a geoid
    grid) and then leaves the coordinates untouched: a Utah tile in UTM metres was drawn in Algeria. The
    horizontal CRS transforms reliably and is all a map needs.

    The split is done with GDAL on the CRS's WKT: asking QGIS for the vertical/horizontal parts proved unreliable
    (after the first layer in a session it reported a compound CRS as having no vertical part).
    """
    wkt = crs.toWkt()
    if "COMPD_CS" not in wkt and "COMPOUNDCRS" not in wkt.upper():
        return crs
    try:
        from osgeo import osr

        srs = osr.SpatialReference()
        if srs.ImportFromWkt(wkt) != 0 or not srs.IsCompound():
            return crs
        srs.StripVertical()
        flat = QgsCoordinateReferenceSystem.fromWkt(srs.ExportToWkt())
        if not flat.isValid():
            return crs
        srs.AutoIdentifyEPSG()
        code = srs.GetAuthorityCode(None)
        if code:  # a plain EPSG id is the most robust thing to hand QGIS
            named = QgsCoordinateReferenceSystem(f"EPSG:{code}")
            if named.isValid():
                return named
        return flat
    except Exception:
        return crs


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
    flat = flat_crs(original)
    if flat != original:  # a compound CRS that did not transform: try its horizontal part
        layer.setCrs(flat)
        if _is_placed(layer, bbox):
            return True
    layer.setCrs(QgsCoordinateReferenceSystem(DEFAULT_CRS))
    if _is_placed(layer, bbox):
        return True
    layer.setCrs(original)  # the fallback doesn't fit either; don't guess
    return False


def _horizontal_epsg(path: str) -> str:
    """The EPSG code of a raster's horizontal CRS (its vertical datum, if any, dropped), or "". Uses GDAL/OSR
    alone, so no QGIS CRS object is created on this worker thread (those can be left empty once the thread ends)."""
    try:
        from osgeo import gdal, osr

        dataset = gdal.Open(path)
        wkt = dataset.GetProjection() if dataset is not None else ""
        dataset = None
        if not wkt:
            return ""
        srs = osr.SpatialReference()
        if srs.ImportFromWkt(wkt) != 0:
            return ""
        if srs.IsCompound():
            srs.StripVertical()
        code = srs.GetAuthorityCode(None)
        if not code:
            srs.AutoIdentifyEPSG()
            code = srs.GetAuthorityCode(None)
        return str(code or "")
    except Exception:
        return ""


def _las_horizontal_epsg(url: str) -> str:
    """The EPSG code of a LAS/COPC file's horizontal CRS, read from the file's own header with GDAL and OSR, or "".

    The header's WKT variable-length record sits within the first few KB, so one small range read is enough. Asking
    QGIS for the CRS of a point cloud opened on a worker thread proved unreliable: after the first compound-CRS
    cloud in a session it came back empty, and the cloud was drawn at its raw UTM numbers.
    """
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        import struct

        from osgeo import gdal, osr

        handle = gdal.VSIFOpenL("/vsicurl/" + url, "rb")
        if handle is None:
            return ""
        try:
            data = gdal.VSIFReadL(1, 131072, handle) or b""
        finally:
            gdal.VSIFCloseL(handle)
        if data[:4] != b"LASF" or len(data) < 375:
            return ""
        header_size = struct.unpack_from("<H", data, 94)[0]
        vlr_count = struct.unpack_from("<I", data, 100)[0]
        position = header_size
        for _ in range(vlr_count):
            if position + 54 > len(data):
                break
            user_id = data[position + 2 : position + 18].split(b"\0", 1)[0]
            record_id, length = struct.unpack_from("<HH", data, position + 18)
            body = data[position + 54 : position + 54 + length]
            position += 54 + length
            if user_id == b"LASF_Projection" and record_id == 2112:
                srs = osr.SpatialReference()
                if srs.ImportFromWkt(body.split(b"\0", 1)[0].decode("utf-8", "ignore")) != 0:
                    return ""
                if srs.IsCompound():
                    srs.StripVertical()
                code = srs.GetAuthorityCode(None)
                if not code:
                    srs.AutoIdentifyEPSG()
                    code = srs.GetAuthorityCode(None)
                return str(code or "")
    except Exception:
        return ""
    return ""


def _point_cloud_epsg(layer: QgsPointCloudLayer) -> str:
    """The EPSG code of a point cloud's horizontal CRS when its CRS is compound (a vertical datum added), else "".

    Read on the worker thread, where the layer's CRS object is still usable. A compound CRS can fail to
    transform later and leave the cloud drawn at its raw UTM numbers (a Utah cloud in Africa), while the plain
    horizontal CRS always transforms; a map needs nothing more."""
    try:
        crs = layer.crs()
        if crs.verticalCrs().isValid():
            authid = crs.horizontalCrs().authid()
            if authid.upper().startswith("EPSG:"):
                return authid.split(":", 1)[1]
    except Exception:
        pass
    return ""


def _build_layer(spec: LayerSpec) -> Tuple[Optional[QgsMapLayer], Optional[str]]:
    """Runs on a worker thread. Returns (layer, error)."""
    try:
        if spec.factory is not None:
            layer = spec.factory()
            if layer is None:
                return None, "could not build the layer"
        elif spec.provider in ("copc", "pdal", "vpc"):
            layer = QgsPointCloudLayer(spec.uri, spec.name, spec.provider)
        else:
            layer = QgsRasterLayer(spec.uri, spec.name, "gdal")
        if not layer.isValid():
            return None, layer.error().summary() or "could not open"
        # The placement check against the catalog footprint (ensure_placed) runs later on the main
        # thread, once the layer has been added, so no coordinate transforms are built on workers.
        if not layer.crs().isValid():
            layer.setCrs(QgsCoordinateReferenceSystem(DEFAULT_CRS))
        # Read the CRS's WKT now, while this thread's CRS object is alive: after the worker ends it can come
        # back empty, which left a compound CRS broken and drew a Utah tile in Algeria (see _fresh_crs).
        layer.setCustomProperty(_WKT_PROPERTY, layer.crs().toWkt())
        if isinstance(layer, QgsRasterLayer) and spec.factory is None:
            layer.setCustomProperty(_EPSG_PROPERTY, _horizontal_epsg(spec.uri))
        elif isinstance(layer, QgsPointCloudLayer):
            layer.setCustomProperty(_EPSG_PROPERTY, _las_horizontal_epsg(spec.uri) or _point_cloud_epsg(layer))
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
            fresh = _fresh_crs(layer.crs(), str(layer.customProperty(_WKT_PROPERTY, "") or ""))
            code = str(layer.customProperty(_EPSG_PROPERTY, "") or "")
            layer.removeCustomProperty(_WKT_PROPERTY)
            layer.removeCustomProperty(_EPSG_PROPERTY)
            if code or isinstance(layer, QgsRasterLayer):
                # Use the plain horizontal EPSG CRS (a compound CRS's vertical datum can break the transform),
                # made fresh on this thread from just its code.
                named = QgsCoordinateReferenceSystem(f"EPSG:{code}") if code else QgsCoordinateReferenceSystem()
                if named.isValid():
                    fresh = named
                elif isinstance(layer, QgsRasterLayer):
                    fresh = flat_crs(fresh)
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
    layers = project.mapLayers().values()
    sources = {layer.source() for layer in layers}
    names = {layer.name() for layer in layers}
    # A factory-built layer's source isn't known beforehand, so it is recognised by its name.
    return [s for s in specs if (s.name in names if s.factory is not None else s.uri in sources)]
