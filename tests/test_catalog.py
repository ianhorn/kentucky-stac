from kentucky_stac.catalog import is_lidar_collection, split_collections
from kentucky_stac.stac import Collection


def test_split_collections_by_kind_and_sorted():
    ids = ["orthos-phase1", "laz-phase3", "dem-phase2", "laz-phase1", "dem-phase1"]
    imagery, lidar = split_collections(Collection(id=i) for i in ids)
    assert [c.id for c in imagery] == ["dem-phase1", "dem-phase2", "orthos-phase1"]
    assert [c.id for c in lidar] == ["laz-phase1", "laz-phase3"]


def test_is_lidar_collection():
    assert is_lidar_collection(Collection(id="laz-phase2"))
    assert is_lidar_collection(Collection(id="LAZ-phase2"))
    assert not is_lidar_collection(Collection(id="dem-phase2"))


def test_primary_asset_prefers_copc_for_lidar():
    from kentucky_stac.catalog import primary_asset
    from kentucky_stac.stac import Item

    item = Item.from_dict(
        {"assets": {"laz": {"href": "a.laz"}, "copc": {"href": "a.copc.laz"}, "thumbnail": {"href": "t.png"}}}
    )
    assert primary_asset(item, lidar=True).href == "a.copc.laz"
    plain = Item.from_dict({"assets": {"laz": {"href": "a.laz"}}})
    assert primary_asset(plain, lidar=True).href == "a.laz"
    assert primary_asset(Item.from_dict({"assets": {"t": {"href": "t.png", "roles": ["thumbnail"]}}}), lidar=True) is None


def test_primary_asset_for_imagery_is_data_asset():
    from kentucky_stac.catalog import primary_asset
    from kentucky_stac.stac import Item

    item = Item.from_dict({"assets": {"thumbnail": {"href": "t.png"}, "data": {"href": "d.tif"}}})
    assert primary_asset(item, lidar=False).href == "d.tif"


def test_format_size():
    from kentucky_stac.catalog import format_size

    assert format_size(None) == ""
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"
    assert format_size(3 * 1024**3) == "3.0 GB"
