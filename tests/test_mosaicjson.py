from kentucky_stac.mosaicjson import MosaicJsonSpec, build_mosaicjson, native_gsd, plan_mosaicjson
from kentucky_stac.stac import Item


def test_quadkey_matches_the_standard_reference_example():
    from kentucky_stac.mosaicjson import _quadkey

    # The canonical example from Microsoft's Bing Maps Tile System docs: tile (3, 5) at level 3.
    assert _quadkey(3, 5, 3) == "213"


def test_zoom_for_extent_is_smaller_for_a_larger_bbox():
    from kentucky_stac.mosaicjson import _zoom_for_extent

    one_tile = _zoom_for_extent((-84.6, 37.98, -84.5, 38.06))
    all_of_kentucky = _zoom_for_extent((-89.5, 36.5, -81.9, 39.1))
    assert one_tile > all_of_kentucky


def test_zoom_for_resolution_is_higher_for_a_finer_gsd():
    from kentucky_stac.mosaicjson import _zoom_for_resolution

    ortho_3in = _zoom_for_resolution(0.075, 37.5)
    dem_2_5ft = _zoom_for_resolution(0.75, 37.5)
    assert ortho_3in > dem_2_5ft


def test_native_gsd():
    assert native_gsd([0, 0, 5000, 5000], [10000, 10000]) == 0.5
    assert native_gsd(None, [10, 10]) is None
    assert native_gsd([0, 0, 5000, 5000], None) is None
    assert native_gsd([0, 0, 5000, 5000], [0, 10000]) is None


def _item(id_: str, collection: str, bbox=None) -> Item:
    return Item.from_dict(
        {"id": id_, "collection": collection, "bbox": bbox, "assets": {"data": {"href": f"{id_}.tif"}}}
    )


def test_plan_mosaicjson_groups_by_collection_and_leaves_singletons_out():
    items = [_item("a", "dem-phase3"), _item("b", "dem-phase3"), _item("c", "orthos-phase3")]
    specs, left_out = plan_mosaicjson(items, "/tmp", "120000")
    assert len(specs) == 1
    assert specs[0].name == "dem-phase3 mosaic (2 tiles)"
    import os

    assert specs[0].json_path == os.path.join("/tmp", "dem-phase3_2tiles_120000.json")
    assert [i.id for i in left_out] == ["c"]


def test_plan_mosaicjson_leaves_out_items_with_no_data_asset():
    no_asset = Item.from_dict({"id": "x", "collection": "dem-phase3", "assets": {}})
    items = [_item("a", "dem-phase3"), _item("b", "dem-phase3"), no_asset]
    specs, left_out = plan_mosaicjson(items, "/tmp", "120000")
    assert len(specs) == 1
    assert [i.id for i in left_out] == ["x"]


def test_build_mosaicjson_structure():
    tiles = (
        _item("a", "dem-phase3", bbox=[-84.55, 38.00, -84.54, 38.01]),
        _item("b", "dem-phase3", bbox=[-84.54, 38.00, -84.53, 38.01]),
    )
    spec = MosaicJsonSpec("dem-phase3 mosaic (2 tiles)", "/tmp/x.json", tiles)
    doc = build_mosaicjson(spec, gsd_meters_for=lambda item: 0.5)

    assert doc["mosaicjson"] == "0.0.2"
    assert doc["name"] == "dem-phase3 mosaic (2 tiles)"
    assert "quadkey_zoom" not in doc  # spec defaults it to minzoom when omitted
    assert doc["minzoom"] <= doc["maxzoom"]
    assert doc["bounds"] == [-84.55, 38.0, -84.53, 38.01]
    assert len(doc["center"]) == 3 and doc["center"][2] == doc["minzoom"]

    all_hrefs = {href for hrefs in doc["tiles"].values() for href in hrefs}
    assert all_hrefs == {"a.tif", "b.tif"}


def test_build_mosaicjson_returns_none_without_any_usable_bbox():
    tiles = (_item("a", "dem-phase3", bbox=None),)
    spec = MosaicJsonSpec("dem-phase3 mosaic (1 tile)", "/tmp/x.json", tiles)
    assert build_mosaicjson(spec, gsd_meters_for=lambda item: 0.5) is None
