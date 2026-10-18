"""Checkbox styling that stays visible in both light and dark themes.

Left to the native style, a check box can nearly vanish on a dark background (an unticked box drawn in
the background colour), which hid the collection checkboxes. This draws every check box in the dock the
same way from the palette: a clear outline in the text colour, filled with the highlight colour and a
white tick when ticked.
"""

from __future__ import annotations

import os

_CHECK = os.path.join(os.path.dirname(__file__), "check.svg").replace("\\", "/")


def checkbox_style() -> str:
    return f"""
QCheckBox::indicator, QListView::indicator, QTreeView::indicator {{
    width: 13px; height: 13px;
    border: 1px solid palette(text);
    border-radius: 2px;
    background: palette(base);
}}
QCheckBox::indicator:hover, QListView::indicator:hover, QTreeView::indicator:hover {{
    border: 1px solid palette(highlight);
}}
QCheckBox::indicator:checked, QListView::indicator:checked, QTreeView::indicator:checked {{
    background: palette(highlight);
    border: 1px solid palette(highlight);
    image: url({_CHECK});
}}
QCheckBox::indicator:disabled, QListView::indicator:disabled, QTreeView::indicator:disabled {{
    border: 1px solid palette(mid);
    background: palette(window);
}}
QCheckBox::indicator:checked:disabled, QListView::indicator:checked:disabled, QTreeView::indicator:checked:disabled {{
    background: palette(mid);
}}
"""
