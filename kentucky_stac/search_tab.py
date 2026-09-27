import os
from datetime import datetime
from typing import Dict, List, Optional

from qgis.core import Qgis, QgsApplication, QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .aoi import AoiState
from .catalog import format_size, primary_asset
from .downloads import DownloadJob, DownloadManager, SizesTask, plan_downloads
from .layers import AddLayersTask, LayerSpec, already_on_map, layer_specs, local_specs
from .mosaic import BuildMosaicsTask, plan_mosaics
from .results_layer import select_results, show_results
from .server_layers import add_streaming_layer, styles_for
from .stac import Collection, Item, SearchQuery
from .tasks import PAGE_SIZE, SearchTask

_COLUMNS = ["Tile", "Collection", "Date", "Size"]
CONFIRM_ABOVE = 25  # ask before adding more layers than this at once


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
        self._add_task: Optional[AddLayersTask] = None
        self._size_task: Optional[SizesTask] = None
        self._mosaic_task: Optional[BuildMosaicsTask] = None
        self._download: Optional[DownloadManager] = None
        self._download_files: List[str] = []  # every file this download run should end up with
        self._download_bboxes: dict = {}  # path -> the tile's catalog footprint
        self._pending_notes: List[str] = []
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

        # Statewide streaming layers come from the tile server's per-collection mosaics, so they
        # only exist for raster collections (imagery/DEM), not point clouds.
        self.stream_style: Optional[QComboBox] = None
        self.stream_button: Optional[QPushButton] = None
        self._stream_collection: Optional[str] = None
        if not lidar:
            self.stream_style = QComboBox()
            self.stream_button = QPushButton("Add streaming layer")
            self.stream_button.clicked.connect(self.add_streaming)
            self.combo.currentIndexChanged.connect(self._update_stream_controls)

        self.results_status = QLabel()
        self.results_status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(_COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)

        self.add_button = QPushButton("Add selected to map")
        self.add_button.setToolTip("Select tiles in the list above (Ctrl+A selects all)")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_to_map)

        self.download_button = QPushButton("Download selected...")
        self.download_button.setToolTip("Save the selected tiles to a folder")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)
        # Point clouds have no VRT equivalent, so only the imagery/DEM tab gets a mosaic button.
        self.mosaic_button: Optional[QPushButton] = None
        if not lidar:
            self.mosaic_button = QPushButton("Add as mosaic (VRT)...")
            self.mosaic_button.setToolTip(
                "Stitch the selected tiles into one virtual raster, one per collection "
                "(select two or more tiles)"
            )
            self.mosaic_button.setEnabled(False)
            self.mosaic_button.clicked.connect(self.add_mosaic)
        self.add_when_done = QCheckBox("Add downloaded files to the map")
        self.add_when_done.setChecked(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_download)

        row = QHBoxLayout()
        row.addWidget(self.combo, 1)
        row.addWidget(self.reload_button)

        actions = QHBoxLayout()
        actions.addWidget(self.add_button, 1)
        actions.addWidget(self.download_button, 1)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Collection"))
        layout.addLayout(row)
        layout.addWidget(self.status)
        if self.stream_button is not None:
            stream_row = QHBoxLayout()
            stream_row.addWidget(self.stream_style, 1)
            stream_row.addWidget(self.stream_button)
            layout.addLayout(stream_row)
        layout.addWidget(self.search_button)
        layout.addWidget(self.results_status)
        layout.addWidget(self.tree, 1)
        layout.addLayout(actions)
        if self.mosaic_button is not None:
            layout.addWidget(self.mosaic_button)
        layout.addWidget(self.add_when_done)
        layout.addLayout(progress_row)

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

    # ---- streaming layers -----------------------------------------------------------------

    def _update_stream_controls(self, *_):
        """Offer the rendering styles of the selected collection (one collection only)."""
        if self.stream_button is None:
            return
        ids = self.selected_collection_ids()
        collection = ids[0] if len(ids) == 1 else None
        if collection != self._stream_collection:
            self._stream_collection = collection
            previous = self.stream_style.currentData()
            self.stream_style.clear()
            for style in styles_for(collection) if collection else []:
                self.stream_style.addItem(style.label, style.key)
            index = self.stream_style.findData(previous)
            if index >= 0:
                self.stream_style.setCurrentIndex(index)
        available = self.stream_style.count() > 0
        self.stream_style.setEnabled(available)
        self.stream_button.setEnabled(available)
        self.stream_button.setToolTip(
            "Stream the whole collection from the tile server as a map layer"
            if available
            else "Pick a single imagery or DEM collection above to stream it"
        )

    def add_streaming(self):
        ids = self.selected_collection_ids()
        if len(ids) != 1:
            return
        style = next((s for s in styles_for(ids[0]) if s.key == self.stream_style.currentData()), None)
        if style is None:
            return
        layer, message = add_streaming_layer(ids[0], self.combo.currentText(), style)
        (self._info if layer is not None or "already" in message else self._warn)(message)

    def _update_search_enabled(self):
        self._update_stream_controls()
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
                self._kind, f"Ky STAC results ({self._what})", self._items, self._lidar
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
        self._update_add_enabled()

    def _busy(self) -> bool:
        return any(t is not None for t in (self._add_task, self._size_task, self._mosaic_task, self._download))

    def _update_add_enabled(self):
        enabled = bool(self.tree.selectedItems()) and not self._busy()
        self.add_button.setEnabled(enabled)
        self.download_button.setEnabled(enabled)
        if self.mosaic_button is not None:
            self.mosaic_button.setEnabled(enabled)

    # ---- add to map -----------------------------------------------------------------------

    def add_to_map(self):
        if self._busy():
            return
        items = self.selected_items()
        specs, skipped = layer_specs(items, self._lidar)
        duplicates = already_on_map(specs)
        specs = [s for s in specs if s not in duplicates]

        notes = []
        if skipped:
            n = len(skipped)
            notes.append(
                f"{n} tile{'s are' if n != 1 else ' is'} plain LAZ/LAS, which QGIS can't stream; "
                "download to view"
            )
        if duplicates:
            notes.append(f"{len(duplicates)} already on the map")
        if not specs:
            self._info("Nothing to add: " + "; ".join(notes) + "." if notes else "Nothing to add.")
            return
        if len(specs) > CONFIRM_ABOVE:
            answer = QMessageBox.question(
                self, "Add many layers", f"Add {len(specs)} layers to the map? Loading them may take a while."
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._run_add(specs, notes)

    def _run_add(self, specs, notes: List[str]):
        self._pending_notes = notes
        self._add_task = AddLayersTask(specs, self._on_layers_added)
        self._update_add_enabled()
        self.add_button.setText(f"Adding {len(specs)} layer{'s' if len(specs) != 1 else ''}...")
        QgsApplication.taskManager().addTask(self._add_task)

    def _on_layers_added(self, added: int, failed: list):
        self._add_task = None
        self.add_button.setText("Add selected to map")
        self._update_add_enabled()
        parts = [f"Added {added} layer{'s' if added != 1 else ''}"] + self._pending_notes
        if failed:
            first = f"{failed[0][0]}: {failed[0][1]}"
            parts.append(f"{len(failed)} failed to load (first: {first})")
        text = "; ".join(parts) + "."
        if failed:
            self._warn(text)
        else:
            self._info(text)

    # ---- mosaic ---------------------------------------------------------------------------

    def add_mosaic(self):
        if self._busy():
            return
        items = self.selected_items()
        if len(items) < 2:
            self._info("Select two or more tiles to build a mosaic.")
            return
        settings = QgsSettings()
        folder = QFileDialog.getExistingDirectory(
            self, "Save the mosaic (.vrt) to", str(settings.value("kentucky_stac/mosaic_dir", "") or "")
        )
        if not folder:
            return
        settings.setValue("kentucky_stac/mosaic_dir", folder)
        self.start_mosaic(items, folder)

    def start_mosaic(self, items: List[Item], folder: str):
        """Build one VRT per collection among `items` in `folder`, then add them to the map."""
        specs, left_out = plan_mosaics(items, folder, datetime.now().strftime("%H%M%S"))
        if not specs:
            self._info("A mosaic needs two or more tiles from the same collection.")
            return
        notes = [f"saved to {folder}"]
        if left_out:
            n = len(left_out)
            notes.append(f"{n} tile{'s' if n != 1 else ''} left out (alone in {'their' if n != 1 else 'its'} collection)")
        self._pending_notes = notes
        self.mosaic_button.setText("Building mosaic...")
        self._mosaic_task = BuildMosaicsTask(specs, self._on_mosaics_built)
        self._update_add_enabled()
        QgsApplication.taskManager().addTask(self._mosaic_task)

    def _on_mosaics_built(self, built: list, failed: list):
        self._mosaic_task = None
        self.mosaic_button.setText("Add as mosaic (VRT)...")
        if failed:
            first = f"{failed[0][0]}: {failed[0][1]}"
            self._warn(f"{len(failed)} mosaic{'s' if len(failed) != 1 else ''} failed (first: {first}).")
        if not built:
            self._update_add_enabled()
            return
        specs = [LayerSpec(m.name, m.vrt_path, "gdal", m.bbox) for m in built]
        self._run_add(specs, list(self._pending_notes))

    # ---- download -------------------------------------------------------------------------

    def download_selected(self):
        if self._busy():
            return
        items = self.selected_items()
        if not items:
            return
        settings = QgsSettings()
        folder = QFileDialog.getExistingDirectory(
            self, "Download tiles to", str(settings.value("kentucky_stac/download_dir", "") or "")
        )
        if not folder:
            return
        settings.setValue("kentucky_stac/download_dir", folder)
        self.start_download(items, folder)

    def start_download(self, items: List[Item], folder: str):
        """Plan the download, look up file sizes, confirm the total, then download."""
        jobs, missing = plan_downloads(items, self._lidar, folder)
        self._download_files = [j.dest for j in jobs]
        self._download_bboxes = {j.dest: j.bbox for j in jobs}
        todo = [j for j in jobs if not os.path.exists(j.dest)]
        notes = []
        if missing:
            notes.append(f"{len(missing)} tile{'s have' if len(missing) != 1 else ' has'} nothing to download")
        if len(jobs) - len(todo):
            notes.append(f"{len(jobs) - len(todo)} already in that folder")
        self._pending_notes = notes
        if not todo:
            self._info("Nothing new to download: " + "; ".join(notes) + "." if notes else "Nothing to download.")
            self._finish_download_run([], [], False)
            return

        self.results_status.setText("Checking file sizes...")
        self._size_task = SizesTask([j.url for j in todo], lambda sizes: self._on_sizes(todo, sizes))
        self._update_add_enabled()
        QgsApplication.taskManager().addTask(self._size_task)

    def _on_sizes(self, todo: List[DownloadJob], sizes: Dict[str, Optional[int]]):
        self._size_task = None
        self._show_sizes(sizes)
        self.results_status.setText(self._results_message or "")
        known = [sizes.get(j.url) for j in todo]
        total = sum(s for s in known if s)
        unknown = sum(1 for s in known if not s)
        size_text = format_size(total) if total else "unknown size"
        if total and unknown:
            size_text += f" plus {unknown} of unknown size"
        if not self._confirm_download(f"Download {len(todo)} file{'s' if len(todo) != 1 else ''} ({size_text})?"):
            self._update_add_enabled()
            return
        self._begin_download(todo, sizes)

    def _confirm_download(self, text: str) -> bool:
        return QMessageBox.question(self, "Download tiles", text) == QMessageBox.StandardButton.Yes

    def _begin_download(self, jobs: List[DownloadJob], sizes: Dict[str, Optional[int]]):
        self._download = DownloadManager(jobs, sizes, parent=self)
        self._download.progress.connect(self._on_download_progress)
        self._download.finished.connect(self._finish_download_run)
        self.progress.setValue(0)
        self.progress.setFormat(f"0 of {len(jobs)} files - %p%")
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        self._update_add_enabled()
        self._download.start()

    def _on_download_progress(self, done: int, total: int, percent: int):
        self.progress.setValue(percent)
        self.progress.setFormat(f"{done} of {total} files - %p%")

    def cancel_download(self):
        if self._download is not None:
            self.cancel_button.setEnabled(False)
            self._download.cancel()

    def _finish_download_run(self, completed: list, failed: list, cancelled: bool):
        manager, self._download = self._download, None
        if manager is not None:
            manager.deleteLater()
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        self._update_add_enabled()

        notes = list(self._pending_notes)
        if completed or failed or cancelled:
            head = f"Downloaded {len(completed)} file{'s' if len(completed) != 1 else ''}"
            if cancelled:
                head += " (cancelled)"
            parts = [head] + notes
            if failed:
                parts.append(f"{len(failed)} failed (first: {failed[0][0]}: {failed[0][1]})")
            (self._warn if failed else self._info)("; ".join(parts) + ".")
        if cancelled or not self.add_when_done.isChecked():
            return
        # Add every planned file that is now on disk, including ones downloaded on an earlier run.
        specs = local_specs([f for f in self._download_files if os.path.exists(f)], self._download_bboxes)
        specs = [s for s in specs if s not in already_on_map(specs)]
        if specs:
            self._run_add(specs, [])

    def _show_sizes(self, sizes: Dict[str, Optional[int]]):
        for row, item in enumerate(self._items):
            asset = primary_asset(item, self._lidar)
            size = sizes.get(asset.href) if asset else None
            if size:
                self.tree.topLevelItem(row).setText(3, format_size(size))
        self.tree.resizeColumnToContents(3)

    def selected_items(self) -> List[Item]:
        """The tiles currently selected in the results list."""
        rows = sorted(self.tree.indexOfTopLevelItem(i) for i in self.tree.selectedItems())
        return [self._items[r] for r in rows if r < len(self._items)]

    def _warn(self, text: str):
        if self._bar is not None:
            self._bar.pushMessage("Kentucky STAC", text, level=Qgis.MessageLevel.Warning, duration=8)

    def _info(self, text: str):
        if self._bar is not None:
            self._bar.pushMessage("Kentucky STAC", text, level=Qgis.MessageLevel.Info, duration=8)

    def shutdown(self):
        for attr in ("_task", "_add_task", "_size_task", "_mosaic_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                setattr(self, attr, None)
        if self._download is not None:
            self._download.cancel()
            self._download = None
