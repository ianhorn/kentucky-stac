"""The collapsible "Point filter" section of the LiDAR tab: tick the classifications to keep and choose the
first, the last or all returns. Collapsed by default, like the search Filters; its header says what is kept.
The choice applies to Add selected to map, Add selected as Virtual Point Cloud and Download selected (see
point_filter.py)."""

from __future__ import annotations

from typing import Dict

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .point_filter import ALL_CLASSES, ALL_RETURNS, CLASSES, PRESETS, RETURN_CHOICES, PointFilter

_COLUMNS = 2


class PointFilterPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.header = QToolButton()
        self.header.setCheckable(True)
        self.header.setAutoRaise(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.toggled.connect(self._toggle)

        self.class_boxes: Dict[int, QCheckBox] = {}
        grid = QGridLayout()
        grid.setContentsMargins(16, 0, 0, 0)
        rows = (len(CLASSES) + _COLUMNS - 1) // _COLUMNS
        for index, (code, label) in enumerate(CLASSES):
            box = QCheckBox(f"{code} {label}" if code < 19 else label)
            box.setChecked(True)
            box.toggled.connect(self._refresh)
            self.class_boxes[code] = box
            grid.addWidget(box, index % rows, index // rows)

        self.return_buttons: Dict[str, QRadioButton] = {}
        returns_row = QHBoxLayout()
        returns_row.setContentsMargins(16, 0, 0, 0)
        for choice, label in RETURN_CHOICES:
            button = QRadioButton(label)
            button.setChecked(choice == ALL_RETURNS)
            button.toggled.connect(self._refresh)
            self.return_buttons[choice] = button
            returns_row.addWidget(button)
        returns_row.addStretch(1)

        presets_row = QHBoxLayout()
        presets_row.setContentsMargins(16, 0, 0, 0)
        presets_row.addWidget(QLabel("Classes:"))
        for name, codes in PRESETS.items():
            button = QPushButton(name)
            button.clicked.connect(lambda _checked=False, c=codes: self.set_classes(c))
            presets_row.addWidget(button)
        presets_row.addStretch(1)

        hint = QLabel(
            "Applies to Add to map, Virtual Point Cloud and Download. A streamed Virtual Point Cloud can't be "
            "filtered, so with a filter on, those tiles are downloaded and filtered first."
        )
        hint.setWordWrap(True)
        hint.setContentsMargins(16, 0, 0, 0)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addLayout(presets_row)
        body_layout.addLayout(grid)
        body_layout.addLayout(returns_row)
        body_layout.addWidget(hint)
        self.body.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.header)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addWidget(self.body)
        self._refresh()

    # ---- state -------------------------------------------------------------------------------

    def _toggle(self, open_: bool):
        self.body.setVisible(open_)
        self.header.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)

    def value(self) -> PointFilter:
        returns = next((c for c, b in self.return_buttons.items() if b.isChecked()), ALL_RETURNS)
        return PointFilter(classes=frozenset(c for c, b in self.class_boxes.items() if b.isChecked()), returns=returns)

    def set_classes(self, codes) -> None:
        for code, box in self.class_boxes.items():
            box.blockSignals(True)
            box.setChecked(code in codes)
            box.blockSignals(False)
        self._refresh()

    def set_returns(self, choice: str) -> None:
        self.return_buttons[choice].setChecked(True)

    def reset(self) -> None:
        self.set_classes(ALL_CLASSES)
        self.set_returns(ALL_RETURNS)

    def _refresh(self, *_):
        current = self.value()
        if current.empty:
            text = "Point filter (no class ticked)"
        elif current.active:
            text = f"Point filter ({current.summary()})"
        else:
            text = "Point filter"
        self.header.setText(text)
        self.header.setToolTip("Choose which points to keep: by classification (ground, vegetation, ...) and first, last or all returns")
        self.header.setArrowType(Qt.ArrowType.DownArrow if self.header.isChecked() else Qt.ArrowType.RightArrow)
        self.changed.emit()
