"""Longitudes past +-180 degrees, and boxes that cross the antimeridian.

A map can be panned past the antimeridian, so a drawn area of interest near the edge of the world can run
past it. Sent to a STAC API as it comes, that is not the place it looks like on the map, and the search finds
tiles on the other side of the world (an area over eastern New Zealand returned South America and Australia).
Two things cause it:

 * On a Web Mercator map, x runs past the edge of the world; reprojecting to lon/lat wraps 184 degrees to
   -176, so a shape from 176 to 184 turns into a ring from 176 to -176 that spans the whole globe the long
   way round, and a search finds everything on the same latitudes. The shape has to be cut at the world's
   edge in the map's own coordinates, before it is reprojected (cut_at_map_edge).
 * On a lon/lat map the longitudes simply go past 180 (wrap_antimeridian).

RFC 7946 (GeoJSON, section 3.1.9) says a shape over the antimeridian should be cut there, each part inside
-180..180; and a box that crosses it has its west edge east of its east edge (section 5.2).
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
)

_MAX_WORLDS = 4  # a shape wider than this many world copies is not an area anyone means


def crosses_antimeridian(bbox: Optional[Sequence[float]]) -> bool:
    """True for a 2D box (minx, miny, maxx, maxy) whose west edge is east of its east edge (RFC 7946 5.2)."""
    return bool(bbox) and len(bbox) >= 4 and float(bbox[0]) > float(bbox[2])


def wrap_into_world(geometry: QgsGeometry, west: float, east: float) -> QgsGeometry:
    """`geometry` with every part between x = `west` and x = `east` (one world's width apart), cut at those
    edges. A shape already inside is returned as it is; one that runs past either edge comes back as the parts
    on each side, each moved a whole number of worlds so it sits where the map shows it."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return geometry
    width = east - west
    box = geometry.boundingBox()
    if box.xMinimum() >= west and box.xMaximum() <= east:
        return geometry
    first = math.floor((box.xMinimum() - west) / width)
    last = math.floor((box.xMaximum() - west) / width)
    if last - first >= _MAX_WORLDS:
        return geometry
    dimension = geometry.type()
    south, north = box.yMinimum() - 1.0, box.yMaximum() + 1.0
    parts = []
    for world in range(first, last + 1):
        window = QgsGeometry.fromRect(QgsRectangle(west + width * world, south, east + width * world, north))
        piece = geometry.intersection(window)
        if piece.isNull() or piece.isEmpty():
            continue
        # Cutting along a shared edge can leave a stray line or point beside the polygon: keep the input's kind.
        for single in piece.asGeometryCollection() if piece.type() != dimension or piece.isMultipart() else [piece]:
            if single.type() == dimension and not single.isEmpty():
                moved = QgsGeometry(single)
                moved.translate(-width * world, 0.0)
                parts.append(moved)
    if not parts:
        return geometry
    return parts[0] if len(parts) == 1 else QgsGeometry.collectGeometry(parts)


def wrap_antimeridian(geometry: QgsGeometry) -> QgsGeometry:
    """A lon/lat geometry with every part inside -180..180, cut at the antimeridian."""
    return wrap_into_world(geometry, -180.0, 180.0)


def cut_at_map_edge(
    geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem, context: Optional[QgsCoordinateTransformContext] = None
) -> QgsGeometry:
    """`geometry` (in the map CRS `crs`) cut where the antimeridian falls on that map, so reprojecting it to
    lon/lat can't wrap its far side round the world. Needs a map whose x repeats every world (Web Mercator, plate
    carree, ...): the antimeridian is where lon 180 and lon -180 fall, and a shape inside them is left alone."""
    if geometry is None or geometry.isNull() or geometry.isEmpty() or not crs.isValid() or crs.isGeographic():
        return geometry  # a lon/lat map is handled by wrap_antimeridian
    try:
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), crs, context or QgsCoordinateTransformContext()
        )
        east = transform.transform(QgsPointXY(180.0, 0.0)).x()
        west = transform.transform(QgsPointXY(-180.0, 0.0)).x()
        middle = transform.transform(QgsPointXY(0.0, 0.0)).x()
    except Exception:  # noqa: BLE001 - QGIS raises its own exception types from a transform that can't be made
        return geometry
    if not all(math.isfinite(x) for x in (east, west, middle)) or not west < middle < east:
        return geometry  # a map whose x doesn't run from -180 to 180 (polar, UTM, ...): nothing to cut
    return wrap_into_world(geometry, west, east)
