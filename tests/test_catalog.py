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
