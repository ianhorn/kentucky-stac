from typing import List, Optional

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtWidgets import QDockWidget, QTabWidget, QVBoxLayout, QWidget

from .aoi import AoiState
from .aoi_bar import AoiBar
from .catalog import split_collections
from .search_tab import SearchTab
from .stac import DEFAULT_BASE_URI, Collection
from .tasks import CollectionsTask


class KentuckyStacDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Kentucky STAC", parent)
        self.setObjectName("KentuckyStacDock")
        self.iface = iface
        self.base_uri = DEFAULT_BASE_URI
        self._task: Optional[CollectionsTask] = None
        self._loaded = False

        self.aoi_state = AoiState(self)
        self.aoi_bar = AoiBar(iface.mapCanvas(), self.aoi_state, iface.messageBar())

        bar = iface.messageBar()
        self.imagery_tab = SearchTab("imagery and DEM", "imagery", False, self.base_uri, self.aoi_state, bar)
        self.lidar_tab = SearchTab("point cloud", "lidar", True, self.base_uri, self.aoi_state, bar)
        for tab in (self.imagery_tab, self.lidar_tab):
            tab.reload_requested.connect(self.load_collections)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.imagery_tab, "Imagery / DEM")
        self.tabs.addTab(self.lidar_tab, "LiDAR Pointcloud")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(self.aoi_bar)
        layout.addWidget(self.tabs, 1)
        self.setWidget(content)

    def showEvent(self, event):
        super().showEvent(event)
        # Fetch on first show rather than at QGIS startup.
        if not self._loaded:
            self.load_collections()

    def load_collections(self):
        if self._task is not None:
            return
        self._loaded = True
        self.imagery_tab.set_loading()
        self.lidar_tab.set_loading()
        self._task = CollectionsTask(self.base_uri, self._on_collections)
        QgsApplication.taskManager().addTask(self._task)

    def _on_collections(self, collections: List[Collection], error: Optional[str]):
        self._task = None
        if error:
            self.imagery_tab.set_error(error)
            self.lidar_tab.set_error(error)
            self.iface.messageBar().pushMessage(
                "Kentucky STAC", f"Could not load collections: {error}", level=Qgis.MessageLevel.Warning
            )
            return
        imagery, lidar = split_collections(collections)
        self.imagery_tab.set_collections(imagery)
        self.lidar_tab.set_collections(lidar)

    def shutdown(self):
        """Called on plugin unload: drop in-flight tasks so their callbacks can't hit dead widgets."""
        self.aoi_bar.shutdown()
        self.imagery_tab.shutdown()
        self.lidar_tab.shutdown()
        if self._task is not None:
            self._task.cancel()
            self._task = None
