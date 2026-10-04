"""The optional parameters of QGIS's "Build virtual point cloud (VPC)" algorithm (pdal:virtualpointcloud),
asked for when the user combines tiles into a virtual point cloud.

With every option off, vpc.py writes the index straight from the STAC metadata (instant, nothing read).
With any option on, the real algorithm runs instead -- it has to read every point of every tile, so it
takes a while.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from qgis.core import (
    QgsApplication,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsSettings,
)
from qgis.PyQt.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout

ALGORITHM_ID = "pdal:virtualpointcloud"
_SETTINGS = "kentucky_stac/vpc_{}"


@dataclass(frozen=True)
class VpcOptions:
    boundary: bool = False  # calculate boundary polygons (exact data outline, not just the rectangle)
    statistics: bool = False  # calculate per-attribute statistics
    overview: bool = False  # build a thinned overview point cloud (every 1000th point)

    @property
    def any(self) -> bool:
        return self.boundary or self.statistics or self.overview


_CHOICES = (
    ("boundary", "Calculate boundary polygons", "Store each tile's exact data boundary instead of just its rectangular extent."),
    ("statistics", "Calculate statistics", "Store the range of values of each attribute, so QGIS can style by them straight away."),
    ("overview", "Build overview point cloud", "Also write a thinned overview (every 1000th point) next to the .vpc, drawn when zoomed out."),
)


class VpcOptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Virtual point cloud options")
        self.setMinimumWidth(420)
        saved = QgsSettings()
        intro = QLabel(
            "Leave everything unchecked for the fast default: the index is built from the catalog's "
            "metadata and no points are read.<br><br><b>Any option below makes QGIS read every point of every "
            "downloaded tile</b>, which takes longer."
        )
        intro.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        self.boxes: Dict[str, QCheckBox] = {}
        for key, label, tip in _CHOICES:
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.setChecked(str(saved.value(_SETTINGS.format(key), "false")).lower() == "true")
            self.boxes[key] = box
            layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> VpcOptions:
        return VpcOptions(**{k: b.isChecked() for k, b in self.boxes.items()})

    @staticmethod
    def ask(parent=None) -> Optional[VpcOptions]:
        """The chosen options, or None if the user cancelled. The choice is remembered."""
        dialog = VpcOptionsDialog(parent)
        if not dialog.exec():
            return None
        options = dialog.options()
        settings = QgsSettings()
        for key, box in dialog.boxes.items():
            settings.setValue(_SETTINGS.format(key), box.isChecked())
        return options


def start_build(
    sources: List[str], output: str, options: VpcOptions, callback: Callable[[bool, str], None]
) -> Optional[QgsProcessingAlgRunnerTask]:
    """Run QGIS's Build virtual point cloud algorithm in the background over `sources` (file paths or
    URLs). `callback(ok, message)` runs on the main thread when it ends. Returns the task (hold on to it),
    or None -- after calling back with the reason -- if the algorithm isn't available."""
    algorithm = QgsApplication.processingRegistry().createAlgorithmById(ALGORITHM_ID)
    if algorithm is None:
        callback(False, "QGIS's PDAL processing provider isn't available, so these options can't be used")
        return None
    context = QgsProcessingContext()
    feedback = QgsProcessingFeedback()
    params = {
        "LAYERS": list(sources),
        "BOUNDARY": options.boundary,
        "STATISTICS": options.statistics,
        "OVERVIEW": options.overview,
        "OUTPUT": output,
    }
    task = QgsProcessingAlgRunnerTask(algorithm, params, context, feedback)

    def done(ok, results):
        callback(bool(ok), "" if ok else (feedback.htmlLog() or "the algorithm failed"))

    task.executed.connect(done)
    task._keep_alive = (context, feedback)  # the task only borrows them
    QgsApplication.taskManager().addTask(task)
    return task
