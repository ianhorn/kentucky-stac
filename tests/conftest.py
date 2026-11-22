"""Fixtures shared by the tests that need QGIS (those modules skip themselves when it isn't installed)."""

import os

import pytest


@pytest.fixture(scope="session")
def qgis_app():
    """One QgsApplication for the whole run: a second one started in the same process crashes on exit."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qgis.core import QgsApplication

    app = QgsApplication([], False)
    app.initQgis()
    yield app
    app.exitQgis()
