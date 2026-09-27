"""Area-of-interest state shared by both tabs. Geometry is kept in the map CRS it was drawn in
(so buffering in linear units can happen later) and reprojected to lon/lat only when needed."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsGeometry,
)
from qgis.PyQt.QtCore import QObject, pyqtSignal

WGS84 = "EPSG:4326"
# Highest dimension first: when mixed feature types are selected, the AOI uses the highest present.
_DIMENSION_ORDER = (Qgis.GeometryType.Polygon, Qgis.GeometryType.Line, Qgis.GeometryType.Point)


def _ok(result) -> bool:
    return int(getattr(result, "value", result)) == 0


def union_geometries(geometries: List[QgsGeometry]) -> Tuple[Optional[QgsGeometry], int]:
    """Union geometries of the highest dimension present (polygon > line > point).

    Returns (union, number_ignored) where number_ignored counts the lower-dimension inputs that
    were left out, or (None, 0) if there was nothing usable.
    """
    usable = [g for g in geometries if g is not None and not g.isNull() and not g.isEmpty()]
    if not usable:
        return None, 0
    by_dimension: Dict[Any, List[QgsGeometry]] = {}
    for g in usable:
        by_dimension.setdefault(g.type(), []).append(g)
    for dimension in _DIMENSION_ORDER:
        chosen = by_dimension.get(dimension)
        if chosen:
            union = chosen[0] if len(chosen) == 1 else QgsGeometry.unaryUnion(chosen)
            return union, len(usable) - len(chosen)
    return None, 0


def make_valid_same_type(geometry: QgsGeometry) -> QgsGeometry:
    """Repair an invalid geometry (e.g. a self-intersecting drawn polygon) without changing its
    dimension: makeValid() can return a collection mixing in stray lines/points, which are dropped."""
    if geometry.isGeosValid():
        return geometry
    fixed = geometry.makeValid()
    if fixed.isNull() or fixed.isEmpty():
        return geometry
    dimension = geometry.type()
    if fixed.wkbType() != Qgis.WkbType.GeometryCollection and fixed.type() == dimension:
        return fixed
    parts = [p for p in fixed.asGeometryCollection() if p.type() == dimension]
    if not parts:
        return geometry
    return parts[0] if len(parts) == 1 else QgsGeometry.collectGeometry(parts)


class AoiState(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.geometry: Optional[QgsGeometry] = None
        self.crs: Optional[QgsCoordinateReferenceSystem] = None
        self.description: str = ""

    @property
    def has_aoi(self) -> bool:
        return self.geometry is not None

    def set(self, geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem, description: str):
        self.geometry = QgsGeometry(geometry)
        self.crs = QgsCoordinateReferenceSystem(crs)
        self.description = description
        self.changed.emit()

    def clear(self):
        self.geometry = None
        self.crs = None
        self.description = ""
        self.changed.emit()

    def geometry_wgs84(self, transform_context: Optional[QgsCoordinateTransformContext] = None):
        """The AOI as a valid lon/lat (CRS84) geometry, or None if there is no AOI."""
        if self.geometry is None:
            return None
        g = QgsGeometry(self.geometry)
        target = QgsCoordinateReferenceSystem(WGS84)
        if self.crs != target:
            xform = QgsCoordinateTransform(self.crs, target, transform_context or QgsCoordinateTransformContext())
            if not _ok(g.transform(xform)):
                raise ValueError("Could not reproject the area of interest to lon/lat")
        return make_valid_same_type(g)

    def geojson_geometry(self, transform_context: Optional[QgsCoordinateTransformContext] = None):
        """The AOI as a GeoJSON geometry dict (lon/lat), for a STAC "intersects" search."""
        g = self.geometry_wgs84(transform_context)
        return None if g is None else json.loads(g.asJson(8))
