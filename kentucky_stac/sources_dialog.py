"""The "Data sources" dialog: point the dock at another STAC API alongside the built-in KyFromAbove
catalog (Add), or in place of every current source (Replace all), picked from STAC Index or typed
in by hand. Ported from the ArcGIS Pro add-in's AddApiSourceDialog."""

from __future__ import annotations

from typing import List, Optional

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from .sources import (
    BUILTIN_ENTRY,
    ApiSource,
    CatalogEntry,
    add_source,
    is_valid_api_url,
    normalize_url,
    with_tiler_url,
)
from .tasks import StacIndexTask

_WARNING = (
    "<b>Experimental.</b> Other STAC APIs implement the spec differently, so results, thumbnails "
    "or downloads from them may behave differently or fail."
)

_TILER_NOTICE = (
    "<b>&#9888; Bring your own titiler URL.</b> \"Add as server mosaic\" stays grayed out for another API's "
    "tiles until you enter the base URL of your own <b>titiler</b> or <b>titiler-pgstac</b> server below "
    "(or via <i>Tile server...</i>). The built-in server holds KyFromAbove data only. Without one, use "
    "\"Add selected to map\" or download instead."
)


_TILER_INFO = (
    "<p><b>What this is.</b> \"Add as server mosaic\" streams tiles from a <b>titiler</b> server, which turns "
    "cloud-optimized GeoTIFFs into map tiles on the fly. KyFromAbove's tiles use its "
    "titiler-pgstac server. For any other source the button stays grayed out until you set "
    "<b>your own titiler</b>.</p>"
    "<p><b>Two kinds work:</b></p><ul>"
    "<li><b>titiler-pgstac</b> connected to that API's catalog: one mosaic layer per collection.</li>"
    "<li><b>Plain titiler</b> (<code>/cog/tiles</code>): one layer per tile (up to 50 at a time), each reading "
    "that tile's own file. This needs no catalog connection, so it works for any source.</li></ul>"
    "<p>The plugin works out which kind your server is.</p>"
    "<p><b>The server must be able to read the files.</b> Public https files are fine. Files at s3:// "
    "addresses only work if your server has its own AWS access (and, for requester-pays buckets, is set "
    "up to pay).</p>"
    "<p><i>Experimental: servers differ, so it may not work with every one.</i></p>"
)


class TilerUrlDialog(QDialog):
    """Ask for the titiler / titiler-pgstac server behind "Add as server mosaic" for one source."""

    def __init__(self, source_name: str, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tile server")
        self.setMinimumWidth(480)
        warning = QLabel(
            "<b>Experimental.</b> \"Add as server mosaic\" stays grayed out for "
            f"{source_name} until you enter the base URL of your own titiler / titiler-pgstac server. "
            "Servers differ, so this may not work with every one."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "QLabel { background: #fff4d6; color: #5c4400; border: 1px solid #e8cf8a; "
            "border-radius: 4px; padding: 6px; }"
        )
        self.url_edit = QLineEdit(current or "https://")
        self.url_edit.selectAll()
        self.info_button = QToolButton()
        self.info_button.setText("i")
        self.info_button.setToolTip("How tile servers work here")
        self.info_button.setStyleSheet("QToolButton { font-weight: bold; border-radius: 8px; padding: 1px 6px; }")
        self.info_button.clicked.connect(self._show_info)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #c0392b;")
        self.error_label.setVisible(False)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(warning)
        label_row = QHBoxLayout()
        label_row.addWidget(QLabel("Tile server base URL (blank to clear)"))
        label_row.addWidget(self.info_button)
        label_row.addStretch(1)
        layout.addLayout(label_row)
        layout.addWidget(self.url_edit)
        layout.addWidget(self.error_label)
        layout.addLayout(row)

    def _show_info(self):
        box = QMessageBox(self)
        box.setWindowTitle("About tile servers")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(_TILER_INFO)
        box.exec()

    def _accept(self):
        url = self.url()
        if url and not is_valid_api_url(url):
            self.error_label.setText("Enter a valid http(s):// URL.")
            self.error_label.setVisible(True)
            return
        self.accept()

    def url(self) -> str:
        url = normalize_url(self.url_edit.text())
        return "" if url in ("https:", "http:") else url

    @staticmethod
    def ask(source_name: str, current: str = "", parent=None):
        """The entered URL ("" = clear it), or None if the user cancelled."""
        dialog = TilerUrlDialog(source_name, current, parent)
        return dialog.url() if dialog.exec() == QDialog.DialogCode.Accepted else None


class SourcesDialog(QDialog):
    def __init__(self, sources: List[ApiSource], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Data sources")
        self.setMinimumWidth(460)
        self._sources = list(sources)
        self._task: Optional[StacIndexTask] = None

        warning = QLabel(_WARNING)
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "QLabel { background: #fff4d6; color: #5c4400; border: 1px solid #e8cf8a; "
            "border-radius: 4px; padding: 6px; }"
        )

        tiler_notice = QLabel(_TILER_NOTICE)
        tiler_notice.setWordWrap(True)
        tiler_notice.setStyleSheet(
            "QLabel { background: #fde3e0; color: #7a1d12; border: 2px solid #c0392b; "
            "border-radius: 4px; padding: 6px; }"
        )

        self.source_list = QListWidget()
        self.source_list.setMaximumHeight(100)
        self.source_list.currentRowChanged.connect(self._update_remove_enabled)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Remove the selected source (the built-in KyFromAbove catalog can't be removed)")
        self.remove_button.clicked.connect(self._remove_selected)
        self.tiler_button = QPushButton("Tile server...")
        self.tiler_button.setToolTip(
            "Experimental: set the titiler / titiler-pgstac server used for \"Add as server mosaic\" with the selected source's tiles"
        )
        self.tiler_button.clicked.connect(self._edit_tiler)
        side_buttons = QVBoxLayout()
        side_buttons.addWidget(self.tiler_button)
        side_buttons.addWidget(self.remove_button)
        side_buttons.addStretch(1)
        active_row = QHBoxLayout()
        active_row.addWidget(self.source_list, 1)
        active_row.addLayout(side_buttons)

        self.catalog_combo = QComboBox()
        self.catalog_combo.setEnabled(False)
        self.catalog_combo.addItem("Loading catalogs from STAC Index...")
        self.catalog_combo.activated.connect(self._on_catalog_chosen)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Labels this source's collections")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/stac/v1")
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #c0392b;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)

        self.tiler_edit = QLineEdit()
        self.tiler_edit.setPlaceholderText("Your own titiler / titiler-pgstac URL (needed for \"Add as server mosaic\")")

        form = QFormLayout()
        form.addRow("STAC Index", self.catalog_combo)
        form.addRow("Name", self.name_edit)
        form.addRow("API base URL", self.url_edit)
        form.addRow("Your titiler", self.tiler_edit)

        self.add_button = QPushButton("Add")
        self.add_button.setToolTip("Keep the current source(s) and search this one alongside them")
        self.add_button.clicked.connect(lambda: self._apply(replace=False))
        self.replace_button = QPushButton("Replace all sources")
        self.replace_button.setToolTip("Drop every current source and search only this API")
        self.replace_button.clicked.connect(lambda: self._apply(replace=True))
        add_row = QHBoxLayout()
        add_row.addStretch(1)
        add_row.addWidget(self.add_button)
        add_row.addWidget(self.replace_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(warning)
        layout.addWidget(tiler_notice)
        layout.addWidget(QLabel("Active sources"))
        layout.addLayout(active_row)
        layout.addWidget(QLabel("Add a source"))
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addLayout(add_row)
        layout.addWidget(buttons)

        self._refresh_list()
        self._load_catalogs()

    # ---- result ----------------------------------------------------------------------------

    @property
    def sources(self) -> List[ApiSource]:
        return list(self._sources)

    # ---- active list -----------------------------------------------------------------------

    def _refresh_list(self):
        self.source_list.clear()
        for s in self._sources:
            label = f"{s.name}  (built-in)" if s.is_default else s.name
            if s.tiler_url:
                label += "  (tile server set)"
            item = QListWidgetItem(label)
            item.setToolTip(s.base_uri)
            self.source_list.addItem(item)
        self._update_remove_enabled()

    def _update_remove_enabled(self, *_):
        row = self.source_list.currentRow()
        editable = 0 <= row < len(self._sources) and not self._sources[row].is_default
        self.remove_button.setEnabled(editable)
        self.tiler_button.setEnabled(editable)

    def _remove_selected(self):
        row = self.source_list.currentRow()
        if 0 <= row < len(self._sources) and not self._sources[row].is_default:
            del self._sources[row]
            self._refresh_list()

    def _edit_tiler(self):
        row = self.source_list.currentRow()
        if not (0 <= row < len(self._sources)) or self._sources[row].is_default:
            return
        source = self._sources[row]
        url = TilerUrlDialog.ask(source.name, source.tiler_url, self)
        if url is None:
            return
        self._sources = with_tiler_url(self._sources, source.base_uri, url)
        self._refresh_list()
        self.source_list.setCurrentRow(row)

    # ---- STAC Index ------------------------------------------------------------------------

    def _load_catalogs(self):
        self._task = StacIndexTask(self._on_catalogs)
        QgsApplication.taskManager().addTask(self._task)

    def _on_catalogs(self, entries: List[CatalogEntry], error: Optional[str]):
        self._task = None
        self.catalog_combo.clear()
        if error or not entries:
            # Still offer Kentucky's own catalog, so there's always a way back to the default.
            entries = [BUILTIN_ENTRY]
            self.catalog_combo.setToolTip("Couldn't reach STAC Index -- only KyFromAbove is listed. "
                                          "You can still type a URL.")
        self.catalog_combo.addItem("Select a catalog...", None)
        for e in entries:
            self.catalog_combo.addItem(e.title, e)
            self.catalog_combo.setItemData(self.catalog_combo.count() - 1, e.detail, Qt.ItemDataRole.ToolTipRole)
        self.catalog_combo.setEnabled(True)

    def _on_catalog_chosen(self, index: int):
        entry = self.catalog_combo.itemData(index)
        if isinstance(entry, CatalogEntry):
            self.name_edit.setText(entry.name)
            self.url_edit.setText(entry.url)
            self.error_label.setVisible(False)

    # ---- add / replace ---------------------------------------------------------------------

    def _apply(self, replace: bool):
        url = normalize_url(self.url_edit.text())
        if not is_valid_api_url(url):
            self.error_label.setText("Enter a valid http(s):// URL for the STAC API base.")
            self.error_label.setVisible(True)
            return
        tiler = normalize_url(self.tiler_edit.text())
        if tiler and not is_valid_api_url(tiler):
            self.error_label.setText("Enter a valid http(s):// URL for the tile server, or leave it blank.")
            self.error_label.setVisible(True)
            return
        before = len(self._sources)
        self._sources = add_source(self._sources, self.name_edit.text(), url, replace, tiler)
        if not replace and len(self._sources) == before:
            self.error_label.setText("That source is already active.")
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)
        self.name_edit.clear()
        self.url_edit.clear()
        self.tiler_edit.clear()
        self.catalog_combo.setCurrentIndex(0)
        self._refresh_list()

    def done(self, result: int):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        super().done(result)
