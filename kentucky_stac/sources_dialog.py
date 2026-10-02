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
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from .sources import BUILTIN_ENTRY, ApiSource, CatalogEntry, add_source, is_valid_api_url, normalize_url
from .tasks import StacIndexTask

_WARNING = (
    "<b>Experimental.</b> Other STAC APIs implement the spec differently, so results, thumbnails "
    "or downloads from them may behave differently or fail. Kentucky-specific features (the server "
    "mosaic) only apply to KyFromAbove tiles."
)


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

        self.source_list = QListWidget()
        self.source_list.setMaximumHeight(100)
        self.source_list.currentRowChanged.connect(self._update_remove_enabled)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Remove the selected source (the built-in KyFromAbove catalog can't be removed)")
        self.remove_button.clicked.connect(self._remove_selected)
        active_row = QHBoxLayout()
        active_row.addWidget(self.source_list, 1)
        active_row.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignTop)

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

        form = QFormLayout()
        form.addRow("STAC Index", self.catalog_combo)
        form.addRow("Name", self.name_edit)
        form.addRow("API base URL", self.url_edit)

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
            item = QListWidgetItem(label)
            item.setToolTip(s.base_uri)
            self.source_list.addItem(item)
        self._update_remove_enabled()

    def _update_remove_enabled(self, *_):
        row = self.source_list.currentRow()
        self.remove_button.setEnabled(0 <= row < len(self._sources) and not self._sources[row].is_default)

    def _remove_selected(self):
        row = self.source_list.currentRow()
        if 0 <= row < len(self._sources) and not self._sources[row].is_default:
            del self._sources[row]
            self._refresh_list()

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
        before = len(self._sources)
        self._sources = add_source(self._sources, self.name_edit.text(), url, replace)
        if not replace and len(self._sources) == before:
            self.error_label.setText("That source is already active.")
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)
        self.name_edit.clear()
        self.url_edit.clear()
        self.catalog_combo.setCurrentIndex(0)
        self._refresh_list()

    def done(self, result: int):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        super().done(result)
