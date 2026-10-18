"""Right-click > Add asset > Choose several...: tick any number of a tile's raster assets (bands)."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class AssetsDialog(QDialog):
    def __init__(self, entries: Sequence[Tuple[str, str]], tiles: int, parent=None):
        """`entries` is (asset key, label) for each asset on offer; `tiles` how many tiles will get them."""
        super().__init__(parent)
        self.setWindowTitle("Choose bands to add")
        self.setMinimumSize(440, 420)
        who = f"each of the {tiles} selected tiles" if tiles != 1 else "this tile"
        intro = QLabel(f"Tick the bands (assets) to add for {who}.")
        intro.setWordWrap(True)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter, e.g. red or swir")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.list = QListWidget()
        for key, label in entries:
            item = QListWidgetItem(f"{label}  ({key})")
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.itemChanged.connect(self._update_ok)
        all_button = QPushButton("Select all shown")
        all_button.clicked.connect(lambda: self._set_shown(Qt.CheckState.Checked))
        none_button = QPushButton("Clear")
        none_button.clicked.connect(lambda: self._set_shown(Qt.CheckState.Unchecked))
        row = QHBoxLayout()
        row.addWidget(all_button)
        row.addWidget(none_button)
        row.addStretch(1)
        self.stack_box = QCheckBox("Combine the chosen bands into one multi-band layer")
        self.stack_box.setToolTip(
            "One layer per tile holding every chosen band (a three-band choice is drawn as a color image), "
            "instead of one layer per band."
        )
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)
        layout.addWidget(self.stack_box)
        layout.addWidget(self.buttons)
        self._update_ok()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list.count()):
            self.list.item(i).setHidden(bool(needle) and needle not in self.list.item(i).text().lower())

    def _set_shown(self, state) -> None:
        for i in range(self.list.count()):
            if not self.list.item(i).isHidden():
                self.list.item(i).setCheckState(state)

    def chosen(self) -> List[str]:
        return [
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _update_ok(self) -> None:
        n = len(self.chosen())
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(n > 0)
        self.stack_box.setEnabled(n > 1)
        if n < 2:
            self.stack_box.setChecked(False)

    @staticmethod
    def ask(entries: Sequence[Tuple[str, str]], tiles: int, parent=None) -> Optional[Tuple[List[str], bool]]:
        dialog = AssetsDialog(entries, tiles, parent)
        if not dialog.exec():
            return None
        return dialog.chosen(), dialog.stack_box.isChecked()
