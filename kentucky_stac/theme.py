"""Checkbox styling that stays visible in both light and dark themes.

QGIS's own dark themes (Night Mapping, Blend of Gray) draw an unticked box in a colour barely lighter than
the list behind it, and their rules name the state (`:unchecked`), so a plain `::indicator` rule loses to
them. This restates every state, marked `!important`, with colours taken from the palette: a clear outline
in the text colour, filled with the highlight colour and a white tick when ticked.
"""

from __future__ import annotations

import os

_CHECK = os.path.join(os.path.dirname(__file__), "check.svg").replace("\\", "/")

_VIEWS = ("QCheckBox", "QListView", "QTreeView", "QAbstractItemView")


def _rule(state: str, body: str) -> str:
    selectors = ",\n".join(f"{view}::indicator{state}" for view in _VIEWS)
    return f"{selectors} {{\n{body}\n}}\n"


def checkbox_style() -> str:
    return "".join(
        [
            _rule(
                "",
                "    width: 13px; height: 13px;\n"
                "    border: 1px solid palette(text) !important;\n"
                "    border-radius: 2px;\n"
                "    background-color: palette(base) !important;\n"
                "    image: none;",
            ),
            _rule(":unchecked", "    border: 1px solid palette(text) !important;\n    background-color: palette(base) !important;\n    image: none !important;"),
            _rule(":unchecked:hover", "    border: 1px solid palette(highlight) !important;"),
            _rule(
                ":checked",
                "    border: 1px solid palette(highlight) !important;\n"
                "    background-color: palette(highlight) !important;\n"
                f"    image: url({_CHECK}) !important;",
            ),
            _rule(":unchecked:disabled", "    border: 1px solid palette(mid) !important;\n    background-color: palette(window) !important;\n    image: none !important;"),
            _rule(
                ":checked:disabled",
                "    border: 1px solid palette(mid) !important;\n"
                "    background-color: palette(mid) !important;\n"
                f"    image: url({_CHECK}) !important;",
            ),
        ]
    )
