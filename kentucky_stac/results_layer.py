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

from .antimeridian import crosses_antimeridian, wrap_antimeridian
from .catalog import bbox_2d, format_size, primary_asset
from .stac import Item

KIND_PROPERTY = "kentucky_stac/results_kind"


def geojson_to_geometry(geometry: Optional[Dict[str, Any]], bbox: Optional[Sequence[float]] = None) -> Optional[QgsGeometry]:
    """A Polygon/MultiPolygon GeoJSON dict as a QgsGeometry, falling back to the item's bbox. A tile over the
    antimeridian comes back cut there, one part at each edge of the world, not as one shape across all of it."""

    def ring(coords):
        return [QgsPointXY(c[0], c[1]) for c in coords]

    flat = bbox_2d(bbox) if bbox and len(bbox) >= 4 else None
    crossing = crosses_antimeridian(flat)
    shape = None
    if geometry:
        kind, coords = geometry.get("type"), geometry.get("coordinates")
        if kind == "Polygon" and coords:
            shape = QgsGeometry.fromPolygonXY([ring(r) for r in coords])
        elif kind == "MultiPolygon" and coords:
            shape = QgsGeometry.fromMultiPolygonXY([[ring(r) for r in poly] for poly in coords])
    if shape is not None:
        if crossing and shape.boundingBox().width() > 180.0:
            # Not cut at the antimeridian as RFC 7946 asks, so its ring runs the long way round the world: the
            # western longitudes belong a world to the east, where the box says the tile is.
            for index, vertex in enumerate(list(shape.vertices())):
                if vertex.x() < 0.0:
                    shape.moveVertex(vertex.x() + 360.0, vertex.y(), index)
        return wrap_antimeridian(shape)
    if flat:
        if crossing:
            west, south, east, north = flat
            return QgsGeometry.collectGeometry(
                [QgsGeometry.fromRect(QgsRectangle(west, south, 180.0, north)), QgsGeometry.fromRect(QgsRectangle(-180.0, south, east, north))]
            )
        return QgsGeometry.fromRect(QgsRectangle(*flat))
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
