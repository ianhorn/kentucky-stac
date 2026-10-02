from typing import List, Optional, Tuple

from qgis.core import Qgis, QgsApplication, QgsSettings
from qgis.PyQt.QtWidgets import QDialog, QDockWidget, QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget

from .aoi import AoiState
from .aoi_bar import AoiBar
from .catalog import split_collections
from .search_tab import SearchTab
from .sources import ApiSource, deserialize_sources, serialize_sources
from .feedback_dialog import FeedbackDialog, open_help
from .sources_dialog import SourcesDialog
from .stac import Collection
from .tasks import CollectionsTask

SOURCES_SETTINGS_KEY = "kentucky_stac/api_sources"


class KentuckyStacDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Kentucky STAC", parent)
        self.setObjectName("KentuckyStacDock")
        self.iface = iface
        self.sources: List[ApiSource] = deserialize_sources(QgsSettings().value(SOURCES_SETTINGS_KEY, ""))
        self._task: Optional[CollectionsTask] = None
        self._loaded = False

        self.aoi_state = AoiState(self)
        self.aoi_bar = AoiBar(iface.mapCanvas(), self.aoi_state, iface.messageBar())

        # Which STAC API(s) are searched -- the built-in KyFromAbove catalog by default, plus any
        # "bring your own" API (see sources_dialog.py). One compact row, since dock space is tight.
        self.sources_label = QLabel()
        self.sources_label.setWordWrap(True)
        self.sources_button = QPushButton("Sources...")
        self.sources_button.setToolTip("Search another STAC API, from STAC Index or by URL (experimental)")
        self.sources_button.clicked.connect(self.edit_sources)
        # Feedback / Help share the row rather than adding one: same pair as the ArcGIS Pro add-ins.
        self.feedback_button = QPushButton("Feedback")
        self.feedback_button.setToolTip("Report a bug, request a feature, or send feedback directly.")
        self.feedback_button.clicked.connect(lambda: FeedbackDialog(self).exec())
        self.help_button = QPushButton("Help")
        self.help_button.setToolTip("Open the Kentucky STAC documentation site.")
        self.help_button.clicked.connect(lambda: open_help(self))
        sources_row = QHBoxLayout()
        sources_row.addWidget(self.sources_label, 1)
        sources_row.addWidget(self.sources_button)
        sources_row.addWidget(self.feedback_button)
        sources_row.addWidget(self.help_button)

        bar = iface.messageBar()
        self.imagery_tab = SearchTab("imagery and DEM", "imagery", False, self.aoi_state, bar)
        self.lidar_tab = SearchTab("point cloud", "lidar", True, self.aoi_state, bar)
        for tab in (self.imagery_tab, self.lidar_tab):
            tab.reload_requested.connect(self.load_collections)
            tab.sources_changed.connect(self.set_sources)
            tab.set_sources(self.sources)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.imagery_tab, "Imagery / DEM")
        self.tabs.addTab(self.lidar_tab, "LiDAR Pointcloud")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(self.aoi_bar)
        layout.addLayout(sources_row)
        layout.addWidget(self.tabs, 1)
        self.setWidget(content)
        self._update_sources_label()

    def showEvent(self, event):
        super().showEvent(event)
        # Fetch on first show rather than at QGIS startup.
        if not self._loaded:
            self.load_collections()

    # ---- sources ---------------------------------------------------------------------------

    def _update_sources_label(self):
        names = ", ".join(s.name for s in self.sources)
        self.sources_label.setText(f"Source{'s' if len(self.sources) != 1 else ''}: {names}")
        self.sources_label.setToolTip("\n".join(f"{s.name}: {s.base_uri}" for s in self.sources))

    def edit_sources(self):
        dialog = SourcesDialog(self.sources, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.set_sources(dialog.sources)

    def set_sources(self, sources: List[ApiSource]):
        if sources == self.sources:
            return
        # A tile server URL change alone doesn't need the collections fetched again.
        reload = [(s.name, s.base_uri) for s in sources] != [(s.name, s.base_uri) for s in self.sources]
        self.sources = list(sources)
        QgsSettings().setValue(SOURCES_SETTINGS_KEY, serialize_sources(self.sources))
        self._update_sources_label()
        for tab in (self.imagery_tab, self.lidar_tab):
            tab.set_sources(self.sources)
        if reload:
            self.load_collections()

    # ---- collections -----------------------------------------------------------------------

    def load_collections(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._loaded = True
        self.imagery_tab.set_loading()
        self.lidar_tab.set_loading()
        self._task = CollectionsTask(self.sources, self._on_collections)
        QgsApplication.taskManager().addTask(self._task)

    def _on_collections(self, collections: List[Collection], errors: List[Tuple[str, str]]):
        self._task = None
        if errors:
            detail = "; ".join(f"{name}: {msg}" for name, msg in errors)
            self.iface.messageBar().pushMessage(
                "Kentucky STAC", f"Could not load collections from {detail}", level=Qgis.MessageLevel.Warning
            )
        if not collections and errors:
            message = "; ".join(f"{name}: {msg}" for name, msg in errors)
            self.imagery_tab.set_error(message)
            self.lidar_tab.set_error(message)
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
