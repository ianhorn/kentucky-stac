"""One place to record unexpected failures.

Handlers that deliberately catch every exception (a QgsTask worker, a toolbar button) have to keep
QGIS running, but the message they show the user is only `str(error)`. `log_exception` also writes the
full traceback to the QGIS Log Messages panel (tab "Kentucky STAC"), which is what a bug report needs.
QgsMessageLog is safe to call from worker threads.
"""

from __future__ import annotations

import traceback

from qgis.core import Qgis, QgsMessageLog

LOG_TAG = "Kentucky STAC"


def log_exception(context: str) -> None:
    """Log `context` and the traceback of the exception being handled (call from inside an `except`)."""
    QgsMessageLog.logMessage(f"{context}\n{traceback.format_exc()}", LOG_TAG, Qgis.MessageLevel.Warning)
