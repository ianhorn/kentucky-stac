"""Statewide streaming layers from a titiler-pgstac server.

The server already holds each Kentucky From Above collection as a ready-made mosaic, so adding one
needs no search: QGIS just reads XYZ tiles from `/collections/{id}/tiles/WebMercatorQuad/{z}/{x}/{y}`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings

DEFAULT_TILER_URL = "https://vdo05uew72.execute-api.us-west-2.amazonaws.com"
SETTINGS_KEY = "kentucky_stac/tiler_url"
GROUP_NAME = "Ky STAC"

# Elevation colors stretch across roughly the range of Kentucky terrain, in feet.
ELEVATION_RANGE = (400, 3200)


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    query: str  # the part of the tile URL after "?"


def tiler_url() -> str:
    """The titiler-pgstac base URL: the user's setting if present, else the default."""
    return str(QgsSettings().value(SETTINGS_KEY, DEFAULT_TILER_URL) or DEFAULT_TILER_URL).rstrip("/")


def styles_for(collection_id: str) -> List[Style]:
    """The ways a collection can be rendered. Every collection stores its data in the asset `data`
    on the server. Orthos carry a fourth, undefined band that breaks PNG output, so only bands 1-3
    are requested. A DEM is a single float band, so it needs a hillshade or a color ramp to be visible."""
    cid = collection_id.lower()
    if cid.startswith("orthos"):
        return [Style("color", "Color", "assets=data&asset_bidx=data%7C1%2C2%2C3")]
    if cid.startswith("dem"):
        low, high = ELEVATION_RANGE
        return [
            Style("hillshade", "Hillshade", "assets=data&algorithm=hillshade"),
            Style("elevation", f"Elevation colors ({low}-{high} ft)", f"assets=data&rescale={low},{high}&colormap_name=terrain"),
        ]
    return []


def tile_url(base: str, collection_id: str, style: Style, fmt: str = "png") -> str:
    return f"{base.rstrip('/')}/collections/{collection_id}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.{fmt}?{style.query}"


def xyz_uri(url: str, zmin: int = 0, zmax: int = 22) -> str:
    """A QGIS XYZ data source URI. QGIS reads the `url=` value as-is, except that `&` would end it
    and so must be written as %26; fully percent-encoding the URL would break it."""
    return f"type=xyz&url={url.replace('&', '%26')}&zmax={zmax}&zmin={zmin}"


def layer_name(collection_title: str, style: Style) -> str:
    return f"Ky STAC {collection_title} (streaming, {style.label.split(' (')[0].lower()})"


def add_streaming_layer(
    collection_id: str, collection_title: str, style: Style, project: Optional[QgsProject] = None
) -> Tuple[Optional[QgsRasterLayer], str]:
    """Add a streaming XYZ layer to the "Ky STAC" group. Returns (layer, message); layer is None
    when nothing was added (already on the map, or QGIS rejected the source)."""
    project = project or QgsProject.instance()
    uri = xyz_uri(tile_url(tiler_url(), collection_id, style))
    if any(layer.source() == uri for layer in project.mapLayers().values()):
        return None, "That streaming layer is already on the map."
    layer = QgsRasterLayer(uri, layer_name(collection_title, style), "wms")
    if not layer.isValid():
        return None, f"QGIS could not open the tile source: {layer.error().summary()}"
    root = project.layerTreeRoot()
    group = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    return layer, f"Added {layer.name()}."
