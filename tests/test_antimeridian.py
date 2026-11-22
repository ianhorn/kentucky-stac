"""An area of interest or tile over the antimeridian (180 degrees): cut there, never read the long way round.

A drawn area near the edge of the world can run past 180 degrees east. Sent as it was, a STAC API returned tiles on
the other side of the world for it (an area over eastern New Zealand found South America and Australia).
"""

import json
import os

import pytest

pytest.importorskip("qgis.core", reason="needs QGIS (qgis.core)")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsProject  # noqa: E402

from kentucky_stac import results_layer, search_tab  # noqa: E402
from kentucky_stac.antimeridian import crosses_antimeridian, wrap_antimeridian  # noqa: E402
from kentucky_stac.aoi import AoiState  # noqa: E402
from kentucky_stac.layers import item_bbox  # noqa: E402
from kentucky_stac.stac import Collection, Item  # noqa: E402


def _bounds(geometry):
    box = geometry.boundingBox()
    return round(box.xMinimum(), 6), round(box.yMinimum(), 6), round(box.xMaximum(), 6), round(box.yMaximum(), 6)


def _parts(geometry):
    return geometry.asGeometryCollection() if geometry.isMultipart() else [geometry]


# ---- boxes ------------------------------------------------------------------------------------


def test_a_box_with_west_east_of_east_crosses_the_antimeridian():
    assert crosses_antimeridian([178.0, -20.0, -178.0, -10.0])
    assert not crosses_antimeridian([170.0, -20.0, 175.0, -10.0])
    assert not crosses_antimeridian([-180.0, -90.0, 180.0, 90.0])
    assert not crosses_antimeridian(None) and not crosses_antimeridian([1, 2])


def test_a_tile_over_the_antimeridian_has_no_plain_box_to_check_placement_against():
    over = Item(id="o", bbox=[179.0, -18.0, -179.0, -17.0])
    assert item_bbox(over) is None
    assert item_bbox(Item(id="p", bbox=[170.0, -18.0, 171.0, -17.0])) == (170.0, -18.0, 171.0, -17.0)


# ---- the area of interest ----------------------------------------------------------------------


def test_a_shape_inside_the_world_is_left_alone(qgis_app):
    g = QgsGeometry.fromWkt("POLYGON((170 -40, 179 -40, 179 -35, 170 -35, 170 -40))")
    assert wrap_antimeridian(g).asWkt() == g.asWkt()


def test_an_area_running_past_180_is_cut_into_a_part_on_each_side(qgis_app):
    """The reported case: a box over eastern New Zealand that extends beyond the antimeridian."""
    g = QgsGeometry.fromWkt("POLYGON((176 -40, 184 -40, 184 -35, 176 -35, 176 -40))")
    wrapped = wrap_antimeridian(g)
    parts = sorted(_parts(wrapped), key=lambda p: p.boundingBox().xMinimum())
    assert [_bounds(p) for p in parts] == [(-180.0, -40.0, -176.0, -35.0), (176.0, -40.0, 180.0, -35.0)]
    assert round(wrapped.area(), 6) == round(g.area(), 6) == 40.0  # nothing lost or doubled
    assert _bounds(wrapped)[0] >= -180.0 and _bounds(wrapped)[2] <= 180.0


def test_an_area_running_past_minus_180_is_cut_too(qgis_app):
    g = QgsGeometry.fromWkt("POLYGON((-184 -40, -176 -40, -176 -35, -184 -35, -184 -40))")
    parts = sorted(_parts(wrap_antimeridian(g)), key=lambda p: p.boundingBox().xMinimum())
    assert [_bounds(p) for p in parts] == [(-180.0, -40.0, -176.0, -35.0), (176.0, -40.0, 180.0, -35.0)]


def test_an_area_wholly_past_180_is_moved_back_into_the_world(qgis_app):
    g = QgsGeometry.fromWkt("POLYGON((185 -40, 190 -40, 190 -35, 185 -35, 185 -40))")
    assert _bounds(wrap_antimeridian(g)) == (-175.0, -40.0, -170.0, -35.0)


def test_a_point_and_a_line_past_180_are_wrapped_too(qgis_app):
    assert wrap_antimeridian(QgsGeometry.fromWkt("POINT(181.5 -37)")).asWkt() == "Point (-178.5 -37)"
    line = wrap_antimeridian(QgsGeometry.fromWkt("LINESTRING(178 -37, 182 -37)"))
    assert sorted(_bounds(p)[0::2] for p in _parts(line)) == [(-180.0, -178.0), (178.0, 180.0)]


def test_the_area_drawn_in_web_mercator_past_the_edge_is_sent_as_two_parts(qgis_app):
    """A polygon on a Web Mercator map panned past the date line: the search body holds both parts, inside -180..180."""
    # Map coordinates past the edge of the world, as a canvas allows (a lon/lat to mercator transform would wrap 184 to
    # -176 and hide the case): x runs to 184/180 of the half-world width.
    half_world = 20037508.342789244
    x0, x1 = 176 / 180 * half_world, 184 / 180 * half_world
    south, north = -4865942.28, -4163881.14  # about -40 and -35 degrees
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    drawn = QgsGeometry.fromWkt(f"POLYGON(({x0} {south}, {x1} {south}, {x1} {north}, {x0} {north}, {x0} {south}))")
    state = AoiState()
    state.set(drawn, mercator, "drawn")
    body = state.geojson_geometry()
    assert body["type"] == "MultiPolygon" and len(body["coordinates"]) == 2
    xs = [x for polygon in body["coordinates"] for ring in polygon for x, _y in ring]
    assert -180.0 <= min(xs) and max(xs) <= 180.0
    # Each part is one side of the cut, a few degrees wide: nothing spans the globe the long way round.
    assert max(xs) - min(xs) > 350.0  # the two parts sit at the opposite edges of the world...
    for polygon in body["coordinates"]:
        part = [x for ring in polygon for x, _y in ring]
        assert max(part) - min(part) <= 4.0 + 1e-6  # ...and each is only 4 degrees wide
    json.dumps(body)  # serialisable, as the search request needs


def test_the_world_wrapped_ring_is_what_an_uncut_mercator_area_becomes(qgis_app):
    """The bug itself: without the cut, projecting x past the edge wraps it and the ring spans the globe."""
    from qgis.core import QgsCoordinateTransform

    half_world = 20037508.342789244
    x0, x1 = 176 / 180 * half_world, 184 / 180 * half_world
    south, north = -4865942.28, -4163881.14
    drawn = QgsGeometry.fromWkt(f"POLYGON(({x0} {south}, {x1} {south}, {x1} {north}, {x0} {north}, {x0} {south}))")
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    uncut = QgsGeometry(drawn)
    uncut.transform(QgsCoordinateTransform(mercator, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()))
    assert uncut.boundingBox().width() > 300.0  # wide as the world: what the API was sent before


def test_an_area_inside_the_world_is_sent_unchanged(qgis_app):
    state = AoiState()
    state.set(QgsGeometry.fromWkt("POLYGON((-85 37, -84 37, -84 38, -85 38, -85 37))"), QgsCoordinateReferenceSystem("EPSG:4326"), "ky")
    body = state.geojson_geometry()
    assert body["type"] == "Polygon"


def test_an_area_inside_the_world_on_other_maps_is_sent_as_one_polygon(qgis_app):
    """Web Mercator and a UTM zone, neither near the edge: the cut must leave them alone."""
    from qgis.core import QgsCoordinateTransform

    lonlat = QgsGeometry.fromWkt("POLYGON((-85 37, -84 37, -84 38, -85 38, -85 37))")
    for code in ("EPSG:3857", "EPSG:32616", "EPSG:3089", "EPSG:3395"):
        crs = QgsCoordinateReferenceSystem(code)
        drawn = QgsGeometry(lonlat)
        drawn.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance()))
        state = AoiState()
        state.set(drawn, crs, code)
        body = state.geojson_geometry()
        assert body["type"] == "Polygon", code
        xs = [x for ring in body["coordinates"] for x, _y in ring]
        assert -85.0001 <= min(xs) and max(xs) <= -83.9999, (code, min(xs), max(xs))


# ---- result footprints --------------------------------------------------------------------------


def test_a_tile_with_only_a_box_over_the_antimeridian_is_drawn_at_both_edges(qgis_app):
    shape = results_layer.geojson_to_geometry(None, [178.0, -18.0, -178.0, -17.0])
    parts = sorted(_parts(shape), key=lambda p: p.boundingBox().xMinimum())
    assert [_bounds(p) for p in parts] == [(-180.0, -18.0, -178.0, -17.0), (178.0, -18.0, 180.0, -17.0)]


def test_a_footprint_not_cut_at_the_antimeridian_is_not_drawn_across_the_world(qgis_app):
    """RFC 7946 asks for the cut, but a ring running 178 to -178 is common: its short way is across 180."""
    ring = {"type": "Polygon", "coordinates": [[[178, -18], [-178, -18], [-178, -17], [178, -17], [178, -18]]]}
    shape = results_layer.geojson_to_geometry(ring, [178.0, -18.0, -178.0, -17.0])
    assert round(shape.area(), 6) == 4.0  # 4 degrees wide, not 356
    assert _bounds(shape)[0] >= -180.0 and _bounds(shape)[2] <= 180.0
    assert len(_parts(shape)) == 2


def test_a_footprint_already_cut_at_the_antimeridian_is_kept_as_it_is(qgis_app):
    cut = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[178, -18], [180, -18], [180, -17], [178, -17], [178, -18]]],
            [[[-180, -18], [-178, -18], [-178, -17], [-180, -17], [-180, -18]]],
        ],
    }
    shape = results_layer.geojson_to_geometry(cut, [178.0, -18.0, -178.0, -17.0])
    assert round(shape.area(), 6) == 4.0 and len(_parts(shape)) == 2


def test_an_ordinary_footprint_is_unchanged(qgis_app):
    poly = {"type": "Polygon", "coordinates": [[[-85, 37], [-84, 37], [-84, 38], [-85, 38], [-85, 37]]]}
    assert _bounds(results_layer.geojson_to_geometry(poly, [-85.0, 37.0, -84.0, 38.0])) == (-85.0, 37.0, -84.0, 38.0)


# ---- zoom to a collection ----------------------------------------------------------------------


def test_zoom_extent_of_a_collection_over_the_antimeridian_runs_the_short_way(qgis_app):
    tab = search_tab.SearchTab("imagery", "test-am", False, AoiState())
    try:
        tab.set_collections([Collection(id="fiji", bbox=[177.0, -20.0, -178.0, -12.0])])
        tab.select_all_collections()
        assert tab._collection_extent() == (177.0, -20.0, 182.0, -12.0)
        tab.select_all_collections()
    finally:
        tab.shutdown()
        QgsProject.instance().clear()
