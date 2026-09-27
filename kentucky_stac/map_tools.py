"""Map tools that set the area of interest: draw a point/line/polygon, or pick existing features."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication

from .aoi import AoiState, union_geometries
from .aoi_layers import add_aoi_feature

_LINE = QColor(0, 160, 220, 255)
_FILL = QColor(0, 200, 255, 60)
_CLICK_TOLERANCE_PX = 5
_LABELS = {
    Qgis.GeometryType.Point: "Point AOI",
    Qgis.GeometryType.Line: "Line AOI",
    Qgis.GeometryType.Polygon: "Polygon AOI",
}


def _style(band: QgsRubberBand) -> QgsRubberBand:
    band.setColor(_LINE)
    band.setFillColor(_FILL)
    band.setWidth(2)
    return band


def _notify(message_bar, text: str, level=None):
    if message_bar is not None:
        message_bar.pushMessage("Kentucky STAC", text, level=level or Qgis.MessageLevel.Info, duration=6)


class DrawAoiTool(QgsMapTool):
    """Left-click adds a vertex, right-click finishes, Backspace removes the last vertex, Esc cancels.
    A point AOI finishes on the first click."""

    def __init__(self, canvas, state: AoiState, geometry_type, message_bar=None):
        super().__init__(canvas)
        self._state = state
        self._type = geometry_type
        self._bar = message_bar
        self._points: List[QgsPointXY] = []
        self._band = _style(QgsRubberBand(canvas, Qgis.GeometryType.Line))
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._points.append(QgsPointXY(event.mapPoint()))
            if self._type == Qgis.GeometryType.Point:
                self._finish()
            else:
                self._redraw(None)
        elif event.button() == Qt.MouseButton.RightButton:
            self._finish()

    def canvasMoveEvent(self, event):
        if self._points:
            self._redraw(QgsPointXY(event.mapPoint()))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._reset()
        elif event.key() == Qt.Key.Key_Backspace and self._points:
            self._points.pop()
            self._redraw(None)
        else:
            super().keyPressEvent(event)

    def deactivate(self):
        self._reset()
        super().deactivate()

    def cleanup(self):
        self._reset()
        scene = self.canvas().scene()
        if scene is not None:
            scene.removeItem(self._band)

    def _reset(self):
        self._points = []
        self._band.reset(Qgis.GeometryType.Line)

    def _redraw(self, cursor: Optional[QgsPointXY]):
        pts = self._points + ([cursor] if cursor is not None else [])
        if self._type == Qgis.GeometryType.Polygon and len(pts) >= 3:
            geometry = QgsGeometry.fromPolygonXY([pts])
        else:
            geometry = QgsGeometry.fromPolylineXY(pts) if len(pts) >= 2 else QgsGeometry.fromPointXY(pts[0])
        self._band.setToGeometry(geometry, None)

    def _finish(self):
        pts = self._points
        needed = {Qgis.GeometryType.Point: 1, Qgis.GeometryType.Line: 2, Qgis.GeometryType.Polygon: 3}[self._type]
        if len(pts) < needed:
            return  # not enough vertices yet; keep drawing (Esc cancels)
        if self._type == Qgis.GeometryType.Point:
            geometry = QgsGeometry.fromPointXY(pts[0])
        elif self._type == Qgis.GeometryType.Line:
            geometry = QgsGeometry.fromPolylineXY(pts)
        else:
            geometry = QgsGeometry.fromPolygonXY([pts])
        crs = self.canvas().mapSettings().destinationCrs()
        label = _LABELS[self._type]
        self._reset()

        self._state.set(geometry, crs, label)
        try:
            add_aoi_feature(geometry, crs, label)
        except Exception as e:  # the AOI itself is set; only the scratch-layer record failed
            _notify(self._bar, f"Drew the AOI, but could not save it to a layer: {e}", Qgis.MessageLevel.Warning)


def collect_geometries(
    layers,
    rect: QgsRectangle,
    canvas_crs: QgsCoordinateReferenceSystem,
    transform_context: QgsCoordinateTransformContext,
) -> Tuple[List[QgsGeometry], Dict[QgsVectorLayer, List[int]]]:
    """Geometries (in the canvas CRS) of every feature in `layers` that intersects `rect`, plus the
    matching feature ids per layer."""
    geometries: List[QgsGeometry] = []
    hits: Dict[QgsVectorLayer, List[int]] = {}
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
            continue
        to_layer = QgsCoordinateTransform(canvas_crs, layer.crs(), transform_context)
        to_canvas = QgsCoordinateTransform(layer.crs(), canvas_crs, transform_context)
        layer_rect = to_layer.transformBoundingBox(rect)
        probe = QgsGeometry.fromRect(layer_rect)
        request = QgsFeatureRequest().setFilterRect(layer_rect).setSubsetOfAttributes([])
        for feature in layer.getFeatures(request):
            geometry = feature.geometry()
            if geometry.isNull() or not geometry.intersects(probe):
                continue
            geometry = QgsGeometry(geometry)
            geometry.transform(to_canvas)
            geometries.append(geometry)
            hits.setdefault(layer, []).append(feature.id())
    return geometries, hits


class SelectFeaturesTool(QgsMapTool):
    """Click, or drag a box, over existing features; their union becomes the AOI. Features in the
    visible vector layers are picked, and get QGIS's normal selection highlight."""

    def __init__(self, canvas, state: AoiState, message_bar=None):
        super().__init__(canvas)
        self._state = state
        self._bar = message_bar
        self._start = None  # QPoint, pixel coordinates
        self._band = _style(QgsRubberBand(canvas, Qgis.GeometryType.Polygon))
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.pixelPoint()

    def canvasMoveEvent(self, event):
        if self._start is not None:
            rect = QgsRectangle(self.toMapCoordinates(self._start), self.toMapCoordinates(event.pixelPoint()))
            self._band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        start, end = self._start, event.pixelPoint()
        self._start = None
        self._band.reset(Qgis.GeometryType.Polygon)
        if abs(end.x() - start.x()) < _CLICK_TOLERANCE_PX and abs(end.y() - start.y()) < _CLICK_TOLERANCE_PX:
            tolerance = _CLICK_TOLERANCE_PX * self.canvas().mapUnitsPerPixel()
            center = QgsPointXY(event.mapPoint())
            rect = QgsRectangle(center.x() - tolerance, center.y() - tolerance, center.x() + tolerance, center.y() + tolerance)
        else:
            rect = QgsRectangle(self.toMapCoordinates(start), self.toMapCoordinates(end))
        self.select_in_rect(rect)

    def deactivate(self):
        self._start = None
        self._band.reset(Qgis.GeometryType.Polygon)
        super().deactivate()

    def cleanup(self):
        self._band.reset(Qgis.GeometryType.Polygon)
        scene = self.canvas().scene()
        if scene is not None:
            scene.removeItem(self._band)

    def select_in_rect(self, rect: QgsRectangle) -> bool:
        """Set the AOI from the features under `rect` (canvas CRS). Returns True if any were found."""
        from qgis.core import QgsProject

        canvas = self.canvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        layers = canvas.layers()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            geometries, hits = collect_geometries(layers, rect, canvas_crs, QgsProject.instance().transformContext())
            union, ignored = union_geometries(geometries)
            if union is None:
                _notify(
                    self._bar,
                    "No feature found there. Click directly on a feature, or drag a box over one.",
                )
                return False
            for layer in layers:
                if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
                    layer.selectByIds(hits.get(layer, []))
            count = len(geometries) - ignored
            description = f"AOI from {count} selected feature{'s' if count != 1 else ''}"
            if ignored:
                description += f" (ignored {ignored} lower-dimension feature{'s' if ignored != 1 else ''})"
            self._state.set(union, canvas_crs, description)
            return True
        except Exception as e:
            _notify(self._bar, f"Could not use the selected features as an AOI: {e}", Qgis.MessageLevel.Warning)
            return False
        finally:
            QApplication.restoreOverrideCursor()
