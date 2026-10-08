import json

from kentucky_stac.catalog import can_add_to_map
from kentucky_stac.item_json import item_json_text, json_to_html, parse_action_url
from kentucky_stac.stac import Asset, Item

RAW = {
    "id": "X",
    "assets": {
        "nir08": {"href": "s3://b/k_B5.TIF", "type": "image/vnd.stac.geotiff; cloud-optimized=true"},
        "MTL.xml": {"href": "s3://b/k_MTL.xml", "type": "application/xml"},
        "thumbnail": {"href": "https://x.com/t.jpeg", "type": "image/jpeg"},
        "data": {"href": "https://x.com/a/b.copc.laz"},
    },
    "links": [{"rel": "self", "href": "https://x.com/self"}],
}


def _links(page):
    import re

    return [parse_action_url(u) for u in re.findall(r'href="(kyasset:[^"]*)"', page)]


def test_every_asset_gets_download_and_copy_and_only_mappable_ones_add_to_map():
    item = Item.from_dict(RAW)
    links = _links(json_to_html(item_json_text(item), item))
    by_key = {}
    for action, key in links:
        by_key.setdefault(key, []).append(action)
    assert by_key["nir08"] == ["download", "copy", "map"]
    assert by_key["data"] == ["download", "copy", "map"]
    assert by_key["MTL.xml"] == ["download", "copy"]
    assert by_key["thumbnail"] == ["download", "copy"]


def test_no_action_links_without_an_item_and_ordinary_links_survive():
    item = Item.from_dict(RAW)
    assert "kyasset" not in json_to_html(item_json_text(item))
    assert 'href="https://x.com/self"' in json_to_html(item_json_text(item), item)


def test_action_urls_round_trip_odd_keys():
    from kentucky_stac.item_json import action_url

    assert parse_action_url(action_url("copy", "ANG.txt")) == ("copy", "ANG.txt")
    assert parse_action_url(action_url("map", "a/b c")) == ("map", "a/b c")
    assert parse_action_url("https://example.com/") is None


def test_can_add_to_map():
    assert can_add_to_map(Asset(href="https://x/a.tif"))
    assert can_add_to_map(Asset(href="https://x/a.copc.laz"))
    assert can_add_to_map(Asset(href="https://x/download?id=1", type="image/tiff; application=geotiff"))
    assert not can_add_to_map(Asset(href="https://x/a.laz"))
    assert not can_add_to_map(Asset(href="https://x/t.jpeg", type="image/jpeg"))
    assert not can_add_to_map(Asset(href=""))
