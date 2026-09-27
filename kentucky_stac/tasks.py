"""Background tasks, so network calls never block the QGIS UI."""

from __future__ import annotations

from typing import Callable, List, Optional

from qgis.core import QgsTask

from .qgis_transport import qgis_transport
from .stac import Collection, StacClient


class CollectionsTask(QgsTask):
    """Fetch the catalog's collections. `callback(collections, error)` runs on the main thread
    when the task finishes, unless the task was cancelled."""

    def __init__(self, base_uri: str, callback: Callable[[List[Collection], Optional[str]], None]):
        super().__init__("Loading Kentucky STAC collections")
        self._base_uri = base_uri
        self._callback = callback
        self.collections: List[Collection] = []
        self.error: Optional[str] = None

    def run(self) -> bool:
        # Worker thread: no GUI access here.
        try:
            self.collections = StacClient(self._base_uri, qgis_transport).collections()
            return True
        except Exception as e:  # report any failure to the UI instead of losing it in the thread
            self.error = str(e)
            return False

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        self._callback(self.collections if result else [], self.error)
