"""Link the results footprints on the map back to the results list.

The list already drives the map: selecting a tile selects its footprint in the results layer. This is
the other direction:

* selecting footprints on the map (QGIS's Select tools on the results layer) selects those tiles in the list;
* resting the mouse over the map tints the tile(s) under it in the list, and scrolls to the first.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QEvent, QObject
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import QAbstractItemView, QTreeWidget

from .results_layer import KIND_PROPERTY, geojson_to_geometry
from .stac import Item

_HOVER_COLOR = QColor(255, 228, 140)  # warm yellow: distinct from the selection blue
_MIN_INTERVAL = 0.03  # seconds between hover lookups while the mouse moves
WGS84 = "EPSG:4326"


class MapResultsLink(QObject):
    def __init__(self, canvas, tree: QTreeWidget, kind: str, on_selection_applied: Callable[[], None], parent=None):
        super().__init__(parent)
        self._canvas = canvas
        self._tree = tree
        self._kind = kind
        self._on_applied = on_selection_applied
        self._fids: List[Optional[int]] = []
        self._shapes: List[Tuple[Optional[Tuple[float, float, float, float]], Optional[QgsGeometry]]] = []
        self._layer: Optional[QgsVectorLayer] = None
        self._hovered: List[int] = []
        self._last_lookup = 0.0
        self._transform: Optional[QgsCoordinateTransform] = None
        self._transform_crs = ""
        if canvas is not None:
            canvas.xyCoordinates.connect(self._on_map_move)
            canvas.viewport().installEventFilter(self)

    # ---- feeding it the current results ------------------------------------------------------------

    def set_results(self, items: Sequence[Item], fids: Sequence[Optional[int]]) -> None:
        self.clear_hover()
        self._fids = list(fids)
        self._shapes = []
        for item in items:
            geometry = geojson_to_geometry(item.geometry, item.bbox)
            box = geometry.boundingBox() if geometry is not None else None
            self._shapes.append(((box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()) if box else None, geometry))
        self._watch_layer()

    def clear(self) -> None:
        self.clear_hover()
        self._fids = []
        self._shapes = []

    def shutdown(self) -> None:
        self.clear_hover()
        self._unwatch_layer()
        if self._canvas is not None:
            try:
                self._canvas.xyCoordinates.disconnect(self._on_map_move)
                self._canvas.viewport().removeEventFilter(self)
            except (TypeError, RuntimeError):
                pass

    # ---- map selection -> list selection -----------------------------------------------------------

    def _results_layer(self) -> Optional[QgsVectorLayer]:
        for layer in QgsProject.instance().mapLayers().values():
            if layer.customProperty(KIND_PROPERTY) == self._kind:
                return layer
        return None

    def _watch_layer(self) -> None:
        layer = self._results_layer()
        if layer is self._layer:
            return
        self._unwatch_layer()
        if layer is not None:
            layer.selectionChanged.connect(self._on_layer_selection)
            self._layer = layer

    def _unwatch_layer(self) -> None:
        if self._layer is not None:
            try:
                self._layer.selectionChanged.disconnect(self._on_layer_selection)
            except (TypeError, RuntimeError):
                pass
        self._layer = None

    def _on_layer_selection(self, *_args) -> None:
        if self._layer is None or not self._fids:
            return
        try:
            selected = set(self._layer.selectedFeatureIds())
        except RuntimeError:  # the layer was deleted
            self._layer = None
            return
        wanted = {row for row, fid in enumerate(self._fids) if fid is not None and fid in selected}
        current = {self._tree.indexOfTopLevelItem(i) for i in self._tree.selectedItems()}
        if wanted == current:  # the change came from the list itself
            return
        self._tree.blockSignals(True)
        try:
            self._tree.clearSelection()
            first = None
            for row in sorted(wanted):
                item = self._tree.topLevelItem(row)
                if item is not None:
                    item.setSelected(True)
                    first = item if first is None else first
        finally:
            self._tree.blockSignals(False)
        if first is not None:
            self._tree.scrollToItem(first, QAbstractItemView.ScrollHint.EnsureVisible)
        self._on_applied()

    # ---- map hover -> list highlight ---------------------------------------------------------------

    def _to_wgs84(self, point: QgsPointXY) -> Optional[QgsPointXY]:
        crs = self._canvas.mapSettings().destinationCrs()
        if not crs.isValid():
            return None
        if self._transform is None or self._transform_crs != crs.authid() + crs.toWkt():
            self._transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(WGS84), QgsProject.instance())
            self._transform_crs = crs.authid() + crs.toWkt()
        try:
            return self._transform.transform(point)
        except Exception:
            return None

    def _on_map_move(self, point: QgsPointXY) -> None:
        if not self._shapes or not self._tree.isVisible():
            if self._hovered:
                self.clear_hover()
            return
        now = time.monotonic()
        if now - self._last_lookup < _MIN_INTERVAL:
            return
        self._last_lookup = now
        lonlat = self._to_wgs84(point)
        if lonlat is None:
            return
        x, y = lonlat.x(), lonlat.y()
        probe = QgsGeometry.fromPointXY(lonlat)
        rows = []
        for row, (box, geometry) in enumerate(self._shapes):
            if box is None or not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                continue
            if geometry is None or geometry.contains(probe):
                rows.append(row)
        self._highlight(rows)

    def _highlight(self, rows: List[int]) -> None:
        if rows == self._hovered:
            return
        self._paint(self._hovered, None)
        self._hovered = rows
        self._paint(rows, QBrush(_HOVER_COLOR))
        if rows:
            item = self._tree.topLevelItem(rows[0])
            if item is not None:
                self._tree.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)

    def _paint(self, rows: List[int], brush: Optional[QBrush]) -> None:
        for row in rows:
            item = self._tree.topLevelItem(row)
            if item is None:
                continue
            for column in range(self._tree.columnCount()):
                item.setBackground(column, brush if brush is not None else QBrush())

    def clear_hover(self) -> None:
        self._paint(self._hovered, None)
        self._hovered = []

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.Leave:  # the mouse left the map
            self.clear_hover()
        return False
