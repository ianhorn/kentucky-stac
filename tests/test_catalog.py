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


def test_thumbnail_href_prefers_the_items_own_asset():
    from kentucky_stac.catalog import thumbnail_href
    from kentucky_stac.stac import Item

    item = Item.from_dict({"collection": "dem-phase3", "id": "x", "assets": {"thumbnail": {"href": "t.png"}}})
    assert thumbnail_href(item, lidar=False) == "t.png"
    assert thumbnail_href(item, lidar=True) == "t.png"


def test_thumbnail_href_reconstructs_for_lidar_only():
    from kentucky_stac.catalog import LIDAR_THUMBNAIL_BASE, thumbnail_href
    from kentucky_stac.stac import Item

    # A LiDAR item's /search response never carries a thumbnail asset (see thumbnail_href's
    # docstring) -- falls back to the known URL convention.
    item = Item.from_dict({"collection": "laz-phase3", "id": "N046E341_LAS_Phase3.copc", "assets": {"pointcloud": {"href": "d.laz"}}})
    assert thumbnail_href(item, lidar=True) == f"{LIDAR_THUMBNAIL_BASE}/collections/laz-phase3/thumbnails/N046E341_LAS_Phase3.copc.png"
    # Same missing-thumbnail item, but NOT lidar -- no guessing, since imagery's own URL scheme
    # varies by collection and isn't reliably reconstructable.
    assert thumbnail_href(item, lidar=False) is None


def test_format_size():
    from kentucky_stac.catalog import format_size

    assert format_size(None) == ""
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"
    assert format_size(3 * 1024**3) == "3.0 GB"
