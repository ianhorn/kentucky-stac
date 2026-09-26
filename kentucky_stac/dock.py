from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QTabWidget, QVBoxLayout, QWidget


def _placeholder(text):
    page = QWidget()
    layout = QVBoxLayout(page)
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    layout.addWidget(label)
    return page


class KentuckyStacDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Kentucky STAC", parent)
        self.setObjectName("KentuckyStacDock")

        self.tabs = QTabWidget()
        self.tabs.addTab(_placeholder("Imagery / DEM search goes here."), "Imagery / DEM")
        self.tabs.addTab(_placeholder("LiDAR point cloud search goes here."), "LiDAR Pointcloud")
        self.setWidget(self.tabs)
