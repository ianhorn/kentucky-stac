"""The "Custom formula" dialog for right-click > Add index, and the formulas it remembers."""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .bands import FUNCTIONS, INDICES, check_formula

_KEY = "kentucky_stac/custom_indices"


def saved_formulas() -> Dict[str, str]:
    """The user's own formulas, name -> formula."""
    try:
        data = json.loads(str(QgsSettings().value(_KEY, "") or "{}"))
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _store(formulas: Dict[str, str]) -> None:
    QgsSettings().setValue(_KEY, json.dumps(formulas))


class IndexDialog(QDialog):
    def __init__(self, band_names: Sequence[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom index formula")
        self.setMinimumWidth(460)
        self._saved = saved_formulas()
        hint = QLabel(
            "Write a formula over band names, for example <code>(nir - red) / (nir + red)</code>. "
            "Use + - * / ^ ( ) and numbers, plus the functions " + ", ".join(FUNCTIONS) + ".<br>"
            "Bands this tile offers: <b>" + (", ".join(band_names) or "none found") + "</b>."
        )
        hint.setWordWrap(True)
        self.saved_combo = QComboBox()
        self.saved_combo.addItem("(a new formula)", None)
        for name, formula in self._saved.items():
            self.saved_combo.addItem(name, formula)
        self.saved_combo.currentIndexChanged.connect(self._load_saved)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_saved)
        saved_row = QHBoxLayout()
        saved_row.addWidget(self.saved_combo, 1)
        saved_row.addWidget(self.delete_button)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Green ratio")
        self.formula_edit = QLineEdit()
        self.formula_edit.setPlaceholderText("(nir - red) / (nir + red)")
        self.formula_edit.textChanged.connect(self._validate)
        self.name_edit.textChanged.connect(self._validate)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #e04a3a;")
        self.error_label.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Saved", saved_row)
        form.addRow("Name", self.name_edit)
        form.addRow("Formula", self.formula_edit)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)
        self._validate()

    def _load_saved(self) -> None:
        formula = self.saved_combo.currentData()
        self.delete_button.setEnabled(formula is not None)
        if formula is not None:
            self.name_edit.setText(self.saved_combo.currentText())
            self.formula_edit.setText(formula)

    def _delete_saved(self) -> None:
        name = self.saved_combo.currentText()
        if name in self._saved:
            del self._saved[name]
            _store(self._saved)
            self.saved_combo.removeItem(self.saved_combo.currentIndex())

    def _problem(self) -> Optional[str]:
        name = self.name_edit.text().strip()
        if not name:
            return "Give the formula a name."
        if name in INDICES:
            return "That name is already used by a built-in index."
        return check_formula(self.formula_edit.text())

    def _validate(self) -> None:
        problem = self._problem() if (self.formula_edit.text() or self.name_edit.text()) else None
        self.error_label.setText(problem or "")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not self._problem())

    def _accept(self) -> None:
        self._saved[self.name_edit.text().strip()] = self.formula_edit.text().strip()
        _store(self._saved)  # remembered for the next tile
        self.accept()

    def result_formula(self) -> Tuple[str, str]:
        return self.name_edit.text().strip(), self.formula_edit.text().strip()

    @staticmethod
    def ask(band_names: Sequence[str], parent=None) -> Optional[Tuple[str, str]]:
        dialog = IndexDialog(band_names, parent)
        return dialog.result_formula() if dialog.exec() else None
