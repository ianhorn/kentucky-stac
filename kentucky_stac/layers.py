"""Add search results to the map as layers: COG rasters and COPC point clouds, loaded remotely.

Opening a remote layer takes a couple of seconds (network round-trips), so layers are built on
worker threads and only added to the project on the main thread.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsMapLayer,
    QgsPointCloudLayer,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
)

from .catalog import DEFAULT_CRS, primary_asset
from .gdal_setup import ensure_ca_bundle
from .stac import Item

GROUP_NAME = "Kentucky STAC"
MAX_WORKERS = 4


@dataclass(frozen=True)
class LayerSpec:
    name: str
    uri: str
    provider: str  # "gdal" or "copc"


def raster_uri(href: str) -> str:
    # list_dir=no stops GDAL from probing the "directory" around each tile (several extra requests).
    return f"/vsicurl?list_dir=no&url={href}"


def layer_specs(items: Iterable[Item], lidar: bool) -> Tuple[List[LayerSpec], List[Item]]:
    """Layer specs for the tiles that can be streamed, plus the tiles that can't (plain LAZ or
    LAS point clouds, which QGIS can only read once downloaded)."""
    specs: List[LayerSpec] = []
    skipped: List[Item] = []
    for item in items:
        asset = primary_asset(item, lidar)
        if asset is None or not asset.href:
            skipped.append(item)
        elif lidar:
            if asset.is_copc:
                specs.append(LayerSpec(item.id, asset.href, "copc"))
            else:
                skipped.append(item)
        else:
            specs.append(LayerSpec(item.id, raster_uri(asset.href), "gdal"))
    return specs, skipped


def _build_layer(spec: LayerSpec) -> Tuple[Optional[QgsMapLayer], Optional[str]]:
    """Runs on a worker thread. Returns (layer, error)."""
    try:
        if spec.provider == "copc":
            layer: QgsMapLayer = QgsPointCloudLayer(spec.uri, spec.name, "copc")
        else:
            layer = QgsRasterLayer(spec.uri, spec.name, "gdal")
        if not layer.isValid():
            return None, layer.error().summary() or "could not open"
        # Fallback only: the tiles checked so far all carry a usable CRS (Phase 3 point clouds embed
        # a compound one with no EPSG code), so this should rarely trigger.
        if not layer.crs().isValid():
            layer.setCrs(QgsCoordinateReferenceSystem(DEFAULT_CRS))
        # Layers must belong to the main thread before they are added to the project.
        layer.moveToThread(QgsApplication.instance().thread())
        return layer, None
    except Exception as e:
        return None, str(e)


class AddLayersTask(QgsTask):
    """Build layers in parallel, then add them to the project (main thread) in the
    "Kentucky STAC" group. `callback(added, failed)` where failed is a list of (name, error)."""

    def __init__(self, specs: List[LayerSpec], callback: Callable[[int, List[Tuple[str, str]]], None]):
        super().__init__(f"Adding {len(specs)} Kentucky STAC layer{'s' if len(specs) != 1 else ''}")
        self._specs = specs
        self._callback = callback
        self._built: List[Tuple[LayerSpec, Optional[QgsMapLayer], Optional[str]]] = []

    def run(self) -> bool:
        ensure_ca_bundle()
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
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            added += 1
        self._callback(added, failed)


def already_on_map(specs: List[LayerSpec], project: Optional[QgsProject] = None) -> List[LayerSpec]:
    """The specs whose source is already a layer in the project."""
    project = project or QgsProject.instance()
    sources = {layer.source() for layer in project.mapLayers().values()}
    return [s for s in specs if s.uri in sources]
