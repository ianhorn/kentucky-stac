from typing import List, Optional

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .catalog import split_collections
from .stac import DEFAULT_BASE_URI, Collection
from .tasks import CollectionsTask


def _describe(c: Collection) -> str:
    start, end = (list(c.interval) + [None, None])[:2] if c.interval else (None, None)
    span = f"{(start or '?')[:10]} to {(end or 'present')[:10]}"
    return f"{c.description}\n\n{span}" if c.description else span


class CollectionsTab(QWidget):
    """A tab that lets the user pick which collection(s) of one kind to search."""

    reload_requested = pyqtSignal()

    def __init__(self, what: str, parent=None):
        super().__init__(parent)
        self._what = what

        self.combo = QComboBox()
        self.combo.setEnabled(False)
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip("Reload the collection list from the STAC API")
        self.reload_button.clicked.connect(self.reload_requested)
        self.status = QLabel()
        self.status.setWordWrap(True)

        row = QHBoxLayout()
        row.addWidget(self.combo, 1)
        row.addWidget(self.reload_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Collection"))
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.set_loading()

    def set_loading(self):
        self.combo.clear()
        self.combo.setEnabled(False)
        self.reload_button.setEnabled(False)
        self.status.setText("Loading collections...")

    def set_error(self, message: str):
        self.combo.clear()
        self.combo.setEnabled(False)
        self.reload_button.setEnabled(True)
        self.status.setText(f"Could not load collections: {message}")

    def set_collections(self, collections: List[Collection]):
        self.combo.clear()
        self.reload_button.setEnabled(True)
        if not collections:
            self.status.setText(f"No {self._what} collections found.")
            return
        # Entry 0 searches every collection in this tab; the rest search one.
        self.combo.addItem(f"All {self._what}", [c.id for c in collections])
        for c in collections:
            self.combo.addItem(c.title_or_id, [c.id])
            self.combo.setItemData(self.combo.count() - 1, _describe(c), Qt.ItemDataRole.ToolTipRole)
        self.combo.setEnabled(True)
        self.status.setText(f"{len(collections)} collection{'s' if len(collections) != 1 else ''}")

    def selected_collection_ids(self) -> List[str]:
        return list(self.combo.currentData() or [])


class KentuckyStacDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Kentucky STAC", parent)
        self.setObjectName("KentuckyStacDock")
        self.iface = iface
        self.base_uri = DEFAULT_BASE_URI
        self._task: Optional[CollectionsTask] = None
        self._loaded = False

        self.imagery_tab = CollectionsTab("imagery and DEM")
        self.lidar_tab = CollectionsTab("point cloud")
        for tab in (self.imagery_tab, self.lidar_tab):
            tab.reload_requested.connect(self.load_collections)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.imagery_tab, "Imagery / DEM")
        self.tabs.addTab(self.lidar_tab, "LiDAR Pointcloud")
        self.setWidget(self.tabs)

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
        """Called on plugin unload: drop any in-flight task so its callback can't hit a dead widget."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
