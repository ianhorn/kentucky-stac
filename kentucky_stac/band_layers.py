"""Layers made from a tile's separate bands: band combinations and spectral indices.

* A **band combination** (true color, color infrared, SWIR...) is a small VRT stacking three bands, each
  from its own remote file, drawn as a multiband color layer with a percentile contrast stretch.
* An **index** (NDVI, NDWI, your own formula...) is a QGIS *virtual raster*: the formula is evaluated by QGIS
  as the map draws, straight from the remote files. No GDAL pixel-function plugin is needed, nothing is
  written, and inputs at different resolutions are resampled for you.

Which bands a tile has, and the formulas, are in bands.py. The builders here return a LayerSpec carrying a
`factory`, which AddLayersTask runs on a worker thread (opening remote files is slow).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from qgis.core import (
    QgsApplication,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsMapLayer,
    QgsMultiBandColorRenderer,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtGui import QColor

from . import auth, s3, signing
from .bands import COMBINATIONS, Band, bands_for_formula, find_band, to_expression
from .layers import LayerSpec, item_bbox, raster_uri
from .stac import Item


def _enum(cls, scope: str, name: str):
    """A QGIS enum member under either spelling: Class.Scope.Name (QGIS 4) or Class.Name (older)."""
    holder = getattr(cls, scope, None)
    return getattr(holder, name) if holder is not None and hasattr(holder, name) else getattr(cls, name)


# ---- opening a band's file -------------------------------------------------------------------------


def _gdal_path(href: str, authcfg: str = "") -> str:
    """A GDAL path for the file, for a VRT (the usual /vsicurl form; the VRT writer escapes any &)."""
    return raster_uri(href, authcfg)


def _calc_path(href: str, authcfg: str = "") -> str:
    """A GDAL path with no & of its own, for a virtual-raster URI (where & separates the parameters)."""
    if s3.is_s3(href) and (authcfg or s3.credentials_available()):
        return s3.vsis3_path(href)
    return "/vsicurl/" + signing.fetchable_url(href)


def _wrapper_vrt(href: str, authcfg: str = "") -> str:
    """A one-line VRT pointing at the remote file, so a virtual-raster URI can name a plain local path.

    A signed URL put straight into that URI is mangled by the layers of decoding it goes through (the `?`,
    `&` and `%` of an Azure token), so the URL goes inside a VRT file instead, where it is just XML text.
    """
    from osgeo import gdal

    source = _calc_path(href, authcfg)
    out = os.path.join(_vrt_directory(), "src_" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:16] + ".vrt")
    if not os.path.exists(out):
        dataset = gdal.Translate(out, source, format="VRT")
        if dataset is None:
            raise RuntimeError("could not open " + href)
        dataset = None  # flush the file
    return out


def _local_uri_value(path: str) -> str:
    """A local path inside a virtual-raster URI: as is, except that an & needs escaping twice."""
    path = path.replace("\\", "/")
    return path.replace("&", "%2526") if "&" in path else path


def _crs_text(layer: QgsRasterLayer, item: Item) -> str:
    authid = layer.crs().authid()
    if authid:
        return authid
    epsg = item.properties.get("proj:epsg")
    return f"EPSG:{epsg}" if epsg else layer.crs().toWkt()


# ---- band combinations -----------------------------------------------------------------------------


def _vrt_directory() -> str:
    path = os.path.join(QgsApplication.qgisSettingsDirPath(), "kentucky_stac", "vrt")
    os.makedirs(path, exist_ok=True)
    return path


def _write_stack_vrt(sources: Sequence[Tuple[str, int]], out_path: str) -> None:
    """A VRT with one band per (file, band number), on the finest grid among the files."""
    from osgeo import gdal

    opened = {}
    for path, _ in sources:
        if path not in opened:
            dataset = gdal.Open(path)
            if dataset is None:
                raise RuntimeError("could not open " + path)
            opened[path] = dataset
    grids = {p: (d.RasterXSize, d.RasterYSize, d.GetGeoTransform()) for p, d in opened.items()}
    ref_path = min(grids, key=lambda p: abs(grids[p][2][1]))  # smallest pixel width
    width, height, gt = grids[ref_path]
    west, north = gt[0], gt[3]
    east, south = west + gt[1] * width, north + gt[5] * height
    for path, (w, h, g) in grids.items():
        tolerance = abs(g[1]) * 1.5
        if abs(g[0] - west) > tolerance or abs(g[3] - north) > tolerance or abs(g[0] + g[1] * w - east) > tolerance:
            raise RuntimeError("the bands cover different areas, so they can't be combined")
    srs = opened[ref_path].GetProjection()
    colors = ("Red", "Green", "Blue") if len(sources) == 3 else ()
    bands_xml = []
    for number, (path, band_no) in enumerate(sources, start=1):
        dataset = opened[path]
        src_w, src_h, _ = grids[path]
        band = dataset.GetRasterBand(band_no)
        block_x, block_y = band.GetBlockSize()
        dtype = gdal.GetDataTypeName(band.DataType)
        nodata = band.GetNoDataValue()
        bands_xml.append(
            f'<VRTRasterBand dataType="{dtype}" band="{number}">'
            + (f"<ColorInterp>{colors[number - 1]}</ColorInterp>" if colors else "")
            + (f"<NoDataValue>{nodata}</NoDataValue>" if nodata is not None else "")
            + f'<SimpleSource><SourceFilename relativeToVRT="0">{escape(path)}</SourceFilename>'
            f"<SourceBand>{band_no}</SourceBand>"
            f'<SourceProperties RasterXSize="{src_w}" RasterYSize="{src_h}" DataType="{dtype}" '
            f'BlockXSize="{block_x}" BlockYSize="{block_y}"/>'
            f'<SrcRect xOff="0" yOff="0" xSize="{src_w}" ySize="{src_h}"/>'
            f'<DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>'
            "</SimpleSource></VRTRasterBand>"
        )
    xml = (
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">'
        f"<SRS>{escape(srs)}</SRS>"
        f"<GeoTransform>{', '.join(repr(v) for v in gt)}</GeoTransform>" + "".join(bands_xml) + "</VRTDataset>"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)


def _style_color(layer: QgsRasterLayer) -> None:
    provider = layer.dataProvider()
    renderer = QgsMultiBandColorRenderer(provider, 1, 2, 3)
    algorithm = _enum(QgsContrastEnhancement, "ContrastEnhancementAlgorithm", "StretchToMinimumMaximum")
    for channel, setter in enumerate((renderer.setRedContrastEnhancement, renderer.setGreenContrastEnhancement, renderer.setBlueContrastEnhancement), start=1):
        enhancement = QgsContrastEnhancement(provider.dataType(channel))
        enhancement.setContrastEnhancementAlgorithm(algorithm)
        setter(enhancement)
    origin = QgsRasterMinMaxOrigin()
    origin.setLimits(_enum(QgsRasterMinMaxOrigin, "Limits", "CumulativeCut"))
    origin.setCumulativeCutLower(0.02)
    origin.setCumulativeCutUpper(0.98)
    origin.setStatAccuracy(_enum(QgsRasterMinMaxOrigin, "StatAccuracy", "Estimated"))
    origin.setExtent(_enum(QgsRasterMinMaxOrigin, "Extent", "WholeRaster"))
    renderer.setMinMaxOrigin(origin)
    layer.setRenderer(renderer)


def stack_spec(item: Item, asset_keys: Sequence[str], bands: Sequence[Band]) -> Optional[LayerSpec]:
    """One layer holding every band of the chosen assets, in the order given. Three bands draw as a color image."""
    chosen = [b for key in asset_keys for b in bands if b.asset == key]
    if len(chosen) < 2:
        return None
    cfg = auth.cfg_for_source(item.source)
    sources = [(_gdal_path(b.href, cfg), b.index) for b in chosen]
    key = hashlib.sha1(json.dumps([item.id, "stack", sources]).encode("utf-8")).hexdigest()[:16]
    layer_name = f"{item.id} · {len(chosen)}-band stack ({', '.join(asset_keys)})"

    def factory() -> Optional[QgsMapLayer]:
        out_path = os.path.join(_vrt_directory(), f"{key}.vrt")
        if not os.path.exists(out_path):
            _write_stack_vrt(sources, out_path)
        layer = QgsRasterLayer(out_path, layer_name, "gdal")
        if layer.isValid() and len(chosen) == 3:
            _style_color(layer)
        return layer

    return LayerSpec(layer_name, f"stack:{key}", "gdal", item_bbox(item), factory)


def combination_spec(item: Item, name: str, bands: Sequence[Band]) -> Optional[LayerSpec]:
    """A layer spec for a band combination of this tile, or None if it lacks a band the combination needs."""
    picked = [find_band(bands, channel) for channel in COMBINATIONS[name]]
    if any(b is None for b in picked):
        return None
    cfg = auth.cfg_for_source(item.source)
    sources = [(_gdal_path(b.href, cfg), b.index) for b in picked]
    key = hashlib.sha1(json.dumps([item.id, name, sources]).encode("utf-8")).hexdigest()[:16]
    layer_name = f"{item.id} · {name}"

    def factory() -> Optional[QgsMapLayer]:
        out_path = os.path.join(_vrt_directory(), f"{key}.vrt")
        if not os.path.exists(out_path):
            _write_stack_vrt(sources, out_path)
        layer = QgsRasterLayer(out_path, layer_name, "gdal")
        if layer.isValid():
            _style_color(layer)
        return layer

    return LayerSpec(layer_name, f"combination:{key}", "gdal", item_bbox(item), factory)


# ---- indices ---------------------------------------------------------------------------------------


def _style_index(layer: QgsRasterLayer) -> None:
    ramp = QgsGradientColorRamp(
        QColor(215, 25, 28),
        QColor(26, 150, 65),
        False,
        [QgsGradientStop(0.25, QColor(253, 174, 97)), QgsGradientStop(0.5, QColor(255, 255, 191)), QgsGradientStop(0.75, QColor(166, 217, 106))],
    )
    shader_function = QgsColorRampShader(-1.0, 1.0, ramp, _enum(QgsColorRampShader, "Type", "Interpolated"))
    shader_function.classifyColorRamp(9)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_function)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(-1.0)
    renderer.setClassificationMax(1.0)
    layer.setRenderer(renderer)


def index_spec(item: Item, name: str, formula: str, bands: Sequence[Band]) -> Optional[LayerSpec]:
    """A layer spec computing `formula` over this tile's bands, or None if the tile lacks one of them."""
    needed = bands_for_formula(formula, bands)
    if not needed:
        return None
    hrefs: List[str] = []
    for band in needed:
        if band.href not in hrefs:
            hrefs.append(band.href)
    prefix: Dict[str, str] = {href: f"b{i}" for i, href in enumerate(hrefs)}
    cfg = auth.cfg_for_source(item.source)
    for href in hrefs:
        auth.prepare_asset(href, cfg)  # on the main thread: GDAL gets this source's login for the file's host
    try:
        expression = to_expression(formula, lambda band: f'"{prefix[band.href]}@{band.index}"', bands)
    except ValueError:
        return None
    layer_name = f"{item.id} · {name}"
    key = hashlib.sha1(json.dumps([item.id, formula, hrefs]).encode("utf-8")).hexdigest()[:16]

    def factory() -> Optional[QgsMapLayer]:
        inputs, wrappers = {}, {}
        for href in hrefs:
            wrappers[href] = _wrapper_vrt(href, cfg)
            layer = QgsRasterLayer(wrappers[href], "input", "gdal")
            if not layer.isValid():
                raise RuntimeError("could not open " + href)
            inputs[href] = layer
        finest = min(inputs.values(), key=lambda lyr: lyr.extent().width() / max(lyr.width(), 1))
        extent = finest.extent()
        uri = (
            f"?crs={_crs_text(finest, item)}"
            f"&extent={extent.xMinimum()},{extent.yMinimum()},{extent.xMaximum()},{extent.yMaximum()}"
            f"&width={finest.width()}&height={finest.height()}"
            f"&formula={expression}"
            + "".join(f"&{prefix[h]}:uri={_local_uri_value(wrappers[h])}&{prefix[h]}:provider=gdal" for h in hrefs)
        )
        calculated = QgsRasterLayer(uri, layer_name, "virtualraster")
        if calculated.isValid():
            _style_index(calculated)
        return calculated

    return LayerSpec(layer_name, f"index:{key}", "gdal", item_bbox(item), factory)
