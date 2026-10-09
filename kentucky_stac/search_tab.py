import os
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsNetworkAccessManager,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
)
from qgis.PyQt.QtCore import QEvent, QObject, QSize, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QGuiApplication, QIcon, QImage, QPixmap
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QMenu,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .aoi import AoiState, wkt_parts
from .catalog import format_size, primary_asset, thumbnail_href
from .downloads import (
    MAX_DOWNLOAD_CONCURRENCY,
    MIN_DOWNLOAD_CONCURRENCY,
    DownloadJob,
    DownloadManager,
    SizesTask,
    default_concurrency,
    filename_from_href,
    plan_downloads,
)
from . import azure, signing
from .filters_panel import FiltersPanel
from .item_json import item_json_text, json_to_html
from .json_card import ItemJsonHover
from .map_link import MapResultsLink
from .layers import (
    AddLayersTask,
    LayerSpec,
    already_on_map,
    asset_layer_spec,
    item_bbox,
    layer_specs,
    local_specs,
    union_bbox,
)
from .mosaic import BuildMosaicsTask, plan_mosaics
from . import scripts
from .mosaicjson import native_gsd, plan_mosaicjson, write_mosaicjson
from .pdal_clip import CropPointCloudsTask, clipped_path
from .results_layer import select_results, show_results
from .server_layers import RegisterMosaicsTask, add_cog_layers, add_search_layer, plan_server_mosaics, tiler_url
from .sources import DEFAULT_SOURCE, ApiSource, is_default_uri, source_name, tiler_for
from .stac import Collection, Item, SearchQuery
from .tasks import PAGE_SIZE, SearchTask
from .vpc import build_vpc
from .vpc_options import VpcOptions, VpcOptionsDialog, start_build

_COLUMNS = ["", "Tile", "Preview"]
_CHECKBOX_COLUMN = 0
_TILE_COLUMN = 1
_THUMBNAIL_SIZE = 64
_INDEX_ROLE = Qt.ItemDataRole.UserRole + 1  # a collection row's index into SearchTab._collections
CONFIRM_ABOVE = 25  # ask before adding more layers than this at once


class WrapButton(QPushButton):
    """A QPushButton whose label wraps onto multiple lines instead of being silently clipped --
    QPushButton has no built-in word-wrap, so the label is a child QLabel (which does) rather than
    the button's own text. setText()/text() are overridden so existing call sites that change a
    button's label at runtime (e.g. "Adding N layers...") keep working unchanged."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._label = QLabel(text, self)
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(self._label)

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def text(self) -> str:
        return self._label.text()


def _projection_label(item: Item) -> Optional[str]:
    """A short EPSG-code label from the item's own STAC properties, to save space in the results
    list -- plain "EPSG:N" for imagery/DEM (a single proj:epsg), or "EPSG:horizontal/EPSG:vertical"
    for a LiDAR item's compound CRS. LiDAR items carry no proj:epsg at all (confirmed live: every
    laz-phaseN item's properties have proj:wkt2 only) -- GDAL's osr resolves that compound WKT's
    horizontal (PROJCS) and vertical (VERT_CS) authority codes separately (verified live:
    EPSG:6473/EPSG:6360 for NAD83(2011) KY Single Zone + NAVD88 height), which QGIS's own CRS class
    can't do (a compound CRS's authid() comes back empty)."""
    epsg = item.properties.get("proj:epsg")
    if epsg:
        return f"EPSG:{epsg}"
    wkt = item.properties.get("proj:wkt2")
    if not wkt:
        return None
    from osgeo import osr

    srs = osr.SpatialReference()
    if srs.ImportFromWkt(wkt) != 0:
        return None
    if srs.IsCompound():
        horizontal = srs.GetAuthorityCode("PROJCS") or srs.GetAuthorityCode("GEOGCS")
        vertical = srs.GetAuthorityCode("VERT_CS")
        if horizontal and vertical:
            return f"EPSG:{horizontal}/EPSG:{vertical}"
        if horizontal:
            return f"EPSG:{horizontal}"
    code = srs.GetAuthorityCode(None)
    if code:
        return f"EPSG:{code}"
    crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
    return crs.description() if crs.isValid() else None


def _point_count_label(item: Item) -> Optional[str]:
    count = item.properties.get("pc:count")
    return f"{int(count):,} points" if count else None


def _gsd_meters_for(item: Item) -> Optional[float]:
    """An item's ground sample distance in meters, for MosaicJSON's maxzoom (mosaicjson.py). Needs a
    units-of-CRS-to-meters conversion, which needs QGIS's CRS database -- kept out of mosaicjson.py
    itself (pure Python, unit-testable without a QgsApplication) and injected from here instead."""
    gsd_native = native_gsd(item.properties.get("proj:bbox"), item.properties.get("proj:shape"))
    epsg = item.properties.get("proj:epsg")
    if gsd_native is None or not epsg:
        return None
    crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
    if not crs.isValid():
        return None
    factor = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), QgsUnitTypes.DistanceUnit.DistanceMeters)
    return gsd_native * factor


class ResultCard(QWidget):
    """One result row's tile id/collection/date/size (plus projection and, for point clouds, point
    count), stacked vertically instead of spread across separate tree columns -- a narrow dock would
    otherwise need horizontal scrolling to read them (reported live via a screenshot). A transparent
    background lets the tree's own selection highlight/alternating row color show through, same as a
    plain-text row would."""

    def __init__(
        self,
        tile_id: str,
        collection: str,
        date: str,
        size: str,
        projection: Optional[str] = None,
        point_count: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        title = QLabel(tile_id)
        title.setStyleSheet("font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(QLabel(collection))
        layout.addWidget(QLabel(date))
        # Hidden until set_size() gives it real text -- the catalog never carries a file size at
        # search time (only a later download's size-check does), so an always-empty label here would
        # leave a permanent blank line in every card.
        self._size_label = QLabel(size)
        self._size_label.setVisible(bool(size))
        layout.addWidget(self._size_label)
        if projection:
            proj_label = QLabel(projection)
            proj_label.setWordWrap(True)
            layout.addWidget(proj_label)
        if point_count:
            layout.addWidget(QLabel(point_count))

    def set_size(self, text: str) -> None:
        self._size_label.setText(text)
        self._size_label.setVisible(bool(text))


def _describe(c: Collection, source: str = "") -> str:
    start, end = (list(c.interval) + [None, None])[:2] if c.interval else (None, None)
    span = f"{(start or '?')[:10]} to {(end or 'present')[:10]}"
    text = f"{c.description}\n\n{span}" if c.description else span
    return f"{text}\n\nSource: {source}" if source else text


class _EveryClickToggles(QObject):
    """Make a rapid second click on the same tile toggle it again.

    The list toggles a tile on each click, but Qt turns a second click at the same spot within the
    double-click interval into a double-click event, which does not change the selection -- so quickly
    ticking and unticking a checkbox (or two clicks that land together) looked like it did nothing."""

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            tree = self.parent()
            item = tree.itemAt(event.position().toPoint() if hasattr(event, "position") else event.pos())
            if item is not None:
                item.setSelected(not item.isSelected())
            return True
        return False


class SearchTab(QWidget):
    """Pick collection(s), search the current AOI, and list the matching tiles."""

    reload_requested = pyqtSignal()
    sources_changed = pyqtSignal(list)  # the source list with a tile server URL newly set

    def __init__(
        self, what: str, kind: str, lidar: bool, aoi_state: AoiState, message_bar=None, parent=None, canvas=None
    ):
        super().__init__(parent)
        self._what = what
        self._kind = kind  # identifies this tab's results layer
        self._lidar = lidar
        self._sources: List[ApiSource] = [DEFAULT_SOURCE]
        self._collections: List[Collection] = []  # the checklist's rows, in order
        self._server_mosaic_note = ""
        self._aoi = aoi_state
        self._bar = message_bar
        self._task: Optional[SearchTask] = None
        self._add_task: Optional[AddLayersTask] = None
        self._size_task: Optional[SizesTask] = None
        self._mosaic_task: Optional[BuildMosaicsTask] = None
        self._register_task: Optional[RegisterMosaicsTask] = None
        self._vpc_task = None  # QGIS's Build virtual point cloud algorithm, when options were chosen
        self._vpc_options = VpcOptions()  # chosen when a download that will combine into a VPC starts
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
        self._sort: Optional[str] = None  # the sort the current results were fetched with

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

        self.filters = FiltersPanel(lidar)
        self.search_button = WrapButton("Search area of interest")
        self.search_button.clicked.connect(self.search)

        self.results_status = QLabel()
        self.results_status.setWordWrap(True)
        # Ctrl+A already selects every result (see add_button's tooltip), but that's not discoverable
        # -- an explicit button pair matches the collections list's own Select All/Clear Selection.
        self.select_all_results_button = QPushButton("Select All")
        self.select_all_results_button.setEnabled(False)
        self.select_all_results_button.clicked.connect(lambda: self.tree.selectAll())
        self.clear_results_selection_button = QPushButton("Clear Selection")
        self.clear_results_selection_button.setEnabled(False)
        self.clear_results_selection_button.clicked.connect(lambda: self.tree.clearSelection())
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(_COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self._icon_size = _THUMBNAIL_SIZE  # grown to match each card's real height once results are shown
        self.tree.setIconSize(QSize(self._icon_size, self._icon_size))
        # Clicking a tile (or its checkbox) toggles just that tile, so any number can be picked one after
        # another without holding Ctrl; Ctrl+A / Select All still take everything.
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._toggle_clicks = _EveryClickToggles(self.tree)
        self.tree.viewport().installEventFilter(self._toggle_clicks)
        # Resting the mouse on a row opens a card with that tile's raw STAC JSON.
        self._json_hover = ItemJsonHover(self.tree, self._json_html_for_row, self)
        # The results footprints on the map and this list follow each other (see map_link.py).
        self._link = MapResultsLink(canvas, self.tree, kind, self._after_map_selection, self)
        self._json_hover.asset_action.connect(self._on_asset_action)
        self._single_asset_download = False  # set while a download started from the JSON card runs
        self._thumb_replies: List[QNetworkReply] = []

        self.add_button = WrapButton("Add selected to map")
        self.add_button.setToolTip("Select tiles in the list above (Ctrl+A selects all)")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_to_map)

        # Streams several selected tiles directly into one combined virtual point cloud layer, no
        # download involved -- the same VPC format as the download-then-combine path (vpc.py), just
        # pointed at each tile's remote href instead of a local file. Only COPC tiles can stream
        # (see layer_specs); plain LAZ/LAS is skipped, same restriction as "Add selected to map".
        self.vpc_button: Optional[QPushButton] = None
        if lidar:
            self.vpc_button = WrapButton("Add selected as Virtual Point Cloud")
            self.vpc_button.setToolTip(
                "Combine the selected tiles into one virtual point cloud layer, streamed directly "
                "with no download (select two or more COPC tiles)"
            )
            self.vpc_button.setEnabled(False)
            self.vpc_button.clicked.connect(self.add_selected_as_vpc)

        self.download_button = WrapButton("Download selected...")
        self.download_button.setToolTip("Save the selected tiles to a folder")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)
        # Point clouds have no VRT equivalent, so only the imagery/DEM tab gets a mosaic button.
        self.mosaic_button: Optional[QPushButton] = None
        if not lidar:
            self.mosaic_button = WrapButton("Export VRT")
            self.mosaic_button.setToolTip(
                "Stitch the selected tiles into one virtual raster, one per collection "
                "(select two or more tiles)"
            )
            self.mosaic_button.setEnabled(False)
            self.mosaic_button.clicked.connect(self.add_mosaic)
        # A portable index file (https://github.com/developmentseed/mosaicjson-spec) that titiler/
        # rio-tiler/cogeo-mosaic can read directly -- pure metadata, no GDAL open and no server
        # round-trip, unlike the VRT mosaic above. Same raster-only, one-per-collection grouping.
        self.mosaicjson_button: Optional[QPushButton] = None
        if not lidar:
            self.mosaicjson_button = WrapButton("Export as MosaicJSON...")
            self.mosaicjson_button.setToolTip(
                "Save a MosaicJSON file for the selected tiles, one per collection, for use with "
                "titiler/rio-tiler/cogeo-mosaic (select two or more tiles)"
            )
            self.mosaicjson_button.setEnabled(False)
            self.mosaicjson_button.clicked.connect(self.export_mosaicjson)
        # Registers a search restricted to the selected tiles' ids and streams that as an XYZ
        # layer -- a precise crop instead of the whole-state streaming layer, with no download and
        # no local stitching. Same raster-only restriction as the VRT mosaic.
        self.server_mosaic_button: Optional[QPushButton] = None
        if not lidar:
            self.server_mosaic_button = WrapButton("Add as server mosaic")
            self._server_mosaic_tip = (
                "Register the selected tiles as a mosaic on the tile server and stream it "
                "(one mosaic per collection). For another API this needs your own titiler: "
                "set it under Sources... > Tile server..."
            )
            self.server_mosaic_button.setToolTip(self._server_mosaic_tip)
            self.server_mosaic_button.setEnabled(False)
            self.server_mosaic_button.clicked.connect(self.add_server_mosaic)
        # A script (notebook, Python or shell) that downloads the selected tiles outside QGIS.
        self.script_button = WrapButton("Export as script...")
        self.script_button.setToolTip("Save a notebook, Python script or shell script that downloads the selected tiles")
        self.script_button.setEnabled(False)
        self.script_button.clicked.connect(self.export_script)
        # How many files download() at once (DownloadManager's own max_parallel) -- ported from the
        # old ArcGIS Pro add-in's "Parallel Downloads" option, which this plugin otherwise had no
        # equivalent of (a flat default of 3, not adjustable). Shared by both tabs via one QgsSettings
        # key, same as mosaic_dir/download_dir.
        self.concurrency_label = QLabel("Downloads at once")
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(MIN_DOWNLOAD_CONCURRENCY, MAX_DOWNLOAD_CONCURRENCY)
        self.concurrency_spin.setValue(
            int(QgsSettings().value("kentucky_stac/download_concurrency", default_concurrency()))
        )
        self.concurrency_spin.setToolTip(
            "How many files download at the same time. Higher can be faster, but may get throttled "
            "by the server or saturate your connection."
        )
        self.concurrency_spin.valueChanged.connect(
            lambda value: QgsSettings().setValue("kentucky_stac/download_concurrency", value)
        )
        # Crops to the AOI's actual shape rather than the tiles' full rectangular extent -- a GDAL
        # warp cutline for a raster mosaic, PDAL filters.crop (see pdal_clip.py) for downloaded point
        # clouds. Only makes sense for a polygon AOI; disabled otherwise (see _update_search_enabled).
        self.clip_to_aoi = QCheckBox("Clip to area of interest")
        self.add_when_done = QCheckBox(
            "Add downloaded files to the map, combined into one virtual point cloud layer"
            if lidar
            else "Add downloaded files to the map"
        )
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
        layout.addWidget(self.filters)
        layout.addWidget(self.search_button)
        layout.addWidget(self.results_status)
        results_selection_row = QHBoxLayout()
        results_selection_row.addWidget(self.select_all_results_button)
        results_selection_row.addWidget(self.clear_results_selection_button)
        layout.addLayout(results_selection_row)
        layout.addWidget(self.tree, 1)
        layout.addLayout(actions)
        if self.vpc_button is not None:
            vpc_row = QHBoxLayout()
            vpc_row.addWidget(self.vpc_button, 1)
            vpc_row.addWidget(self.script_button, 1)
            layout.addLayout(vpc_row)
        if self.mosaic_button is not None or self.mosaicjson_button is not None:
            export_row = QHBoxLayout()
            if self.mosaic_button is not None:
                export_row.addWidget(self.mosaic_button, 1)
            if self.mosaicjson_button is not None:
                export_row.addWidget(self.mosaicjson_button, 1)
            layout.addLayout(export_row)
        if self.server_mosaic_button is not None:
            server_row = QHBoxLayout()
            server_row.addWidget(self.server_mosaic_button, 1)
            server_row.addWidget(self.script_button, 1)
            layout.addLayout(server_row)
        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(self.concurrency_label)
        concurrency_row.addWidget(self.concurrency_spin)
        concurrency_row.addStretch(1)
        layout.addLayout(concurrency_row)
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

    def set_sources(self, sources: List[ApiSource]):
        self._sources = list(sources)
        self._update_add_enabled()  # a tile server may have just been set (or cleared) for a source

    def _can_server_mosaic(self, item: Item) -> bool:
        """KyFromAbove's tiles always can; another source's only once the user has set a tile server for it."""
        return is_default_uri(item.source) or bool(tiler_for(self._sources, item.source))

    def _source_label(self, base_uri: str) -> str:
        """A source's display name, or "" for the built-in catalog (left unlabeled, as before)."""
        return "" if is_default_uri(base_uri) else source_name(self._sources, base_uri)

    def set_collections(self, collections: List[Collection]):
        self._clear_collection_list()
        self._collections = list(collections)
        self.reload_button.setEnabled(True)
        if not collections:
            self._has_collections = False
            self.status.setText(f"No {self._what} collections found.")
            self._update_search_enabled()
            return
        # All collections start unchecked -- pick one or more before searching. A collection from
        # another source is labeled "Title · Source" so two sources' same-named collections are
        # never ambiguous. UserRole keeps the collection id; _INDEX_ROLE holds the row's index into
        # self._collections, since an id alone isn't unique across sources.
        self.list.blockSignals(True)
        for index, c in enumerate(collections):
            source = self._source_label(c.source)
            item = QListWidgetItem(f"{c.title_or_id} · {source}" if source else c.title_or_id)
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            item.setData(_INDEX_ROLE, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(_describe(c, source))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setEnabled(True)
        self.select_all_button.setEnabled(True)
        self.clear_selection_button.setEnabled(True)
        self._has_collections = True
        self.status.setText(f"{len(collections)} collection{'s' if len(collections) != 1 else ''}")
        self._update_search_enabled()

    def selected_collections(self) -> List[Collection]:
        return [
            self._collections[self.list.item(i).data(_INDEX_ROLE)]
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def selected_collection_ids(self) -> List[str]:
        return [c.id for c in self.selected_collections()]

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

    def _aoi_is_polygon(self) -> bool:
        return self._aoi.has_aoi and self._aoi.geometry.type() == Qgis.GeometryType.Polygon

    def _update_search_enabled(self):
        polygon = self._aoi_is_polygon()
        self.clip_to_aoi.setEnabled(polygon)
        # Only applies to the download/mosaic flows -- streaming "Add selected to map" reads tiles
        # directly from their remote hrefs, with no local file for a GDAL cutline or PDAL crop to
        # act on.
        not_valid_note = ' Not valid for "Add selected to map".'
        self.clip_to_aoi.setToolTip(
            not_valid_note.strip()
            if polygon
            else (
                "Draw a Polygon AOI, or select polygon features, to clip point clouds to its "
                "shape (a point or line AOI has no area to clip to)."
                if self._lidar
                else "Draw a Polygon AOI, or select features, to clip a mosaic to its shape "
                "(a point or line AOI has no area to clip to)."
            )
            + not_valid_note
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
        collections = self.selected_collections()
        try:
            intersects = self._aoi.geojson_geometry(QgsProject.instance().transformContext())
        except Exception as e:
            self._warn(f"Could not use the area of interest: {e}")
            return
        if not collections or intersects is None:
            return
        problem = self.filters.error()
        if problem:
            self._warn(problem)
            return

        # One search per source, each restricted to that source's checked collections.
        groups: Dict[str, List[str]] = {}
        for c in collections:
            groups.setdefault(c.source or DEFAULT_SOURCE.base_uri, []).append(c.id)
        names = {s.base_uri: s.name for s in self._sources}

        self._clear_results()
        self._set_results_message("Searching...")
        query = self.filters.apply(SearchQuery(intersects=intersects, limit=min(PAGE_SIZE, self.filters.max_tiles())))
        self._sort = query.sortby
        self._task = SearchTask(list(groups.items()), query, self._on_results, names, self.filters.max_tiles())
        self._update_search_enabled()
        QgsApplication.taskManager().addTask(self._task)

    def _on_results(self, items: List[Item], matched: Optional[int], truncated: bool, error: Optional[str]):
        task, self._task = self._task, None
        if task is not None and task.errors and not error:
            # Some sources failed but at least one answered: show what came back, and say which didn't.
            detail = "; ".join(f"{name}: {msg}" for name, msg in task.errors)
            self._warn(f"Some sources could not be searched -- {detail}")
        if task is not None and task.notes and not error:
            self._info("; ".join(task.notes) + ".")
        if error:
            self._set_results_message(f"Search failed: {error}")
            self._warn(f"Search failed: {error}")
            self._update_search_enabled()
            return
        if self._sort:  # by capture date, as asked (missing dates last)
            dated = sorted((i for i in items if i.datetime), key=lambda i: i.datetime, reverse=self._sort == "desc")
            self._items = dated + sorted((i for i in items if not i.datetime), key=lambda i: (i.collection or "", i.id))
        else:
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
        self._link.set_results(self._items, self._fids)

        for index, item in enumerate(self._items):
            asset = primary_asset(item, self._lidar)
            row = QTreeWidgetItem(["", "", ""])
            self.tree.addTopLevelItem(row)
            checkbox = QCheckBox()
            # Only a mirror of the row's selection (see _sync_card_checkboxes). Letting it take clicks of its
            # own meant a click could toggle the box and the row separately, and they drifted apart; now a
            # click anywhere on the row, checkbox included, reaches the list and toggles the tile once.
            checkbox.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.tree.setItemWidget(row, _CHECKBOX_COLUMN, checkbox)
            self._json_hover.watch(checkbox, index)
            source = self._source_label(item.source)
            card = ResultCard(
                item.id,
                f"{item.collection or ''} · {source}" if source else (item.collection or ""),
                (item.datetime or "")[:10],
                format_size(asset.file_size) if asset else "",
                projection=_projection_label(item),
                point_count=_point_count_label(item),
            )
            self.tree.setItemWidget(row, _TILE_COLUMN, card)
            self._json_hover.watch(card, index)
        self.tree.resizeColumnToContents(_TILE_COLUMN)
        self.tree.setColumnWidth(_CHECKBOX_COLUMN, 24)
        # Grow the thumbnail to fill the same height as the stacked text card next to it, instead of
        # a small fixed square floating in a much taller row -- measured after layout since the
        # card's real height depends on which optional lines it ends up showing.
        self._icon_size = max(_THUMBNAIL_SIZE, self.tree.sizeHintForRow(0))
        self.tree.setIconSize(QSize(self._icon_size, self._icon_size))
        self.tree.setColumnWidth(len(_COLUMNS) - 1, self._icon_size + 8)
        self._fetch_thumbnails()
        self.select_all_results_button.setEnabled(True)
        self.clear_results_selection_button.setEnabled(True)

        count = len(self._items)
        text = f"{count} tile{'s' if count != 1 else ''} found"
        if truncated:
            text += f" (showing the first {count}" + (f" of {matched}" if matched else "") + "; narrow the area or add filters to see the rest)"
        self._set_results_message(text)
        self._update_search_enabled()

    # ---- asset actions from the hover card -----------------------------------------------------

    def _on_asset_action(self, row: int, action: str, key: str):
        """download / copy URL / add to map for one asset of a tile, picked in the JSON hover card."""
        if not 0 <= row < len(self._items):
            return
        item = self._items[row]
        asset = item.assets.get(key)
        if asset is None or not asset.href:
            self._warn(f"{item.id} has no URL for asset '{key}'.")
            return
        if action == "copy":
            url = signing.fetchable_url(asset.href) if azure.parse(asset.href) else asset.href
            QGuiApplication.clipboard().setText(url)
            note = " (signed, so it works for a limited time)" if url != asset.href else ""
            self._info(f"Copied the URL of asset '{key}' ({item.id}) to the clipboard{note}.")
        elif action == "map":
            if self._busy():
                self._warn("Wait for the current task to finish first.")
                return
            spec = asset_layer_spec(item, key)
            if spec is None:
                self._warn(f"Asset '{key}' can't be shown on the map; download it instead.")
            elif already_on_map([spec]):
                self._info(f"Asset '{key}' of {item.id} is already on the map.")
            else:
                self._run_add([spec], [f"asset '{key}'"])
        elif action == "download":
            self._download_asset(item, key, asset.href)

    def _download_asset(self, item: Item, key: str, href: str):
        if self._busy():
            self._warn("Wait for the current task to finish first.")
            return
        settings = QgsSettings()
        start = os.path.join(str(settings.value("kentucky_stac/download_dir", "") or ""), filename_from_href(href))
        path, _ = QFileDialog.getSaveFileName(self, f"Save asset '{key}' of {item.id}", start)
        if not path:
            return
        settings.setValue("kentucky_stac/download_dir", os.path.dirname(path))
        try:
            if os.path.exists(path):
                os.remove(path)  # the save dialog already asked about overwriting
        except OSError as e:
            self._warn(f"Could not replace {path}: {e}")
            return
        job = DownloadJob(f"{item.id} · {key}", href, path)
        self._download_files, self._download_bboxes, self._download_items = [path], {}, {}
        self._pending_notes = []
        self._single_asset_download = True  # just save it: no adding to the map afterwards
        self._begin_download([job], {})

    def _json_html_for_row(self, row: int) -> Optional[str]:
        if not 0 <= row < len(self._items):
            return None
        item = self._items[row]
        return json_to_html(item_json_text(item), item)

    def _clear_results(self):
        self._json_hover.reset()
        self._link.clear()
        self._cancel_thumbnail_fetches()
        self._items = []
        self._fids = []
        self.tree.clear()
        select_results(self._kind, [])
        self.select_all_results_button.setEnabled(False)
        self.clear_results_selection_button.setEnabled(False)

    def _fetch_thumbnails(self):
        """Populate each result row's Preview column with its STAC thumbnail asset, fetched
        asynchronously (QgsNetworkAccessManager, non-blocking) so a slow/failed fetch for one tile
        never holds up the others or the UI."""
        for row, item in enumerate(self._items):
            href = thumbnail_href(item, self._lidar)
            if not href:
                continue
            reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(signing.fetchable_url(href))))
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
            self._icon_size, self._icon_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
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
        self._sync_card_checkboxes()
        self._update_add_enabled()

    def _after_map_selection(self):
        """The tile selection was just changed from the map: bring the checkboxes and buttons along."""
        self._sync_card_checkboxes()
        self._update_add_enabled()

    def _sync_card_checkboxes(self):
        """Keep each row's checkbox in step with the tree's own selection, however it changed --
        clicking a row, Ctrl-click, Select All/Clear Selection, or the checkbox itself."""
        for i in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(i)
            checkbox = self.tree.itemWidget(row, _CHECKBOX_COLUMN)
            if isinstance(checkbox, QCheckBox):
                checkbox.blockSignals(True)
                checkbox.setChecked(row.isSelected())
                checkbox.blockSignals(False)

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
                self._vpc_task,
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
        if self.mosaicjson_button is not None:
            self.mosaicjson_button.setEnabled(enabled)
        if self.server_mosaic_button is not None:
            self._update_server_mosaic_button(enabled)
        self.script_button.setEnabled(enabled)

    def _update_server_mosaic_button(self, enabled: bool):
        """Gray out "Add as server mosaic" unless at least one selected tile can be served: KyFromAbove's
        can, another API's only with a titiler of the user's own (Sources... > Tile server...)."""
        items = self.selected_items() if enabled else []
        usable = any(self._can_server_mosaic(i) for i in items)
        self.server_mosaic_button.setEnabled(enabled and usable)
        missing = sorted({source_name(self._sources, i.source) for i in items if not self._can_server_mosaic(i)})
        if enabled and not usable and missing:
            self.server_mosaic_button.setToolTip(
                f"Needs a titiler of your own for {', '.join(missing)}: enter its URL under Sources... > Tile server..."
            )
        else:
            self.server_mosaic_button.setToolTip(self._server_mosaic_tip)

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
                entries.append((item, signing.fetchable_url(asset.href)))
            else:
                skipped += 1
        if len(entries) < 2:
            self._info("Select two or more streamable (COPC) tiles to combine into a virtual point cloud.")
            return
        # No options here: QGIS's Build virtual point cloud algorithm has to read every point, and over
        # https it never finished (tested), so the options are offered on the download path only.
        options = VpcOptions()
        settings = QgsSettings()
        folder = QFileDialog.getExistingDirectory(
            self, "Save the virtual point cloud (.vpc) to", str(settings.value("kentucky_stac/vpc_dir", "") or "")
        )
        if not folder:
            return
        settings.setValue("kentucky_stac/vpc_dir", folder)

        vpc_path = os.path.join(folder, f"kentucky_stac_pointcloud_{datetime.now().strftime('%H%M%S')}.vpc")
        notes = [f"streamed directly (no download), saved to {os.path.basename(vpc_path)}"]
        if skipped:
            notes.append(f"{skipped} tile{'s' if skipped != 1 else ''} plain LAZ/LAS, which QGIS can't stream; left out")
        self._make_vpc(entries, vpc_path, options, notes)

    def _make_vpc(self, entries, vpc_path: str, options: VpcOptions, notes: List[str]):
        """Write the .vpc for `entries` ((item, local path or url) pairs) and add it to the map. With no
        options it's built from the STAC metadata at once; with any, QGIS's own algorithm runs in the
        background (it reads every point) and the layer is added when it finishes."""
        bbox = union_bbox([b for b in (item_bbox(e[0]) for e in entries) if b is not None])
        if not options.any:
            count = build_vpc(entries, vpc_path)
            if count < 2:
                self._warn("Could not build a combined virtual point cloud (missing tile footprints); nothing added.")
                return
            self._run_add([LayerSpec(f"Ky STAC point cloud ({count} tiles)", vpc_path, "vpc", bbox)], notes)
            return
        spec = LayerSpec(f"Ky STAC point cloud ({len(entries)} tiles)", vpc_path, "vpc", bbox)
        self.results_status.setText("Building the virtual point cloud (reading every point)...")
        self._vpc_task = start_build(
            [e[1] for e in entries], vpc_path, options, lambda ok, message: self._on_vpc_built(ok, message, spec, notes)
        )
        self._update_add_enabled()

    def _on_vpc_built(self, ok: bool, message: str, spec: LayerSpec, notes: List[str]):
        self._vpc_task = None
        self.results_status.setText(self._results_message or "")
        self._update_add_enabled()
        if not ok:
            self._warn(f"Could not build the virtual point cloud: {message}")
            return
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

        clip_wkt = None
        if self.clip_to_aoi.isChecked() and self._aoi_is_polygon():
            try:
                clip_wkt = self._aoi.geometry_wgs84(QgsProject.instance().transformContext()).asWkt()
                notes.append("clipped to the area of interest")
            except Exception as e:
                self._warn(f"Could not use the area of interest to clip the mosaic, building it unclipped instead: {e}")

        self._pending_notes = notes
        self.mosaic_button.setText("Building mosaic...")
        self._mosaic_task = BuildMosaicsTask(specs, self._on_mosaics_built, clip_wkt=clip_wkt)
        self._update_add_enabled()
        QgsApplication.taskManager().addTask(self._mosaic_task)

    def _on_mosaics_built(self, built: list, failed: list):
        self._mosaic_task = None
        self.mosaic_button.setText("Export VRT")
        if failed:
            first = f"{failed[0][0]}: {failed[0][1]}"
            self._warn(f"{len(failed)} mosaic{'s' if len(failed) != 1 else ''} failed (first: {first}).")
        if not built:
            self._update_add_enabled()
            return
        specs = [LayerSpec(m.name, m.vrt_path, "gdal", m.bbox) for m in built]
        self._run_add(specs, list(self._pending_notes))

    # ---- script export ----------------------------------------------------------------------

    def export_script(self):
        """Offer notebook / Python / shell, then save a script that downloads the selected tiles."""
        if self._busy():
            return
        items = self.selected_items()
        jobs, missing = plan_downloads(items, self._lidar, "")
        if not jobs:
            self._info("None of the selected tiles has a downloadable file.")
            return
        menu = QMenu(self)
        for kind, (label, _ext, _filter) in scripts.KINDS.items():
            menu.addAction(label).setData(kind)
        chosen = menu.exec(self.script_button.mapToGlobal(self.script_button.rect().bottomLeft()))
        if chosen is None:
            return
        kind = chosen.data()
        _label, ext, file_filter = scripts.KINDS[kind]
        settings = QgsSettings()
        start = os.path.join(str(settings.value("kentucky_stac/script_dir", "") or ""), "download_tiles" + ext)
        path, _ = QFileDialog.getSaveFileName(self, "Save the script", start, file_filter)
        if not path:
            return
        settings.setValue("kentucky_stac/script_dir", os.path.dirname(path))
        entries = scripts.entries_for((os.path.basename(j.dest), j.url) for j in jobs)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(scripts.render(kind, entries))
        except OSError as e:
            self._warn(f"Could not save the script: {e}")
            return
        text = f"Saved a script for {len(entries)} tile{'s' if len(entries) != 1 else ''} to {path}"
        if missing:
            text += f"; {len(missing)} without a downloadable file left out"
        self._info(text + ".")

    # ---- MosaicJSON export ------------------------------------------------------------------

    def export_mosaicjson(self):
        """Save a MosaicJSON file per collection among the selected tiles. Pure metadata (each
        tile's own STAC properties), so unlike the VRT mosaic this needs no background task -- no
        GDAL open, no network access."""
        if self._busy():
            return
        items = self.selected_items()
        if len(items) < 2:
            self._info("Select two or more tiles to export a MosaicJSON.")
            return
        settings = QgsSettings()
        folder = QFileDialog.getExistingDirectory(
            self, "Save the MosaicJSON to", str(settings.value("kentucky_stac/mosaicjson_dir", "") or "")
        )
        if not folder:
            return
        settings.setValue("kentucky_stac/mosaicjson_dir", folder)

        specs, left_out = plan_mosaicjson(items, folder, datetime.now().strftime("%H%M%S"))
        if not specs:
            self._info("A MosaicJSON needs two or more tiles from the same collection.")
            return
        written = 0
        failed = []
        for spec in specs:
            try:
                if write_mosaicjson(spec, _gsd_meters_for):
                    written += 1
                else:
                    failed.append((spec.name, "no tile had a usable footprint"))
            except Exception as e:
                failed.append((spec.name, str(e)))

        notes = [f"saved to {folder}"]
        if left_out:
            n = len(left_out)
            notes.append(f"{n} tile{'s' if n != 1 else ''} left out (alone in {'their' if n != 1 else 'its'} collection)")
        parts = [f"Exported {written} MosaicJSON file{'s' if written != 1 else ''}"] + notes
        if failed:
            parts.append(f"{len(failed)} failed (first: {failed[0][0]}: {failed[0][1]})")
        (self._warn if failed else self._info)("; ".join(parts) + ".")

    # ---- server mosaic ----------------------------------------------------------------------

    def add_server_mosaic(self):
        if self._busy():
            return
        items = self.selected_items()
        # KyFromAbove's tiles use its titiler-pgstac server. Another source's tiles need that source's own
        # titiler (Sources... > Tile server...); tiles of a source without one are left out.
        skipped = sum(1 for i in items if not self._can_server_mosaic(i))
        self._server_mosaic_note = (
            f"{skipped} tile{'s' if skipped != 1 else ''} left out (no tile server set for {'their' if skipped != 1 else 'its'} source)"
            if skipped
            else ""
        )
        groups = plan_server_mosaics(
            items,
            lambda uri: tiler_url() if is_default_uri(uri) else tiler_for(self._sources, uri),
            lambda uri: source_name(self._sources, uri),
        )
        if not groups:
            self._info(
                "Nothing to register: no tile server is set for the selected tiles' source."
                if skipped
                else "Nothing to register: the selected tiles have no usable collection."
            )
            return
        self.server_mosaic_button.setText("Registering mosaic...")
        self._register_task = RegisterMosaicsTask(groups, self._on_server_mosaics_registered)
        self._update_add_enabled()
        QgsApplication.taskManager().addTask(self._register_task)

    def _on_server_mosaics_registered(self, built: list, failed: list):
        self._register_task = None
        self.server_mosaic_button.setText("Add as server mosaic")
        added, tile_layers, extra_notes = 0, 0, []
        for group, search_id, style, kind in built:
            if kind == "cog":  # a plain titiler: one layer per tile
                count, notes = add_cog_layers(group)
                tile_layers += count
                extra_notes.extend(notes)
                continue
            layer, message = add_search_layer(search_id, group, style)
            if layer is not None:
                added += 1
            elif "already" not in message:
                extra_notes.append(message)

        parts = [f"Added {added} server mosaic{'s' if added != 1 else ''}"]
        if tile_layers:
            parts.append(f"{tile_layers} tile layer{'s' if tile_layers != 1 else ''} from a titiler server")
        if self._server_mosaic_note:
            parts.append(self._server_mosaic_note)
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
        # Downloaded point clouds are combined into a virtual point cloud when that box is ticked,
        # so ask for its options now rather than interrupting once the downloads finish.
        self._vpc_options = VpcOptions()
        if self._lidar and self.add_when_done.isChecked() and len(items) >= 2:
            options = VpcOptionsDialog.ask(self)
            if options is None:
                return
            self._vpc_options = options
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
        self._download = DownloadManager(jobs, sizes, max_parallel=self.concurrency_spin.value(), parent=self)
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
        single, self._single_asset_download = self._single_asset_download, False
        if cancelled or single or not self.add_when_done.isChecked():
            return
        # Add every planned file that is now on disk, including ones downloaded on an earlier run.
        paths = [f for f in self._download_files if os.path.exists(f)]
        if not paths:
            return
        # PDAL clipping only applies to downloaded point clouds (see pdal_clip.py) -- the imagery/DEM
        # tab's own clip_to_aoi only applies to the mosaic-building flow (start_mosaic), not a plain
        # download, which has no clipping implementation of its own.
        if self._lidar and self.clip_to_aoi.isChecked() and self._aoi_is_polygon():
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
        notes.append(f"combined into one virtual point cloud ({os.path.basename(vpc_path)})")
        self._make_vpc(entries, vpc_path, self._vpc_options, notes)

    def _show_sizes(self, sizes: Dict[str, Optional[int]]):
        for row, item in enumerate(self._items):
            asset = primary_asset(item, self._lidar)
            size = sizes.get(asset.href) if asset else None
            if size:
                card = self.tree.itemWidget(self.tree.topLevelItem(row), _TILE_COLUMN)
                if isinstance(card, ResultCard):
                    card.set_size(format_size(size))
        self.tree.resizeColumnToContents(_TILE_COLUMN)

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
        self._json_hover.reset()
        self._link.shutdown()
        self._cancel_thumbnail_fetches()
        for attr in ("_task", "_add_task", "_size_task", "_mosaic_task", "_register_task", "_clip_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                setattr(self, attr, None)
        if self._download is not None:
            self._download.cancel()
            self._download = None
