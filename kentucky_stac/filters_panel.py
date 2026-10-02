"""The collapsible "Filters" section of a search tab: date range, maximum cloud cover, sort order and a
cap on how many tiles to fetch. Collapsed by default so it doesn't cost dock space; its header says how
many filters are active."""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional

from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .stac import SearchQuery

DEFAULT_MAX_TILES = 20
_SORTS = [("Server default", None), ("Newest first", "desc"), ("Oldest first", "asc")]


class FiltersPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, lidar: bool, parent=None):
        super().__init__(parent)
        today = QDate.currentDate()

        self.header = QToolButton()
        self.header.setCheckable(True)
        self.header.setAutoRaise(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.toggled.connect(self._toggle)

        self.from_check = QCheckBox("From")
        self.from_date = QDateEdit(today.addYears(-1))
        self.to_check = QCheckBox("To")
        self.to_date = QDateEdit(today)
        for edit in (self.from_date, self.to_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setEnabled(False)
        self.from_check.toggled.connect(self.from_date.setEnabled)
        self.to_check.toggled.connect(self.to_date.setEnabled)

        self.cloud_check = QCheckBox("Max cloud cover")
        self.cloud_check.setToolTip(
            "Keep tiles with at most this much cloud cover. Tiles that don't report cloud cover "
            "(radar, elevation, aerial imagery) are kept."
        )
        self.cloud_spin = QSpinBox()
        self.cloud_spin.setRange(0, 100)
        self.cloud_spin.setValue(20)
        self.cloud_spin.setSuffix(" %")
        self.cloud_spin.setEnabled(False)
        self.cloud_check.toggled.connect(self.cloud_spin.setEnabled)

        self.sort_combo = QComboBox()
        for label, _ in _SORTS:
            self.sort_combo.addItem(label)
        self.sort_combo.setToolTip("Order results by capture date. With many matches, 'Newest first' keeps the latest.")

        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 10000)
        self.max_spin.setSingleStep(10)
        self.max_spin.setValue(DEFAULT_MAX_TILES)
        self.max_spin.setToolTip("The most tiles to fetch per source (default 20). Raise it to see more of a big result; more is slower and heavier on the map.")

        self.reset_button = QPushButton("Reset filters")
        self.reset_button.clicked.connect(self.reset)

        grid = QGridLayout()
        grid.setContentsMargins(16, 0, 0, 0)
        grid.addWidget(self.from_check, 0, 0)
        grid.addWidget(self.from_date, 0, 1)
        grid.addWidget(self.to_check, 1, 0)
        grid.addWidget(self.to_date, 1, 1)
        self.cloud_row = 2
        if not lidar:  # cloud cover means nothing for a point cloud
            grid.addWidget(self.cloud_check, 2, 0)
            grid.addWidget(self.cloud_spin, 2, 1)
        else:
            self.cloud_check.setVisible(False)
            self.cloud_spin.setVisible(False)
        grid.addWidget(_label("Sort"), 3, 0)
        grid.addWidget(self.sort_combo, 3, 1)
        grid.addWidget(_label("Max tiles"), 4, 0)
        grid.addWidget(self.max_spin, 4, 1)
        grid.setColumnStretch(2, 1)
        reset_row = QHBoxLayout()
        reset_row.setContentsMargins(16, 0, 0, 0)
        reset_row.addWidget(self.reset_button)
        reset_row.addStretch(1)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addLayout(grid)
        body_layout.addLayout(reset_row)
        self.body.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.header)
        layout.addWidget(self.body)

        for signal in (
            self.from_check.toggled, self.to_check.toggled, self.cloud_check.toggled,
            self.from_date.dateChanged, self.to_date.dateChanged, self.cloud_spin.valueChanged,
            self.sort_combo.currentIndexChanged, self.max_spin.valueChanged,
        ):
            signal.connect(lambda *_: self._refresh_header())
        self._refresh_header()

    # ---- state -------------------------------------------------------------------------------

    def _toggle(self, open_: bool):
        self.body.setVisible(open_)
        self.header.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)

    def active_count(self) -> int:
        return (
            int(self.from_check.isChecked())
            + int(self.to_check.isChecked())
            + int(self.cloud_check.isChecked() and self.cloud_check.isVisibleTo(self))
            + int(self.sort_combo.currentIndex() != 0)
            + int(self.max_spin.value() != DEFAULT_MAX_TILES)
        )

    def _refresh_header(self):
        count = self.active_count()
        self.header.setText("Filters" + (f" ({count} active)" if count else ""))
        self.header.setArrowType(Qt.ArrowType.DownArrow if self.header.isChecked() else Qt.ArrowType.RightArrow)
        self.changed.emit()

    def reset(self):
        self.from_check.setChecked(False)
        self.to_check.setChecked(False)
        self.cloud_check.setChecked(False)
        self.sort_combo.setCurrentIndex(0)
        self.max_spin.setValue(DEFAULT_MAX_TILES)
        today = QDate.currentDate()
        self.from_date.setDate(today.addYears(-1))
        self.to_date.setDate(today)
        self.cloud_spin.setValue(20)

    # ---- using it ----------------------------------------------------------------------------

    def error(self) -> Optional[str]:
        if self.from_check.isChecked() and self.to_check.isChecked() and self.from_date.date() > self.to_date.date():
            return "The filter's start date is after its end date."
        return None

    def max_tiles(self) -> int:
        return self.max_spin.value()

    def apply(self, query: SearchQuery) -> SearchQuery:
        """`query` with the active filters added. The end date is inclusive (through 23:59:59)."""
        if self.from_check.isChecked():
            d = self.from_date.date()
            query.start = datetime.combine(datetime(d.year(), d.month(), d.day()).date(), time.min)
        if self.to_check.isChecked():
            d = self.to_date.date()
            query.end = datetime.combine(datetime(d.year(), d.month(), d.day()).date(), time(23, 59, 59))
        if self.cloud_check.isChecked() and self.cloud_check.isVisibleTo(self):
            query.max_cloud_cover = float(self.cloud_spin.value())
        query.sortby = _SORTS[self.sort_combo.currentIndex()][1]
        return query


def _label(text: str):
    from qgis.PyQt.QtWidgets import QLabel

    return QLabel(text)
