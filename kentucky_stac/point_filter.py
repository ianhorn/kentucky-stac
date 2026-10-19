"""Which points of a point cloud to keep: by LAS classification and by return (first, last or all). Pure
Python -- no QGIS import -- so the rules can be tested on their own; point_filter_panel.py is the widget
that edits one.

A filter turns into two spellings of the same rule:
 - a QGIS point cloud filter expression (`Classification IN (2,6) AND ReturnNumber = 1`), set on a streamed
   layer with QgsPointCloudLayer.setSubsetString. Tested on QGIS 3.44 and 4.2: it hides the other points
   from drawing. It does NOT work on a virtual point cloud (.vpc) layer -- the string is accepted and ignored.
 - PDAL rules, used to write a filtered copy of a downloaded tile: `filters.range` limits
   (`Classification[2:2],Classification[6:6],ReturnNumber[1:1]`; several limits on one dimension are ORed,
   limits on different dimensions ANDed) and, for the last return, a `filters.expression` comparing two
   dimensions (`ReturnNumber == NumberOfReturns`), which a range can't say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, List, Tuple

# The ASPRS LAS classes. 12 is "reserved" in LAS 1.4 (it was "overlap" in 1.1); OTHER stands for every code
# above 18 (19-63 reserved, 64-255 user-defined), so unticking the named ones can't silently drop those.
OTHER = 19
CLASSES: Tuple[Tuple[int, str], ...] = (
    (0, "Never classified"),
    (1, "Unclassified"),
    (2, "Ground"),
    (3, "Low vegetation"),
    (4, "Medium vegetation"),
    (5, "High vegetation"),
    (6, "Building"),
    (7, "Low point (noise)"),
    (8, "Model key / reserved"),
    (9, "Water"),
    (10, "Rail"),
    (11, "Road surface"),
    (12, "Overlap / reserved"),
    (13, "Wire guard"),
    (14, "Wire conductor"),
    (15, "Transmission tower"),
    (16, "Wire connector"),
    (17, "Bridge deck"),
    (18, "High noise"),
    (OTHER, "Other (19 and up)"),
)
ALL_CLASSES: FrozenSet[int] = frozenset(code for code, _ in CLASSES)

# Which returns to keep. A pulse with one return counts as both its first and its last.
ALL_RETURNS, FIRST_RETURN, LAST_RETURN = "all", "first", "last"
RETURN_CHOICES: Tuple[Tuple[str, str], ...] = (
    (ALL_RETURNS, "All returns"),
    (FIRST_RETURN, "First return"),
    (LAST_RETURN, "Last return"),
)

# One-click choices for the class boxes.
PRESETS = {
    "All": ALL_CLASSES,
    "Ground only": frozenset({2}),
    "Without noise": ALL_CLASSES - {7, 18},
    "Vegetation": frozenset({3, 4, 5}),
    "Buildings": frozenset({6}),
}


@dataclass(frozen=True)
class PointFilter:
    classes: FrozenSet[int] = ALL_CLASSES
    returns: str = ALL_RETURNS

    @property
    def active(self) -> bool:
        """True when the filter drops anything at all."""
        return self.classes != ALL_CLASSES or self.returns != ALL_RETURNS

    @property
    def empty(self) -> bool:
        """True when no class is ticked, so nothing would be left."""
        return not self.classes

    # ---- QGIS -----------------------------------------------------------------------------------

    def qgis_expression(self) -> str:
        """A QGIS point cloud filter expression, or "" when nothing is filtered out."""
        if not self.active or self.empty:
            return ""
        parts: List[str] = []
        if self.classes != ALL_CLASSES:
            parts.append(_or(_ranges(self.classes)))
        if self.returns == FIRST_RETURN:
            parts.append("ReturnNumber = 1")
        elif self.returns == LAST_RETURN:
            parts.append("ReturnNumber = NumberOfReturns")
        return " AND ".join(parts)

    # ---- PDAL -----------------------------------------------------------------------------------

    def pdal_limits(self) -> str:
        """The `limits` option of PDAL's filters.range, or "" when there is no range to apply."""
        if self.empty:
            return ""
        limits: List[str] = []
        if self.classes != ALL_CLASSES:
            limits += [f"Classification[{low}:{high}]" for low, high in _runs(self.classes)]
        if self.returns == FIRST_RETURN:
            limits.append("ReturnNumber[1:1]")
        return ",".join(limits)

    def pdal_expression(self) -> str:
        """The `expression` option of PDAL's filters.expression (needed for the last return), or ""."""
        return "ReturnNumber == NumberOfReturns" if self.returns == LAST_RETURN and not self.empty else ""

    # ---- people ---------------------------------------------------------------------------------

    def summary(self) -> str:
        """A short description for the panel header and messages: "Ground, Building; first return"."""
        if not self.active:
            return "all points"
        names = dict(CLASSES)
        parts: List[str] = []
        if self.classes != ALL_CLASSES:
            chosen = [names[c] for c in sorted(self.classes)]
            if len(chosen) > 3:
                parts.append(f"{len(chosen)} classes")
            else:
                parts.append(", ".join(chosen) or "no class")
        if self.returns != ALL_RETURNS:
            parts.append(f"{self.returns} return")
        return "; ".join(parts)


def _runs(chosen: FrozenSet[int]) -> List[Tuple[int, int]]:
    """The chosen codes as (low, high) runs of neighbouring codes. The OTHER box stands for everything from
    19 up to 255."""
    runs: List[Tuple[int, int]] = []
    for code in sorted(chosen):
        high = 255 if code == OTHER else code
        if runs and runs[-1][1] + 1 == code:
            runs[-1] = (runs[-1][0], high)
        else:
            runs.append((code, high))
    return runs


def _ranges(chosen: FrozenSet[int]) -> List[str]:
    out = []
    for low, high in _runs(chosen):
        if low == high:
            out.append(f"Classification = {low}")
        elif high == 255:
            out.append(f"Classification >= {low}")
        else:
            out.append(f"Classification >= {low} AND Classification <= {high}")
    return out


def _or(clauses: List[str]) -> str:
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " OR ".join(f"({c})" if " AND " in c else c for c in clauses) + ")"
