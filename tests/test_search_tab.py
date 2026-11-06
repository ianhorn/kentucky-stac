"""The search tab: its label helpers and the SearchTab widget's collection, AOI and results handling.

search_tab imports QGIS and Qt, so this runs only where QGIS is installed (the QGIS CI jobs); elsewhere it is
skipped. The widgets are created off-screen and no network request is made (the test items have no thumbnails).
"""

import os

import pytest

pytest.importorskip("qgis.core", reason="needs QGIS (qgis.core)")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsProject,
)

from kentucky_stac import auth, search_tab  # noqa: E402
from kentucky_stac.aoi import AoiState  # noqa: E402
from kentucky_stac.sources import DEFAULT_SOURCE, ApiSource  # noqa: E402
from kentucky_stac.stac import Asset, Collection, Item  # noqa: E402

OTHER = ApiSource("Other API", "https://other.example/stac")
POLYGON = "POLYGON((-85 37, -84 37, -84 38, -85 38, -85 37))"


@pytest.fixture(scope="session")
def qgis_app():
    app = QgsApplication([], False)
    app.initQgis()
    yield app
    app.exitQgis()


class FakeBar:
    """Stands in for QgsMessageBar and records what the tab told the user."""

    def __init__(self):
        self.messages = []

    def pushMessage(self, title, text, level=None, duration=0):  # noqa: N802 (Qt-style name)
        self.messages.append((text, level))

    def texts(self):
        return [text for text, _level in self.messages]


@pytest.fixture
def aoi(qgis_app):
    return AoiState()


@pytest.fixture
def bar():
    return FakeBar()


@pytest.fixture
def tab(qgis_app, aoi, bar):
    widget = search_tab.SearchTab("imagery", "test-imagery", False, aoi, message_bar=bar)
    widget.set_sources([DEFAULT_SOURCE, OTHER])
    yield widget
    widget.shutdown()
    QgsProject.instance().clear()


def _collection(cid, title=None, source="", bbox=None, **extra):
    return Collection(id=cid, title=title, source=source, bbox=bbox, **extra)


def _item(iid, collection="phase1", date="2022-05-01T00:00:00Z"):
    return Item(
        id=iid,
        collection=collection,
        bbox=[-85.0, 37.0, -84.9, 37.1],
        geometry={
            "type": "Polygon",
            "coordinates": [[[-85.0, 37.0], [-84.9, 37.0], [-84.9, 37.1], [-85.0, 37.1], [-85.0, 37.0]]],
        },
        properties={"datetime": date},
    )


def _set_aoi(aoi, wkt=POLYGON):
    aoi.set(QgsGeometry.fromWkt(wkt), QgsCoordinateReferenceSystem("EPSG:4326"), "test area")


# ---- label helpers --------------------------------------------------------------------------


def test_point_count_label():
    assert search_tab._point_count_label(Item(properties={"pc:count": 1234567})) == "1,234,567 points"
    assert search_tab._point_count_label(Item(properties={"pc:count": 0})) is None
    assert search_tab._point_count_label(Item()) is None


def test_projection_label_uses_a_plain_epsg_code():
    assert search_tab._projection_label(Item(properties={"proj:epsg": 3089})) == "EPSG:3089"


def test_projection_label_is_none_without_projection_info():
    assert search_tab._projection_label(Item()) is None


def test_projection_label_for_a_compound_crs_gives_horizontal_and_vertical(qgis_app):
    osr = pytest.importorskip("osgeo.osr")
    srs = osr.SpatialReference()
    srs.SetFromUserInput("EPSG:6473+6360")  # NAD83(2011) / Kentucky Single Zone + NAVD88 height (ftUS)
    wkt = srs.ExportToWkt(["FORMAT=WKT2"])
    assert search_tab._projection_label(Item(properties={"proj:wkt2": wkt})) == "EPSG:6473/EPSG:6360"


def test_gsd_in_meters_converts_from_the_crs_units(qgis_app):
    metres = Item(properties={"proj:epsg": 32616, "proj:bbox": [0, 0, 1000, 1000], "proj:shape": [100, 100]})
    assert search_tab._gsd_meters_for(metres) == pytest.approx(10.0)
    us_feet = Item(properties={"proj:epsg": 3089, "proj:bbox": [0, 0, 1000, 1000], "proj:shape": [100, 100]})
    assert search_tab._gsd_meters_for(us_feet) == pytest.approx(3.048, rel=1e-3)  # 10 ft


def test_gsd_in_meters_is_none_when_it_cannot_be_worked_out(qgis_app):
    assert search_tab._gsd_meters_for(Item(properties={"proj:epsg": 32616})) is None  # no bbox/shape
    no_epsg = Item(properties={"proj:bbox": [0, 0, 10, 10], "proj:shape": [1, 1]})
    assert search_tab._gsd_meters_for(no_epsg) is None


def test_collection_tooltip_text():
    plain = _collection("c")
    assert search_tab._describe(plain) == "? to present"
    dated = _collection("c", description="Aerial imagery", interval=["2020-01-01T00:00:00Z", "2021-12-31T00:00:00Z"])
    assert search_tab._describe(dated) == "Aerial imagery\n\n2020-01-01 to 2021-12-31"
    assert search_tab._describe(dated, "Other API").endswith("\n\nSource: Other API")


# ---- collections ----------------------------------------------------------------------------


def test_starts_waiting_for_collections(tab):
    assert not tab.search_button.isEnabled()
    assert tab.search_button.toolTip() == "Waiting for collections."
    assert not tab.list.isEnabled()


def test_loading_and_error_states_disable_searching(tab):
    tab.set_collections([_collection("a", "A")])
    tab.set_loading()
    assert tab.status.text() == "Loading collections..." and not tab.reload_button.isEnabled()
    tab.set_error("no route to host")
    assert tab.status.text() == "Could not load collections: no route to host"
    assert tab.reload_button.isEnabled() and not tab.list.isEnabled() and tab.list.count() == 0


def test_no_collections_says_so(tab):
    tab.set_collections([])
    assert tab.status.text() == "No imagery collections found."
    assert not tab.search_button.isEnabled()


def test_collections_start_unchecked_and_are_counted(tab):
    tab.set_collections([_collection("a", "Phase 1"), _collection("b", "Phase 2")])
    assert tab.status.text() == "2 collections"
    assert tab.list.count() == 2 and tab.selected_collection_ids() == []
    assert tab.search_button.toolTip() == "Check at least one collection above."
    tab.set_collections([_collection("a", "Phase 1")])
    assert tab.status.text() == "1 collection"


def test_another_sources_collections_are_labelled_with_the_source(tab):
    tab.set_collections(
        [_collection("a", "Phase 1"), _collection("s2", "Sentinel-2", source=OTHER.base_uri)]
    )
    assert [tab.list.item(i).text() for i in range(tab.list.count())] == ["Phase 1", "Sentinel-2 · Other API"]


def test_select_all_and_clear_selection(tab):
    tab.set_collections([_collection("a", "A"), _collection("b", "B"), _collection("c", "C")])
    tab.select_all_collections()
    assert tab.selected_collection_ids() == ["a", "b", "c"]
    tab.clear_collection_selection()
    assert tab.selected_collection_ids() == []


def test_the_selection_keeps_ids_apart_across_sources(tab):
    """Two sources can both have a collection called "data": the row index, not the id, picks the right one."""
    tab.set_collections(
        [_collection("data", "Data", source=""), _collection("data", "Data", source=OTHER.base_uri)]
    )
    tab.list.item(1).setCheckState(search_tab.Qt.CheckState.Checked)
    chosen = tab.selected_collections()
    assert len(chosen) == 1 and chosen[0].source == OTHER.base_uri


def test_collection_extent_covers_every_ticked_collection_that_states_one(tab):
    tab.set_collections(
        [
            _collection("a", bbox=[-86.0, 36.0, -85.0, 37.0]),
            _collection("b", bbox=[-84.0, 37.5, -83.0, 39.0]),
            _collection("c"),  # states no extent
        ]
    )
    assert tab._collection_extent() is None  # nothing ticked
    tab.select_all_collections()
    assert tab._collection_extent() == (-86.0, 36.0, -83.0, 39.0)
    tab.clear_collection_selection()
    tab.list.item(2).setCheckState(search_tab.Qt.CheckState.Checked)
    assert tab._collection_extent() is None
    assert "don't state the area" in tab.zoom_button.toolTip()


# ---- search readiness -----------------------------------------------------------------------


def test_search_needs_collections_ticked_and_an_aoi(tab, aoi):
    tab.set_collections([_collection("a", "A")])
    tab.select_all_collections()
    assert not tab.search_button.isEnabled()
    assert tab.search_button.toolTip() == "Set an area of interest above to search."
    _set_aoi(aoi)
    assert tab.search_button.isEnabled() and tab.search_button.toolTip() == ""
    tab.clear_collection_selection()
    assert not tab.search_button.isEnabled()
    assert tab.search_button.toolTip() == "Check at least one collection above."


def test_clipping_is_only_offered_for_a_polygon_aoi(tab, aoi):
    tab.set_collections([_collection("a", "A")])
    _set_aoi(aoi, "POINT(-85 37)")
    assert not tab.clip_to_aoi.isEnabled()
    _set_aoi(aoi)
    assert tab.clip_to_aoi.isEnabled()
    assert tab._aoi_is_polygon()


# ---- results --------------------------------------------------------------------------------


def test_results_are_listed_in_collection_then_id_order(tab):
    tab._on_results([_item("t2", "phase2"), _item("t1", "phase2"), _item("t9", "phase1")], 3, False, None)
    assert [i.id for i in tab._items] == ["t9", "t1", "t2"]
    assert tab.tree.topLevelItemCount() == 3
    assert tab.results_status.text() == "3 tiles found"
    assert tab.select_all_results_button.isEnabled()


def test_a_sorted_search_orders_by_date_and_puts_undated_tiles_last(tab):
    tab._sort = "desc"
    undated = _item("u", date="")
    undated.properties.clear()
    tab._on_results([_item("old", date="2020-01-01T00:00:00Z"), undated, _item("new", date="2023-01-01T00:00:00Z")], 3, False, None)
    assert [i.id for i in tab._items] == ["new", "old", "u"]


def test_a_truncated_result_says_how_many_were_shown(tab):
    tab._on_results([_item("a"), _item("b")], 50, True, None)
    assert tab.results_status.text().startswith("2 tiles found (showing the first 2 of 50")


def test_no_matching_tiles_says_so(tab):
    tab._on_results([], 0, False, None)
    assert tab.results_status.text() == "No tiles intersect the area of interest."
    assert tab.tree.topLevelItemCount() == 0


def test_a_failed_search_is_reported(tab, bar):
    tab._on_results([], None, False, "boom")
    assert tab.results_status.text() == "Search failed: boom"
    assert any("Search failed: boom" in text for text in bar.texts())
    assert any(level == Qgis.MessageLevel.Warning for _text, level in bar.messages)


def test_selecting_tiles_enables_the_action_buttons(tab):
    tab._on_results([_item("a"), _item("b")], 2, False, None)
    assert not tab.add_button.isEnabled() and not tab.download_button.isEnabled()
    tab.tree.topLevelItem(1).setSelected(True)
    assert tab.add_button.isEnabled() and tab.download_button.isEnabled()
    assert [i.id for i in tab.selected_items()] == ["b"]
    tab.tree.clearSelection()
    assert not tab.add_button.isEnabled() and tab.selected_items() == []


def test_server_mosaic_needs_a_tile_server_for_another_source(tab):
    other_item = _item("x")
    other_item.source = OTHER.base_uri
    tab._on_results([other_item], 1, False, None)
    tab.tree.selectAll()
    assert not tab.server_mosaic_button.isEnabled()
    assert "Other API" in tab.server_mosaic_button.toolTip()
    tab.set_sources([DEFAULT_SOURCE, ApiSource(OTHER.name, OTHER.base_uri, tiler_url="https://titiler.example")])
    assert tab.server_mosaic_button.isEnabled()


@pytest.fixture
def lidar_tab(qgis_app, aoi, bar):
    widget = search_tab.SearchTab("point cloud", "test-lidar", True, aoi, message_bar=bar)
    yield widget
    widget.shutdown()
    QgsProject.instance().clear()


def _copc_item(iid, source):
    item = _item(iid, "laz-phase2")
    item.source = source
    item.assets = {"data": Asset(href=f"https://files.example/{iid}.copc.laz", type="application/vnd.laszip+copc")}
    return item


def test_a_point_cloud_that_needs_a_login_is_not_streamed(lidar_tab, bar):
    """QGIS's COPC reader ignores a login however it is passed, so streaming would only fail with an obscure error."""
    sources = [DEFAULT_SOURCE, ApiSource(OTHER.name, OTHER.base_uri, auth_cfg="abc1234")]
    lidar_tab.set_sources(sources)
    auth.set_sources(sources)  # the dock registers each source's login here, as it does at run time
    try:
        lidar_tab._on_results([_copc_item("a", OTHER.base_uri)], 1, False, None)
        lidar_tab.tree.selectAll()
        lidar_tab.add_to_map()
        assert lidar_tab._add_task is None
        assert any("needs a login" in text and "download" in text for text in bar.texts()), bar.texts()
    finally:
        auth.set_sources([DEFAULT_SOURCE])


def test_a_point_cloud_without_a_login_is_still_streamed(lidar_tab, bar):
    lidar_tab.set_sources([DEFAULT_SOURCE, OTHER])
    lidar_tab._on_results([_copc_item("a", OTHER.base_uri)], 1, False, None)
    lidar_tab.tree.selectAll()
    lidar_tab.add_to_map()
    assert lidar_tab._add_task is not None
    lidar_tab._add_task.cancel()
    lidar_tab._add_task = None


def test_the_point_filter_blocks_an_add_with_no_class_ticked(lidar_tab, bar):
    lidar_tab._on_results([_copc_item("a", OTHER.base_uri)], 1, False, None)
    lidar_tab.tree.selectAll()
    lidar_tab.point_filter.set_classes(set())
    lidar_tab.add_to_map()
    assert lidar_tab._add_task is None
    assert any("keeps nothing" in text for text in bar.texts())
