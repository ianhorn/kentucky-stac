"""Streaming layers from a titiler-pgstac server: whole-collection statewide mosaics, and mosaics
registered on the fly and restricted to a chosen set of tiles.

The server already holds each Kentucky From Above collection as a ready-made mosaic, so a statewide
layer needs no search: QGIS just reads XYZ tiles from `/collections/{id}/tiles/WebMercatorQuad/{z}/{x}/{y}`.
A mosaic of specific tiles instead registers a search scoped to their ids (POST /searches/register,
idempotent -- the same ids/collection hash to the same search and reuse the existing one) and reads
XYZ tiles from `/searches/{search_id}/tiles/...`. A tile outside the registered set comes back 204 No
Content, which QGIS's XYZ provider treats as an empty tile rather than an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings, QgsTask

from .qgis_transport import qgis_transport
from .stac import Item

DEFAULT_TILER_URL = "https://vdo05uew72.execute-api.us-west-2.amazonaws.com"
SETTINGS_KEY = "kentucky_stac/tiler_url"
GROUP_NAME = "Ky STAC"
USER_AGENT = "KentuckyStac/0.1"

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


def default_style_for(collection_id: str) -> Optional[Style]:
    """The one style used for a mosaic of specific tiles (no per-tile style picker): color for
    imagery, hillshade for elevation."""
    styles = styles_for(collection_id)
    return styles[0] if styles else None


def tile_url(base: str, collection_id: str, style: Style, fmt: str = "png") -> str:
    return f"{base.rstrip('/')}/collections/{collection_id}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.{fmt}?{style.query}"


def search_tile_url(base: str, search_id: str, style: Style, fmt: str = "png") -> str:
    return f"{base.rstrip('/')}/searches/{search_id}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.{fmt}?{style.query}"


def xyz_uri(url: str, zmin: int = 0, zmax: int = 22) -> str:
    """A QGIS XYZ data source URI. QGIS reads the `url=` value as-is, except that `&` would end it
    and so must be written as %26; fully percent-encoding the URL would break it."""
    return f"type=xyz&url={url.replace('&', '%26')}&zmax={zmax}&zmin={zmin}"


def layer_name(collection_title: str, style: Style) -> str:
    return f"Ky STAC {collection_title} (streaming, {style.label.split(' (')[0].lower()})"


def mosaic_layer_name(collection_id: str, style: Style, tile_count: int) -> str:
    return f"Ky STAC {collection_id} mosaic ({tile_count} tile{'s' if tile_count != 1 else ''}, {style.label.split(' (')[0].lower()})"


def add_xyz_layer(url: str, name: str, project: Optional[QgsProject] = None) -> Tuple[Optional[QgsRasterLayer], str]:
    """Add an XYZ tile layer to the "Ky STAC" group. Returns (layer, message); layer is None when
    nothing was added (already on the map, or QGIS rejected the source)."""
    project = project or QgsProject.instance()
    uri = xyz_uri(url)
    if any(layer.source() == uri for layer in project.mapLayers().values()):
        return None, "That streaming layer is already on the map."
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        return None, f"QGIS could not open the tile source: {layer.error().summary()}"
    root = project.layerTreeRoot()
    group = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    return layer, f"Added {layer.name()}."


def add_streaming_layer(
    collection_id: str, collection_title: str, style: Style, project: Optional[QgsProject] = None
) -> Tuple[Optional[QgsRasterLayer], str]:
    """Add a whole-collection streaming XYZ layer to the "Ky STAC" group."""
    return add_xyz_layer(tile_url(tiler_url(), collection_id, style), layer_name(collection_title, style), project)


def add_search_layer(
    search_id: str, collection_id: str, style: Style, tile_count: int, project: Optional[QgsProject] = None
) -> Tuple[Optional[QgsRasterLayer], str]:
    """Add an XYZ layer for a registered search (a mosaic of specific tiles) to the "Ky STAC" group."""
    return add_xyz_layer(
        search_tile_url(tiler_url(), search_id, style), mosaic_layer_name(collection_id, style, tile_count), project
    )


@dataclass(frozen=True)
class MosaicGroup:
    collection_id: str
    ids: Tuple[str, ...]


def plan_server_mosaics(items: Iterable[Item]) -> List[MosaicGroup]:
    """Group items by collection (each collection needs its own registered search and its own
    render style), skipping any item with no collection id. Unlike the local VRT mosaic, a group of
    just one tile is kept -- registering a search restricted to one tile is still useful (a precise
    XYZ crop instead of the whole state), and costs nothing extra."""
    groups: Dict[str, List[str]] = {}
    for item in items:
        if item.collection:
            groups.setdefault(item.collection, []).append(item.id)
    return [MosaicGroup(cid, tuple(ids)) for cid, ids in sorted(groups.items())]


def register_search(base: str, collection_id: str, ids: Iterable[str]) -> dict:
    """Register (or reuse) a titiler-pgstac search restricted to specific tile ids. Returns the
    parsed JSON response, whose "id" is the search id used to build tile URLs. Raises StacError."""
    body = {"collections": [collection_id], "ids": list(ids), "filter-lang": "cql2-json"}
    data = qgis_transport(
        "POST",
        f"{base.rstrip('/')}/searches/register",
        json.dumps(body).encode("utf-8"),
        {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    return json.loads(data)


class RegisterMosaicsTask(QgsTask):
    """Register one search per collection group. `callback(built, failed)` runs on the main thread
    unless cancelled: `built` is a list of (MosaicGroup, search_id, Style), `failed` a list of
    (collection_id, error)."""

    def __init__(self, groups: List[MosaicGroup], callback: Callable[[list, list], None]):
        super().__init__(f"Registering {len(groups)} server mosaic{'s' if len(groups) != 1 else ''}")
        self._groups = groups
        self._callback = callback
        self.built: list = []
        self.failed: list = []

    def run(self) -> bool:
        base = tiler_url()
        for group in self._groups:
            if self.isCanceled():
                return False
            style = default_style_for(group.collection_id)
            if style is None:
                self.failed.append((group.collection_id, "this collection has no renderable style"))
                continue
            try:
                result = register_search(base, group.collection_id, group.ids)
                self.built.append((group, result["id"], style))
            except Exception as e:
                self.failed.append((group.collection_id, str(e)))
        return True

    def finished(self, result: bool) -> None:
        if not self.isCanceled():
            self._callback(self.built, self.failed)
