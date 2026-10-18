"""Checkbox styling that stays visible in both light and dark themes.

QGIS's dark themes (Night Mapping, Blend of Gray) draw an unticked box in a colour barely lighter than
the list behind it, and their rules name the state (`:unchecked`), so a plain `::indicator` rule loses to
them. This restates every state, marked `!important`: a muted outline (the text colour blended part of the
way into the background, so it is clear without glaring), filled with the highlight colour and a white
tick when ticked.
"""

from __future__ import annotations

import os

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication

_CHECK = os.path.join(os.path.dirname(__file__), "check.svg").replace("\\", "/")
_SIZE = 11  # pixels

_VIEWS = ("QCheckBox", "QListView", "QTreeView", "QAbstractItemView")


def mix_colors(a: QColor, b: QColor, amount: float) -> QColor:
    """`amount` of colour a blended into b (0 = all b, 1 = all a)."""
    return QColor(
        round(a.red() * amount + b.red() * (1 - amount)),
        round(a.green() * amount + b.green() * (1 - amount)),
        round(a.blue() * amount + b.blue() * (1 - amount)),
    )


def _rule(state: str, body: str) -> str:
    selectors = ",\n".join(f"{view}::indicator{state}" for view in _VIEWS)
    return f"{selectors} {{\n{body}\n}}\n"


def checkbox_style() -> str:
    palette = QApplication.palette()
    text, base, window = palette.text().color(), palette.base().color(), palette.window().color()
    outline = mix_colors(text, base, 0.5).name()  # muted, but clearly lighter than a dark list
    hover = mix_colors(text, base, 0.8).name()
    faint = mix_colors(text, window, 0.25).name()
    return "".join(
        [
            _rule(
                "",
                f"    width: {_SIZE}px; height: {_SIZE}px;\n"
                f"    border: 1px solid {outline} !important;\n"
                "    border-radius: 2px;\n"
                "    background-color: palette(base) !important;\n"
                "    image: none;",
            ),
            _rule(":unchecked", f"    border: 1px solid {outline} !important;\n    background-color: palette(base) !important;\n    image: none !important;"),
            _rule(":unchecked:hover", f"    border: 1px solid {hover} !important;"),
            _rule(
                ":checked",
                "    border: 1px solid palette(highlight) !important;\n"
                "    background-color: palette(highlight) !important;\n"
                f"    image: url({_CHECK}) !important;",
            ),
            _rule(":unchecked:disabled", f"    border: 1px solid {faint} !important;\n    background-color: palette(window) !important;\n    image: none !important;"),
            _rule(
                ":checked:disabled",
                f"    border: 1px solid {faint} !important;\n"
                f"    background-color: {faint} !important;\n"
                f"    image: url({_CHECK}) !important;",
            ),
        ]
    )
