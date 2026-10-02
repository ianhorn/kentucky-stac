"""Streaming layers from a tile server, restricted to a chosen set of tiles.

Two kinds of server are supported:

* titiler-pgstac (KyFromAbove's own, or a user's that serves another API's catalog): one mosaic layer
  per collection, described next.
* plain titiler (a user's own, for a source with no pgstac server): one layer per tile, each reading
  that tile's own file through `/cog/tiles/...?url=<file>`. See plain_tile_url / add_cog_layers.

A mosaic of specific tiles registers a search scoped to their ids (POST /searches/register,
idempotent -- the same ids/collection hash to the same search and reuse the existing one) and reads
XYZ tiles from `/searches/{search_id}/tiles/...`. A tile outside the registered set comes back 204 No
Content, which QGIS's XYZ provider treats as an empty tile rather than an error.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings, QgsTask

from .qgis_transport import qgis_transport
from .sources import is_default_uri
from .stac import Item, StacError

DEFAULT_TILER_URL = "https://vdo05uew72.execute-api.us-west-2.amazonaws.com"
SETTINGS_KEY = "kentucky_stac/tiler_url"
GROUP_NAME = "Ky STAC"
# A plain titiler gets one layer per tile, so cap how many a single click adds.
MAX_TILE_LAYERS = 50
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


def generic_style(asset: str, bands: int = 0) -> Style:
    """For a collection on someone else's tile server, whose layout is unknown: render the tile's
    own data asset as-is, with no color ramp (that's a KyFromAbove specific). A 4+ band image (RGB
    plus near-infrared or an undefined band) can't be encoded as PNG, so then bands 1-3 are asked for."""
    quoted = urllib.parse.quote(asset, safe="")
    query = f"assets={quoted}"
    if bands >= 4:
        query += f"&asset_bidx={quoted}%7C1%2C2%2C3"
    return Style("color", "Color", query)


def plain_tile_url(base: str, href: str, fmt: str = "png", bands: int = 0) -> str:
    """XYZ template for a plain titiler reading one tile's file. The file URL is fully percent-encoded
    so its own "?" is not mistaken for part of this URL. "=" stays raw, since QGIS's XYZ source decodes
    "%3D" back to it anyway (harmless inside a query value). A "&" can't be carried at all -- QGIS decodes
    "%26" back to a raw "&", which would split the query -- so add_cog_layers skips such files."""
    encoded = urllib.parse.quote(href, safe="=")
    url = f"{base.rstrip('/')}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.{fmt}?url={encoded}"
    # 4+ bands (RGB plus near-infrared or an undefined band) can't be encoded as PNG: ask for 1-3.
    return url + "&bidx=1&bidx=2&bidx=3" if bands >= 4 else url


def search_tile_url(base: str, search_id: str, style: Style, fmt: str = "png") -> str:
    return f"{base.rstrip('/')}/searches/{search_id}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.{fmt}?{style.query}"


def xyz_uri(url: str, zmin: int = 0, zmax: int = 22) -> str:
    """A QGIS XYZ data source URI. QGIS reads the `url=` value as-is, except that `&` would end it
    and so must be written as %26; fully percent-encoding the URL would break it."""
    return f"type=xyz&url={url.replace('&', '%26')}&zmax={zmax}&zmin={zmin}"


def mosaic_layer_name(collection_id: str, style: Style, tile_count: int, source: str = "") -> str:
    where = f"{collection_id} · {source}" if source else collection_id
    return f"Ky STAC {where} mosaic ({tile_count} tile{'s' if tile_count != 1 else ''}, {style.label.split(' (')[0].lower()})"


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


def add_search_layer(
    search_id: str, group: "MosaicGroup", style: Style, project: Optional[QgsProject] = None
) -> Tuple[Optional[QgsRasterLayer], str]:
    """Add an XYZ layer for a registered search (a mosaic of specific tiles) to the "Ky STAC" group."""
    return add_xyz_layer(
        search_tile_url(group.tiler, search_id, style),
        mosaic_layer_name(group.collection_id, style, len(group.ids), group.source_name),
        project,
    )


@dataclass(frozen=True)
class MosaicGroup:
    collection_id: str
    ids: Tuple[str, ...]
    tiler: str = ""  # the titiler-pgstac base URL this group is registered with
    source_name: str = ""  # "" for the built-in catalog
    asset: str = ""  # the data asset to render, for a source other than the built-in one ("" = built-in)
    hrefs: Tuple[str, ...] = ()  # each tile's data file URL, parallel to ids (used by a plain titiler)
    band_counts: Tuple[int, ...] = ()  # each tile's band count (0 = unknown), parallel to ids


def plan_server_mosaics(
    items: Iterable[Item],
    tiler_for_source: Callable[[str], str],
    name_for_source: Callable[[str], str] = lambda _uri: "",
) -> List[MosaicGroup]:
    """Group items by (source, collection) -- each needs its own registered search, tile server and
    render style -- skipping any item with no collection id or no tile server (tiler_for_source
    returns "" for a source with none). Unlike the local VRT mosaic, a group of just one tile is
    kept -- registering a search restricted to one tile is still useful (a precise XYZ crop instead
    of the whole state), and costs nothing extra."""
    groups: Dict[Tuple[str, str], List[Item]] = {}
    for item in items:
        if item.collection:
            groups.setdefault((item.source if not is_default_uri(item.source) else "", item.collection), []).append(item)
    planned = []
    for (source, cid), members in sorted(groups.items()):
        tiler = tiler_for_source(source)
        if not tiler:
            continue
        if source:  # another API's tile server: render whichever asset its tiles' data lives in
            asset = next((k for k in (m.data_asset_key() for m in members) if k), "data")
            hrefs = tuple((m.data_asset().href if m.data_asset() else "") for m in members)
            counts = tuple((m.data_asset().band_count if m.data_asset() else 0) for m in members)
            planned.append(
                MosaicGroup(
                    cid, tuple(m.id for m in members), tiler.rstrip("/"), name_for_source(source), asset, hrefs, counts
                )
            )
        else:
            planned.append(MosaicGroup(cid, tuple(m.id for m in members), tiler.rstrip("/")))
    return planned


def detect_server_kind(base: str) -> str:
    """"pgstac" (has /searches/...), "cog" (a plain titiler, has /cog/...), or "" if it can't tell --
    read from the server's OpenAPI document, which FastAPI apps (both titilers) publish."""
    try:
        raw = qgis_transport(
            "GET", f"{base.rstrip('/')}/openapi.json", None, {"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        paths = list((json.loads(raw).get("paths") or {}).keys())
    except Exception:
        return ""
    if any("/searches/" in p for p in paths):
        return "pgstac"
    if any(p.startswith("/cog/") for p in paths):
        return "cog"
    return ""


def add_cog_layers(group: MosaicGroup, project: Optional[QgsProject] = None) -> Tuple[int, List[str]]:
    """Add one XYZ layer per tile for a plain titiler. Returns (layers added, notes)."""
    notes: List[str] = []
    counts = group.band_counts or (0,) * len(group.ids)
    pairs = [(i, h, b) for i, h, b in zip(group.ids, group.hrefs, counts) if h and "&" not in h]
    if len(pairs) < len(group.ids):
        notes.append(
            f"{len(group.ids) - len(pairs)} tile(s) skipped (no data file URL, or one containing \"&\", "
            "which can't be passed to a tile layer)"
        )
    if len(pairs) > MAX_TILE_LAYERS:
        notes.append(
            f"only the first {MAX_TILE_LAYERS} of {len(pairs)} tiles were added as layers "
            "(Export VRT or Export as MosaicJSON handle many tiles better)"
        )
        pairs = pairs[:MAX_TILE_LAYERS]
    added = 0
    for item_id, href, bands in pairs:
        where = f"{item_id} · {group.source_name}" if group.source_name else item_id
        layer, message = add_xyz_layer(
            plain_tile_url(group.tiler, href, bands=bands), f"Ky STAC {where} (titiler)", project
        )
        if layer is not None:
            added += 1
        elif "already" not in message:
            notes.append(message)
    return added, notes


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
    unless cancelled: `built` is a list of (MosaicGroup, search_id or None, Style, kind), `failed` a list of
    (collection_id, error)."""

    def __init__(self, groups: List[MosaicGroup], callback: Callable[[list, list], None]):
        super().__init__(f"Registering {len(groups)} server mosaic{'s' if len(groups) != 1 else ''}")
        self._groups = groups
        self._callback = callback
        self.built: list = []
        self.failed: list = []

    def run(self) -> bool:
        for group in self._groups:
            if self.isCanceled():
                return False
            bands = next((b for b in group.band_counts if b), 0)
            style = generic_style(group.asset, bands) if group.asset else default_style_for(group.collection_id)
            if style is None:
                self.failed.append((group.collection_id, "this collection has no renderable style"))
                continue
            # The built-in server is always titiler-pgstac; a user's could be either kind.
            kind = detect_server_kind(group.tiler) if group.asset else "pgstac"
            try:
                if kind == "cog":
                    self.built.append((group, None, style, "cog"))
                    continue
                result = register_search(group.tiler, group.collection_id, group.ids)
                self.built.append((group, result["id"], style, "pgstac"))
            except StacError as e:
                if group.asset and kind == "" and e.status in (404, 405):
                    # No /searches/register: not a pgstac server, so treat it as a plain titiler.
                    self.built.append((group, None, style, "cog"))
                else:
                    self.failed.append((group.collection_id, str(e)))
            except Exception as e:
                self.failed.append((group.collection_id, str(e)))
        return True

    def finished(self, result: bool) -> None:
        if not self.isCanceled():
            self._callback(self.built, self.failed)
