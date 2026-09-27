from typing import List, Optional

from qgis.core import Qgis, QgsApplication, QgsProject
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .aoi import AoiState
from .catalog import format_size, primary_asset
from .results_layer import select_results, show_results
from .stac import Collection, Item, SearchQuery
from .tasks import PAGE_SIZE, SearchTask

_COLUMNS = ["Tile", "Collection", "Date", "Size"]


def _describe(c: Collection) -> str:
    start, end = (list(c.interval) + [None, None])[:2] if c.interval else (None, None)
    span = f"{(start or '?')[:10]} to {(end or 'present')[:10]}"
    return f"{c.description}\n\n{span}" if c.description else span


class SearchTab(QWidget):
    """Pick collection(s), search the current AOI, and list the matching tiles."""

    reload_requested = pyqtSignal()

    def __init__(self, what: str, kind: str, lidar: bool, base_uri: str, aoi_state: AoiState, message_bar=None, parent=None):
        super().__init__(parent)
        self._what = what
        self._kind = kind  # identifies this tab's results layer
        self._lidar = lidar
        self._base_uri = base_uri
        self._aoi = aoi_state
        self._bar = message_bar
        self._task: Optional[SearchTask] = None
        self._items: List[Item] = []
        self._fids: List[Optional[int]] = []
        self._has_collections = False
        self._results_message: Optional[str] = None

        self.combo = QComboBox()
        self.combo.setEnabled(False)
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip("Reload the collection list from the STAC API")
        self.reload_button.clicked.connect(self.reload_requested)
        self.status = QLabel()
        self.status.setWordWrap(True)

        self.search_button = QPushButton("Search area of interest")
        self.search_button.clicked.connect(self.search)

        self.results_status = QLabel()
        self.results_status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(_COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)

        row = QHBoxLayout()
        row.addWidget(self.combo, 1)
        row.addWidget(self.reload_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Collection"))
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(self.search_button)
        layout.addWidget(self.results_status)
        layout.addWidget(self.tree, 1)

        self._aoi.changed.connect(self._update_search_enabled)
        self.set_loading()

    # ---- collections ----------------------------------------------------------------------

    def set_loading(self):
        self._has_collections = False
        self.combo.clear()
        self.combo.setEnabled(False)
        self.reload_button.setEnabled(False)
        self.status.setText("Loading collections...")
        self._update_search_enabled()

    def set_error(self, message: str):
        self._has_collections = False
        self.combo.clear()
        self.combo.setEnabled(False)
        self.reload_button.setEnabled(True)
        self.status.setText(f"Could not load collections: {message}")
        self._update_search_enabled()

    def set_collections(self, collections: List[Collection]):
        self.combo.clear()
        self.reload_button.setEnabled(True)
        if not collections:
            self._has_collections = False
            self.status.setText(f"No {self._what} collections found.")
            self._update_search_enabled()
            return
        # Entry 0 searches every collection in this tab; the rest search one.
        self.combo.addItem(f"All {self._what}", [c.id for c in collections])
        for c in collections:
            self.combo.addItem(c.title_or_id, [c.id])
            self.combo.setItemData(self.combo.count() - 1, _describe(c), Qt.ItemDataRole.ToolTipRole)
        self.combo.setEnabled(True)
        self._has_collections = True
        self.status.setText(f"{len(collections)} collection{'s' if len(collections) != 1 else ''}")
        self._update_search_enabled()

    def selected_collection_ids(self) -> List[str]:
        return list(self.combo.currentData() or [])

    # ---- search ---------------------------------------------------------------------------

    def _update_search_enabled(self):
        ready = self._has_collections and self._aoi.has_aoi and self._task is None
        self.search_button.setEnabled(ready)
        if self._task is not None:
            hint = "Searching..."
        elif not self._has_collections:
            hint = "Waiting for collections."
        elif not self._aoi.has_aoi:
            hint = "Set an area of interest above to search."
        else:
            hint = ""
        self.search_button.setToolTip(hint)
        # A finished search's message stays put; the hint only fills the space before the first search.
        if self._results_message is None:
            self.results_status.setText(hint)

    def _set_results_message(self, text: Optional[str]):
        self._results_message = text
        if text is not None:
            self.results_status.setText(text)

    def search(self):
        if self._task is not None or not self._has_collections:
            return
        collection_ids = self.selected_collection_ids()
        try:
            intersects = self._aoi.geojson_geometry(QgsProject.instance().transformContext())
        except Exception as e:
            self._warn(f"Could not use the area of interest: {e}")
            return
        if not collection_ids or intersects is None:
            return

        self._clear_results()
        self._set_results_message("Searching...")
        query = SearchQuery(collections=collection_ids, intersects=intersects, limit=PAGE_SIZE)
        self._task = SearchTask(self._base_uri, query, self._on_results)
        self._update_search_enabled()
        QgsApplication.taskManager().addTask(self._task)

    def _on_results(self, items: List[Item], matched: Optional[int], truncated: bool, error: Optional[str]):
        self._task = None
        if error:
            self._set_results_message(f"Search failed: {error}")
            self._warn(f"Search failed: {error}")
            self._update_search_enabled()
            return
        self._items = sorted(items, key=lambda i: (i.collection or "", i.id))
        if not self._items:
            self._set_results_message("No tiles intersect the area of interest.")
            self._update_search_enabled()
            return

        try:
            self._fids = show_results(
                self._kind, f"Kentucky STAC results ({self._what})", self._items, self._lidar
            )
        except Exception as e:
            self._fids = [None] * len(self._items)
            self._warn(f"Found tiles, but could not draw them on the map: {e}")

        for item in self._items:
            asset = primary_asset(item, self._lidar)
            row = QTreeWidgetItem(
                [item.id, item.collection or "", (item.datetime or "")[:10], format_size(asset.file_size) if asset else ""]
            )
            self.tree.addTopLevelItem(row)
        for column in range(len(_COLUMNS)):
            self.tree.resizeColumnToContents(column)

        count = len(self._items)
        text = f"{count} tile{'s' if count != 1 else ''} found"
        if truncated:
            text += f" (showing the first {count}" + (f" of {matched}" if matched else "") + "; narrow the area to see the rest)"
        self._set_results_message(text)
        self._update_search_enabled()

    def _clear_results(self):
        self._items = []
        self._fids = []
        self.tree.clear()
        select_results(self._kind, [])

    def _on_selection_changed(self):
        rows = [self.tree.indexOfTopLevelItem(i) for i in self.tree.selectedItems()]
        select_results(self._kind, [self._fids[r] for r in rows if r < len(self._fids) and self._fids[r] is not None])

    def selected_items(self) -> List[Item]:
        """The tiles currently selected in the results list."""
        rows = sorted(self.tree.indexOfTopLevelItem(i) for i in self.tree.selectedItems())
        return [self._items[r] for r in rows if r < len(self._items)]

    def _warn(self, text: str):
        if self._bar is not None:
            self._bar.pushMessage("Kentucky STAC", text, level=Qgis.MessageLevel.Warning, duration=8)

    def shutdown(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
