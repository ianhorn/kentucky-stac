"""A scratch layer drawing search-result tile footprints on the map, one per tab."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from qgis.core import (
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

from .catalog import format_size, primary_asset
from .stac import Item

KIND_PROPERTY = "kentucky_stac/results_kind"


def geojson_to_geometry(geometry: Optional[Dict[str, Any]], bbox: Optional[Sequence[float]] = None) -> Optional[QgsGeometry]:
    """A Polygon/MultiPolygon GeoJSON dict as a QgsGeometry, falling back to the item's bbox."""

    def ring(coords):
        return [QgsPointXY(c[0], c[1]) for c in coords]

    if geometry:
        kind, coords = geometry.get("type"), geometry.get("coordinates")
        if kind == "Polygon" and coords:
            return QgsGeometry.fromPolygonXY([ring(r) for r in coords])
        if kind == "MultiPolygon" and coords:
            return QgsGeometry.fromMultiPolygonXY([[ring(r) for r in poly] for poly in coords])
    if bbox and len(bbox) >= 4:
        return QgsGeometry.fromRect(QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))
    return None


def _get_or_create(kind: str, title: str, project: QgsProject) -> QgsVectorLayer:
    for layer in project.mapLayers().values():
        if layer.customProperty(KIND_PROPERTY) == kind:
            return layer
    layer = QgsVectorLayer(
        "Polygon?crs=EPSG:4326&field=id:string&field=collection:string&field=date:string"
        "&field=asset:string&field=size:string",
        title,
        "memory",
    )
    layer.setCustomProperty(KIND_PROPERTY, kind)
    layer.renderer().setSymbol(
        QgsFillSymbol.createSimple({"color": "255,140,0,30", "outline_color": "230,110,0,255", "outline_width": "0.5"})
    )
    project.addMapLayer(layer)
    return layer


def show_results(kind: str, title: str, items: List[Item], lidar: bool, project: Optional[QgsProject] = None) -> List[Optional[int]]:
    """Replace the results layer's features with `items`. Returns, aligned with `items`, each tile's
    feature id in the layer (None for a tile with no usable footprint)."""
    project = project or QgsProject.instance()
    layer = _get_or_create(kind, title, project)
    provider = layer.dataProvider()
    provider.truncate()

    features, positions = [], []
    for position, item in enumerate(items):
        geometry = geojson_to_geometry(item.geometry, item.bbox)
        if geometry is None:
            continue
        asset = primary_asset(item, lidar)
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        feature.setAttributes(
            [item.id, item.collection or "", (item.datetime or "")[:10], asset.href if asset else "",
             format_size(asset.file_size) if asset else ""]
        )
        features.append(feature)
        positions.append(position)

    fids: List[Optional[int]] = [None] * len(items)
    ok, added = provider.addFeatures(features)
    if not ok:
        raise RuntimeError(f"Could not add the search results to the {layer.name()} layer")
    for position, feature in zip(positions, added):
        fids[position] = feature.id()
    layer.updateExtents()
    layer.triggerRepaint()
    return fids


def select_results(kind: str, fids: List[int], project: Optional[QgsProject] = None):
    """Highlight the given features in the results layer (empty list clears the selection)."""
    project = project or QgsProject.instance()
    for layer in project.mapLayers().values():
        if layer.customProperty(KIND_PROPERTY) == kind:
            layer.selectByIds(fids)
            return
