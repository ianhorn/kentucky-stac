from qgis.core import Qgis, QgsGeometry, QgsVectorLayer
from qgis.PyQt.QtWidgets import QGridLayout, QGroupBox, QLabel, QPushButton

from .aoi import AoiState
from .aoi_layers import add_aoi_feature
from .map_tools import DrawAoiTool, SelectFeaturesTool


class AoiBar(QGroupBox):
    """Shared "Area of interest" controls shown above the tabs."""

    def __init__(self, canvas, state: AoiState, message_bar=None, parent=None):
        super().__init__("Area of interest", parent)
        self._canvas = canvas
        self._state = state
        self._bar = message_bar

        self._tools = {
            "Point": DrawAoiTool(canvas, state, Qgis.GeometryType.Point, message_bar),
            "Line": DrawAoiTool(canvas, state, Qgis.GeometryType.Line, message_bar),
            "Polygon": DrawAoiTool(canvas, state, Qgis.GeometryType.Polygon, message_bar),
            "Select features": SelectFeaturesTool(canvas, state, message_bar),
        }
        self._tool_buttons = {}
        tips = {
            "Point": "Click the map to set a point",
            "Line": "Click to add vertices; right-click to finish, Backspace to undo, Esc to cancel",
            "Polygon": "Click to add vertices; right-click to finish, Backspace to undo, Esc to cancel",
            "Select features": "Click, or drag a box over, existing features to use their outline",
        }
        for name, tool in self._tools.items():
            button = QPushButton(name)
            button.setCheckable(True)
            button.setToolTip(tips[name])
            button.clicked.connect(lambda _checked, t=tool, b=button: self._activate(t, b))
            self._tool_buttons[name] = (button, tool)

        self._extent_button = QPushButton("Current extent")
        self._extent_button.setToolTip("Use the area currently shown in the map window")
        self._extent_button.clicked.connect(self.use_current_extent)

        self._clear_button = QPushButton("Clear")
        self._clear_button.setToolTip("Clear the area of interest and any feature selection")
        self._clear_button.clicked.connect(self.clear)

        self._status = QLabel()
        self._status.setWordWrap(True)

        grid = QGridLayout(self)
        grid.addWidget(self._tool_buttons["Point"][0], 0, 0)
        grid.addWidget(self._tool_buttons["Line"][0], 0, 1)
        grid.addWidget(self._tool_buttons["Polygon"][0], 0, 2)
        grid.addWidget(self._tool_buttons["Select features"][0], 1, 0)
        grid.addWidget(self._extent_button, 1, 1)
        grid.addWidget(self._clear_button, 1, 2)
        grid.addWidget(self._status, 2, 0, 1, 3)

        canvas.mapToolSet.connect(self._on_map_tool_set)
        state.changed.connect(self._update_status)
        self._update_status()

    def _activate(self, tool, button):
        self._canvas.setMapTool(tool)
        button.setChecked(self._canvas.mapTool() is tool)

    def _on_map_tool_set(self, new_tool, _old_tool):
        for button, tool in self._tool_buttons.values():
            button.setChecked(tool is new_tool)

    def _update_status(self):
        if self._state.has_aoi:
            self._status.setText(f"AOI ready: {self._state.description}")
        else:
            self._status.setText("No area of interest. Draw one, or select existing features.")

    def use_current_extent(self):
        """Set the AOI to the area the map window is showing (as a rectangle in the map CRS)."""
        settings = self._canvas.mapSettings()
        extent = settings.visibleExtent()
        if extent.isEmpty() or extent.isNull():
            self._notify("The map has no extent yet. Zoom to an area first.", Qgis.MessageLevel.Warning)
            return
        crs = settings.destinationCrs()
        geometry = QgsGeometry.fromRect(extent)
        label = "Map extent AOI"
        self._state.set(geometry, crs, label)
        try:
            add_aoi_feature(geometry, crs, label)
        except Exception as e:  # the AOI itself is set; only the scratch-layer record failed
            self._notify(f"Set the AOI, but could not save it to a layer: {e}", Qgis.MessageLevel.Warning)

    def _notify(self, text: str, level):
        if self._bar is not None:
            self._bar.pushMessage("Kentucky STAC", text, level=level, duration=6)

    def clear(self):
        self._state.clear()
        for layer in self._canvas.layers():
            if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
                layer.removeSelection()

    def shutdown(self):
        """Release the map tools and their rubber bands (plugin unload)."""
        try:
            self._canvas.mapToolSet.disconnect(self._on_map_tool_set)
        except TypeError:
            pass
        for _button, tool in self._tool_buttons.values():
            if self._canvas.mapTool() is tool:
                self._canvas.unsetMapTool(tool)
            tool.cleanup()
