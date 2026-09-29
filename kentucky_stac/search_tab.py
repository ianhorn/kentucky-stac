import os
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional

from qgis.core import Qgis, QgsApplication, QgsNetworkAccessManager, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QImage, QPixmap
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .aoi import AoiState, wkt_parts
from .catalog import format_size, primary_asset, thumbnail_href
from .downloads import DownloadJob, DownloadManager, SizesTask, plan_downloads
from .layers import AddLayersTask, LayerSpec, already_on_map, item_bbox, layer_specs, local_specs, union_bbox
from .mosaic import BuildMosaicsTask, plan_mosaics
from .pdal_clip import CropPointCloudsTask, clipped_path
from .results_layer import select_results, show_results
from .server_layers import RegisterMosaicsTask, add_search_layer, add_streaming_layer, plan_server_mosaics, styles_for
from .stac import Collection, Item, SearchQuery
from .tasks import PAGE_SIZE, SearchTask
from .vpc import build_vpc

_COLUMNS = ["Tile", "Collection", "Date", "Size", "Preview"]
_THUMBNAIL_SIZE = 64
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
        self._register_task: Optional[RegisterMosaicsTask] = None
        self._download: Optional[DownloadManager] = None
        self._clip_task: Optional[CropPointCloudsTask] = None
        self._clip_sources: dict = {}  # cropped path -> the original downloaded path it came from
        self._download_files: List[str] = []  # every file this download run should end up with
        self._download_bboxes: dict = {}  # path -> the tile's catalog footprint
        self._download_items: dict = {}  # path -> the originating STAC item, for building a VPC
        self._pending_notes: List[str] = []
        self._items: List[Item] = []
        self._fids: List[Optional[int]] = []
        self._has_collections = False
        self._results_message: Optional[str] = None

        # A checklist rather than a single-select dropdown, so a search can span any combination of
        # collections (e.g. two DEM phases together) -- styled after QGIS's own "Build Virtual
        # Raster" input-layers panel (a list of checkboxes plus Select All / Clear Selection).
        self.list = QListWidget()
        self.list.setEnabled(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setMaximumHeight(110)
        self.list.itemChanged.connect(self._on_collection_selection_changed)
        self.select_all_button = QPushButton("Select All")
        self.select_all_button.setEnabled(False)
        self.select_all_button.clicked.connect(self.select_all_collections)
        self.clear_selection_button = QPushButton("Clear Selection")
        self.clear_selection_button.setEnabled(False)
        self.clear_selection_button.clicked.connect(self.clear_collection_selection)
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip("Reload the collection list from the STAC API")
        self.reload_button.setEnabled(False)
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

        self.results_status = QLabel()
        self.results_status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(_COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._thumb_replies: List[QNetworkReply] = []

        self.add_button = QPushButton("Add selected to map")
        self.add_button.setToolTip("Select tiles in the list above (Ctrl+A selects all)")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_to_map)

        # Streams several selected tiles directly into one combined virtual point cloud layer, no
        # download involved -- the same VPC format as the download-then-combine path (vpc.py), just
        # pointed at each tile's remote href instead of a local file. Only COPC tiles can stream
        # (see layer_specs); plain LAZ/LAS is skipped, same restriction as "Add selected to map".
        self.vpc_button: Optional[QPushButton] = None
        if lidar:
            self.vpc_button = QPushButton("Add selected as VPC")
            self.vpc_button.setToolTip(
                "Combine the selected tiles into one virtual point cloud layer, streamed directly "
                "with no download (select two or more COPC tiles)"
            )
            self.vpc_button.setEnabled(False)
            self.vpc_button.clicked.connect(self.add_selected_as_vpc)

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
        # Registers a search restricted to the selected tiles' ids and streams that as an XYZ
        # layer -- a precise crop instead of the whole-state streaming layer, with no download and
        # no local stitching. Same raster-only restriction as the VRT mosaic.
        self.server_mosaic_button: Optional[QPushButton] = None
        if not lidar:
            self.server_mosaic_button = QPushButton("Add as server mosaic")
            self.server_mosaic_button.setToolTip(
                "Register the selected tiles as a mosaic on the tile server and stream it "
                "(one mosaic per collection; renders like the streaming layer above)"
            )
            self.server_mosaic_button.setEnabled(False)
            self.server_mosaic_button.clicked.connect(self.add_server_mosaic)
        # Crops each downloaded tile to the AOI's actual shape via PDAL (see pdal_clip.py) before
        # combining into a VPC / adding it, rather than the tiles' full rectangular extent. Only
        # makes sense for a polygon AOI -- a point/line AOI has no area to crop by.
        self.clip_to_aoi: Optional[QCheckBox] = None
        if lidar:
            self.clip_to_aoi = QCheckBox("Clip to area of interest")
            self.clip_to_aoi.setChecked(True)
        self.add_when_done = QCheckBox(
            "Add downloaded files to the map, combined into one virtual point cloud layer"
            if lidar
            else "Add downloaded files to the map"
        )
        self.add_when_done.setChecked(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_download)

        collection_buttons = QVBoxLayout()
        collection_buttons.addWidget(self.select_all_button)
        collection_buttons.addWidget(self.clear_selection_button)
        collection_buttons.addWidget(self.reload_button)
        collection_buttons.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(collection_buttons)

        actions = QHBoxLayout()
        actions.addWidget(self.add_button, 1)
        actions.addWidget(self.download_button, 1)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Collections"))
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
        if self.vpc_button is not None:
            layout.addWidget(self.vpc_button)
        if self.mosaic_button is not None:
            layout.addWidget(self.mosaic_button)
        if self.server_mosaic_button is not None:
            layout.addWidget(self.server_mosaic_button)
        if self.clip_to_aoi is not None:
            layout.addWidget(self.clip_to_aoi)
        layout.addWidget(self.add_when_done)
        layout.addLayout(progress_row)

        self._aoi.changed.connect(self._update_search_enabled)
        self.set_loading()

    # ---- collections ----------------------------------------------------------------------

    def _clear_collection_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        self.list.blockSignals(False)
        self.list.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.clear_selection_button.setEnabled(False)

    def set_loading(self):
        self._has_collections = False
        self._clear_collection_list()
        self.reload_button.setEnabled(False)
        self.status.setText("Loading collections...")
        self._update_search_enabled()

    def set_error(self, message: str):
        self._has_collections = False
        self._clear_collection_list()
        self.reload_button.setEnabled(True)
        self.status.setText(f"Could not load collections: {message}")
        self._update_search_enabled()

    def set_collections(self, collections: List[Collection]):
        self._clear_collection_list()
        self.reload_button.setEnabled(True)
        if not collections:
            self._has_collections = False
            self.status.setText(f"No {self._what} collections found.")
            self._update_search_enabled()
            return
        # All collections start checked, matching the old dropdown's "All <what>" default.
        self.list.blockSignals(True)
        for c in collections:
            item = QListWidgetItem(c.title_or_id)
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setToolTip(_describe(c))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setEnabled(True)
        self.select_all_button.setEnabled(True)
        self.clear_selection_button.setEnabled(True)
        self._has_collections = True
        self.status.setText(f"{len(collections)} collection{'s' if len(collections) != 1 else ''}")
        self._update_search_enabled()

    def selected_collection_ids(self) -> List[str]:
        return [
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _selected_collection_title(self, collection_id: str) -> str:
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == collection_id:
                return item.text()
        return collection_id

    def select_all_collections(self):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked)
        self.list.blockSignals(False)
        self._on_collection_selection_changed()

    def clear_collection_selection(self):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._on_collection_selection_changed()

    def _on_collection_selection_changed(self, *_):
        self._update_search_enabled()

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
        layer, message = add_streaming_layer(ids[0], self._selected_collection_title(ids[0]), style)
        (self._info if layer is not None or "already" in message else self._warn)(message)

    def _aoi_is_polygon(self) -> bool:
        return self._aoi.has_aoi and self._aoi.geometry.type() == Qgis.GeometryType.Polygon

    def _update_search_enabled(self):
        self._update_stream_controls()
        if self.clip_to_aoi is not None:
            polygon = self._aoi_is_polygon()
            self.clip_to_aoi.setEnabled(polygon)
            self.clip_to_aoi.setToolTip(
                ""
                if polygon
                else "Draw a Polygon AOI, or select polygon features, to clip point clouds to its "
                "shape (a point or line AOI has no area to clip to)"
            )
        has_selection = bool(self.selected_collection_ids())
        ready = self._has_collections and has_selection and self._aoi.has_aoi and self._task is None
        self.search_button.setEnabled(ready)
        if self._task is not None:
            hint = "Searching..."
        elif not self._has_collections:
            hint = "Waiting for collections."
        elif not has_selection:
            hint = "Check at least one collection above."
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
                [item.id, item.collection or "", (item.datetime or "")[:10], format_size(asset.file_size) if asset else "", ""]
            )
            self.tree.addTopLevelItem(row)
        for column in range(len(_COLUMNS) - 1):
            self.tree.resizeColumnToContents(column)
        self.tree.setColumnWidth(len(_COLUMNS) - 1, _THUMBNAIL_SIZE + 8)
        self._fetch_thumbnails()

        count = len(self._items)
        text = f"{count} tile{'s' if count != 1 else ''} found"
        if truncated:
            text += f" (showing the first {count}" + (f" of {matched}" if matched else "") + "; narrow the area to see the rest)"
        self._set_results_message(text)
        self._update_search_enabled()

    def _clear_results(self):
        self._cancel_thumbnail_fetches()
        self._items = []
        self._fids = []
        self.tree.clear()
        select_results(self._kind, [])

    def _fetch_thumbnails(self):
        """Populate each result row's Preview column with its STAC thumbnail asset, fetched
        asynchronously (QgsNetworkAccessManager, non-blocking) so a slow/failed fetch for one tile
        never holds up the others or the UI."""
        for row, item in enumerate(self._items):
            href = thumbnail_href(item, self._lidar)
            if not href:
                continue
            reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(href)))
            self._thumb_replies.append(reply)
            reply.finished.connect(lambda reply=reply, row=row: self._on_thumbnail_fetched(reply, row))

    def _on_thumbnail_fetched(self, reply: QNetworkReply, row: int):
        if reply in self._thumb_replies:
            self._thumb_replies.remove(reply)
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        data = bytes(reply.readAll()) if ok else b""
        reply.deleteLater()
        if not ok or row >= self.tree.topLevelItemCount():
            return
        image = QImage()
        if not image.loadFromData(data):
            return
        pixmap = QPixmap.fromImage(image).scaled(
            _THUMBNAIL_SIZE, _THUMBNAIL_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self.tree.topLevelItem(row).setIcon(len(_COLUMNS) - 1, QIcon(pixmap))

    def _cancel_thumbnail_fetches(self):
        for reply in self._thumb_replies:
            try:
                reply.finished.disconnect()
            except TypeError:
                pass
            reply.abort()
            reply.deleteLater()
        self._thumb_replies = []

    def _on_selection_changed(self):
        rows = [self.tree.indexOfTopLevelItem(i) for i in self.tree.selectedItems()]
        select_results(self._kind, [self._fids[r] for r in rows if r < len(self._fids) and self._fids[r] is not None])
        self._update_add_enabled()

    def _busy(self) -> bool:
        return any(
            t is not None
            for t in (
                self._add_task,
                self._size_task,
                self._mosaic_task,
                self._register_task,
                self._download,
                self._clip_task,
            )
        )

    def _update_add_enabled(self):
        enabled = bool(self.tree.selectedItems()) and not self._busy()
        self.add_button.setEnabled(enabled)
        self.download_button.setEnabled(enabled)
        if self.vpc_button is not None:
            self.vpc_button.setEnabled(enabled)
        if self.mosaic_button is not None:
            self.mosaic_button.setEnabled(enabled)
        if self.server_mosaic_button is not None:
            self.server_mosaic_button.setEnabled(enabled)

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

    def add_selected_as_vpc(self):
        """Combine the selected tiles into one virtual point cloud layer, streamed directly from
        their remote hrefs -- same VPC format as the download-then-combine path (see _add_as_vpc),
        no local file involved except the small .vpc index itself. Only COPC tiles can stream, same
        restriction as add_to_map's layer_specs()."""
        if self._busy():
            return
        items = self.selected_items()
        entries = []
        skipped = 0
        for item in items:
            asset = primary_asset(item, True)
            if asset is not None and asset.href and asset.is_copc:
                entries.append((item, asset.href))
            else:
                skipped += 1
        if len(entries) < 2:
            self._info("Select two or more streamable (COPC) tiles to combine into a virtual point cloud.")
            return
        settings = QgsSettings()
        folder = QFileDialog.getExistingDirectory(
            self, "Save the virtual point cloud (.vpc) to", str(settings.value("kentucky_stac/vpc_dir", "") or "")
        )
        if not folder:
            return
        settings.setValue("kentucky_stac/vpc_dir", folder)

        vpc_path = os.path.join(folder, f"kentucky_stac_pointcloud_{datetime.now().strftime('%H%M%S')}.vpc")
        count = build_vpc(entries, vpc_path)
        if count < 2:
            self._warn("Could not build a combined virtual point cloud (missing tile footprints); nothing added.")
            return
        bbox = union_bbox([b for b in (item_bbox(e[0]) for e in entries) if b is not None])
        spec = LayerSpec(f"Ky STAC point cloud ({count} tiles)", vpc_path, "vpc", bbox)
        notes = [f"streamed directly (no download), saved to {os.path.basename(vpc_path)}"]
        if skipped:
            notes.append(f"{skipped} tile{'s' if skipped != 1 else ''} plain LAZ/LAS, which QGIS can't stream; left out")
        self._run_add([spec], notes)

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

    # ---- server mosaic ----------------------------------------------------------------------

    def add_server_mosaic(self):
        if self._busy():
            return
        items = self.selected_items()
        groups = plan_server_mosaics(items)
        if not groups:
            self._info("Nothing to register: the selected tiles have no usable collection.")
            return
        self.server_mosaic_button.setText("Registering mosaic...")
        self._register_task = RegisterMosaicsTask(groups, self._on_server_mosaics_registered)
        self._update_add_enabled()
        QgsApplication.taskManager().addTask(self._register_task)

    def _on_server_mosaics_registered(self, built: list, failed: list):
        self._register_task = None
        self.server_mosaic_button.setText("Add as server mosaic")
        added, extra_notes = 0, []
        for group, search_id, style in built:
            layer, message = add_search_layer(search_id, group.collection_id, style, len(group.ids))
            if layer is not None:
                added += 1
            elif "already" not in message:
                extra_notes.append(message)

        parts = [f"Added {added} server mosaic{'s' if added != 1 else ''}"]
        if failed:
            first = f"{failed[0][0]}: {failed[0][1]}"
            parts.append(f"{len(failed)} failed to register (first: {first})")
        parts.extend(extra_notes)
        text = "; ".join(parts) + "."
        (self._warn if failed or extra_notes else self._info)(text)
        self._update_add_enabled()

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
        self._download_items = {j.dest: j.item for j in jobs}
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
        paths = [f for f in self._download_files if os.path.exists(f)]
        if not paths:
            return
        if self.clip_to_aoi is not None and self.clip_to_aoi.isChecked() and self._aoi_is_polygon():
            self._start_clip(paths)
            return
        self._add_downloaded(paths, [])

    def _start_clip(self, paths: List[str]):
        """Crop every downloaded tile to the AOI (see pdal_clip.py) before adding/combining."""
        try:
            aoi_geom = self._aoi.geometry_wgs84(QgsProject.instance().transformContext())
        except Exception as e:
            self._warn(f"Could not use the area of interest to clip; adding the unclipped tiles instead: {e}")
            self._add_downloaded(paths, [])
            return
        parts = wkt_parts(aoi_geom) if aoi_geom is not None else []
        if not parts:
            self._add_downloaded(paths, [])
            return
        out_dir = os.path.join(os.path.dirname(paths[0]), "clipped")
        jobs = [(p, clipped_path(p, out_dir)) for p in paths]
        self._clip_sources = {dst: src for src, dst in jobs}
        self._clip_task = CropPointCloudsTask(jobs, parts, self._on_clip_finished)
        self._update_add_enabled()
        QgsApplication.taskManager().addTask(self._clip_task)

    def _on_clip_finished(self, cropped: List[str], failed: list, tile_meta: dict):
        self._clip_task = None
        self._update_add_enabled()
        for dst in cropped:
            src = self._clip_sources.get(dst)
            original_item = self._download_items.get(src) if src is not None else None
            meta = tile_meta.get(dst)
            # The VPC's declared pc:count/bbox/geometry must reflect the actual cropped file, not
            # the original (uncropped) tile's -- see pdal_clip.real_metadata's docstring for the bug
            # this fixes.
            if original_item is not None and meta is not None:
                self._download_items[dst] = replace(
                    original_item,
                    bbox=meta["bbox"],
                    geometry=meta["geometry"],
                    properties={**original_item.properties, "pc:count": meta["count"]},
                )
            elif src in self._download_items:
                self._download_items[dst] = self._download_items[src]
            if meta is not None and meta["bbox"] is not None:
                self._download_bboxes[dst] = tuple(meta["bbox"])
            elif src in self._download_bboxes:
                self._download_bboxes[dst] = self._download_bboxes[src]
        notes = []
        if failed:
            first = f"{failed[0][0]}: {failed[0][1]}"
            notes.append(f"{len(failed)} tile{'s' if len(failed) != 1 else ''} left out (first: {first})")
        if not cropped:
            self._warn("; ".join(notes) + "." if notes else "Clipping produced no output; nothing added.")
            return
        self._add_downloaded(cropped, notes)

    def _add_downloaded(self, paths: List[str], notes: List[str]):
        if self._lidar and len(paths) >= 2:
            self._add_as_vpc(paths, notes)
            return
        specs = local_specs(paths, self._download_bboxes)
        specs = [s for s in specs if s not in already_on_map(specs)]
        if specs:
            self._run_add(specs, notes)
        elif notes:
            self._warn("; ".join(notes) + ".")

    def _add_as_vpc(self, paths: List[str], notes: Optional[List[str]] = None):
        """Combine several downloaded point cloud files into one Virtual Point Cloud layer,
        instead of adding each one separately."""
        notes = list(notes or [])
        entries = [(self._download_items[p], p) for p in paths if self._download_items.get(p) is not None]
        if len(entries) < 2:
            specs = local_specs(paths, self._download_bboxes)
            specs = [s for s in specs if s not in already_on_map(specs)]
            if specs:
                self._run_add(specs, notes)
            return
        folder = os.path.dirname(paths[0])
        vpc_path = os.path.join(folder, f"kentucky_stac_pointcloud_{datetime.now().strftime('%H%M%S')}.vpc")
        count = build_vpc(entries, vpc_path)
        if count < 2:
            self._warn("Could not build a combined virtual point cloud (missing tile footprints); nothing added.")
            return
        bbox = union_bbox([b for b in (item_bbox(e[0]) for e in entries) if b is not None])
        spec = LayerSpec(f"Ky STAC point cloud ({count} tiles)", vpc_path, "vpc", bbox)
        notes.append(f"combined into one virtual point cloud ({os.path.basename(vpc_path)})")
        self._run_add([spec], notes)

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
        self._cancel_thumbnail_fetches()
        for attr in ("_task", "_add_task", "_size_task", "_mosaic_task", "_register_task", "_clip_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                setattr(self, attr, None)
        if self._download is not None:
            self._download.cancel()
            self._download = None
