"""Imagery and elevation rasters are drawn with bilinear resampling; class-coded (palette) rasters are not."""

import os
import time

import pytest

pytest.importorskip("qgis.core", reason="needs QGIS (qgis.core)")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402

from kentucky_stac.layers import AddLayersTask, LayerSpec, apply_default_resampling  # noqa: E402

gdal = pytest.importorskip("osgeo.gdal", reason="needs GDAL's Python bindings")


def _tif(path, bands=3, palette=False):
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(str(path), 16, 16, bands, gdal.GDT_Byte)
    dataset.SetGeoTransform((500000.0, 10.0, 0.0, 4200000.0, 0.0, -10.0))
    for band in range(1, bands + 1):
        dataset.GetRasterBand(band).Fill(40 * band)
    if palette:
        table = gdal.ColorTable()
        table.SetColorEntry(1, (200, 0, 0, 255))
        table.SetColorEntry(2, (0, 200, 0, 255))
        dataset.GetRasterBand(1).SetRasterColorTable(table)
        dataset.GetRasterBand(1).SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
    dataset = None
    return str(path)


def _resamplers(layer):
    resample = layer.resampleFilter()
    zoomed_in, zoomed_out = resample.zoomedInResampler(), resample.zoomedOutResampler()
    return (zoomed_in.type() if zoomed_in else None, zoomed_out.type() if zoomed_out else None)


def test_an_rgb_raster_gets_bilinear_both_ways(qgis_app, tmp_path):
    layer = QgsRasterLayer(_tif(tmp_path / "rgb.tif"), "rgb", "gdal")
    assert layer.isValid() and _resamplers(layer) == (None, None)  # QGIS's own default is nearest neighbour
    assert apply_default_resampling(layer) is True
    assert _resamplers(layer) == ("bilinear", "bilinear")


def test_a_single_band_elevation_raster_gets_bilinear(qgis_app, tmp_path):
    layer = QgsRasterLayer(_tif(tmp_path / "dem.tif", bands=1), "dem", "gdal")
    assert apply_default_resampling(layer) is True
    assert _resamplers(layer) == ("bilinear", "bilinear")


def test_a_palette_raster_keeps_nearest_neighbour(qgis_app, tmp_path):
    layer = QgsRasterLayer(_tif(tmp_path / "classes.tif", bands=1, palette=True), "classes", "gdal")
    assert layer.renderer().type() == "paletted"
    assert apply_default_resampling(layer) is False
    assert _resamplers(layer) == (None, None)


def test_something_that_is_not_a_raster_is_left_alone(qgis_app):
    assert apply_default_resampling(None) is False


def test_rasters_added_to_the_map_come_out_bilinear(qgis_app, tmp_path):
    """Through the add-to-map task itself, as the Add buttons run it."""
    specs = [LayerSpec("rgb", _tif(tmp_path / "a.tif"), "gdal"), LayerSpec("classes", _tif(tmp_path / "b.tif", 1, True), "gdal")]
    done = []
    task = AddLayersTask(specs, lambda added, failed: done.append((added, failed)))
    QgsApplication.taskManager().addTask(task)
    end = time.time() + 60
    while not done and time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    assert done == [(2, [])]
    layers = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values()}
    assert _resamplers(layers["rgb"]) == ("bilinear", "bilinear")
    assert _resamplers(layers["classes"]) == (None, None)
    QgsProject.instance().clear()
