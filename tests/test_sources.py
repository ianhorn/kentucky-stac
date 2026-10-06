from kentucky_stac.catalog import is_lidar_collection, split_collections, thumbnail_href
from kentucky_stac.sources import (
    BUILTIN_ENTRY,
    DEFAULT_SOURCE,
    ApiSource,
    add_source,
    deserialize_sources,
    is_default_uri,
    is_usable_base_url,
    is_valid_api_url,
    parse_stac_index,
    serialize_sources,
    source_name,
    tiler_for,
    with_tiler_url,
)
from kentucky_stac.stac import DEFAULT_BASE_URI, Collection, Item

OTHER = "https://earth-search.aws.element84.com/v1"


def _entry(title, url, **kw):
    base = {"title": title, "url": url, "isApi": True, "isPrivate": False, "access": "public", "summary": ""}
    base.update(kw)
    return base


def test_parse_stac_index_filters_like_the_addin():
    data = [
        _entry("Zeta API", "https://zeta.example/stac/"),
        _entry("Alpha API", "https://alpha.example/v1", summary="x" * 300),
        _entry("Static catalog", "https://s3.example/catalog.json"),
        _entry("Keyed", "https://keyed.example/stac?apikey=1"),
        _entry("openEO thing", "https://openeo.example/"),
        _entry("Private", "https://private.example/", isPrivate=True),
        _entry("Protected", "https://protected.example/", access="protected"),
        _entry("Not an API", "https://static.example/", isApi=False),
        _entry("Kentucky duplicate", DEFAULT_BASE_URI + "/"),
        _entry("", "https://untitled.example/"),
        "garbage",
    ]
    entries = parse_stac_index(data)
    assert entries[0] == BUILTIN_ENTRY
    assert [e.title for e in entries[1:]] == ["Alpha API", "Zeta API"]
    assert entries[2].url == "https://zeta.example/stac"  # trailing slash trimmed
    assert entries[1].detail.endswith("...")  # long summary truncated


def test_parse_stac_index_on_bad_data_still_offers_kentucky():
    assert parse_stac_index(None) == [BUILTIN_ENTRY]
    assert parse_stac_index({"not": "a list"}) == [BUILTIN_ENTRY]


def test_url_checks():
    assert is_valid_api_url("https://a.example/stac")
    assert not is_valid_api_url("ftp://a.example")
    assert not is_valid_api_url("not a url")
    assert not is_usable_base_url("https://a.example/catalog.JSON")
    assert is_default_uri("") and is_default_uri(DEFAULT_BASE_URI + "/") and not is_default_uri(OTHER)


def test_sources_round_trip_and_default_fallback():
    sources = [DEFAULT_SOURCE, ApiSource("Earth Search", OTHER)]
    assert deserialize_sources(serialize_sources(sources)) == sources
    assert deserialize_sources(None) == [DEFAULT_SOURCE]
    assert deserialize_sources("not json") == [DEFAULT_SOURCE]
    assert deserialize_sources('[{"name": "x", "base_uri": "nonsense"}]') == [DEFAULT_SOURCE]
    # Only other APIs saved (the user replaced KyFromAbove) -- respected, not forced back.
    assert deserialize_sources(serialize_sources([ApiSource("ES", OTHER)])) == [ApiSource("ES", OTHER)]
    # A saved built-in entry is recognized by URL, keeping its default flag.
    assert deserialize_sources('[{"name": "renamed", "base_uri": "%s"}]' % DEFAULT_BASE_URI) == [DEFAULT_SOURCE]


def test_add_and_replace_sources():
    added = add_source([DEFAULT_SOURCE], "Earth Search", OTHER + "/", replace=False)
    assert added == [DEFAULT_SOURCE, ApiSource("Earth Search", OTHER)]
    assert add_source(added, "dup", OTHER, replace=False) == added  # already active
    assert add_source(added, "Earth Search", OTHER, replace=True) == [ApiSource("Earth Search", OTHER)]
    # Picking Kentucky's URL is the way back, as the built-in source itself.
    assert add_source([ApiSource("ES", OTHER)], "whatever", DEFAULT_BASE_URI, replace=True) == [DEFAULT_SOURCE]
    assert add_source([DEFAULT_SOURCE], "", OTHER, replace=False)[1].name == OTHER  # blank name -> URL
    assert source_name(added, OTHER) == "Earth Search"


def test_lidar_detection_for_other_sources():
    def c(**kw):
        return Collection(id=kw.pop("id", "x"), source=OTHER, **kw)

    assert is_lidar_collection(c(stac_extensions=["https://stac-extensions.github.io/pointcloud/v1.0.0/schema.json"]))
    assert is_lidar_collection(c(keywords=["Point Cloud"]))
    assert is_lidar_collection(c(item_asset_keys=["copc"]))
    # A LiDAR-derived DEM is a raster -- "lidar" alone isn't enough.
    assert not is_lidar_collection(c(keywords=["LiDAR", "DEM"]))
    # Another API's "laz-..." id means nothing; only the built-in catalog uses that naming.
    assert not is_lidar_collection(c(id="laz-thing"))
    assert is_lidar_collection(Collection(id="laz-phase3"))


def test_split_puts_builtin_first_then_other_sources():
    cols = [Collection(id="b", source=OTHER), Collection(id="z"), Collection(id="a")]
    imagery, _ = split_collections(cols)
    assert [(x.id, x.source) for x in imagery] == [("a", ""), ("z", ""), ("b", OTHER)]


def test_other_sources_are_listed_on_both_tabs():
    cols = [Collection(id="laz-phase3"), Collection(id="dem-phase2"), Collection(id="x", source=OTHER)]
    imagery, lidar = split_collections(cols)
    assert [c.id for c in imagery] == ["dem-phase2", "x"]
    assert [c.id for c in lidar] == ["laz-phase3", "x"]


def test_lidar_thumbnail_fallback_is_kentucky_only():
    ky = Item(id="t", collection="laz-phase3")
    other = Item(id="t", collection="laz-phase3", source=OTHER)
    assert thumbnail_href(ky, lidar=True) is not None
    assert thumbnail_href(other, lidar=True) is None


def test_tiler_url_persists_and_is_validated():
    tiler = "https://titiler.example.com"
    src = ApiSource("ES", OTHER, tiler_url=tiler)
    assert deserialize_sources(serialize_sources([DEFAULT_SOURCE, src])) == [DEFAULT_SOURCE, src]
    # Older saved lists (no tiler_url) and junk values load as "no tile server".
    assert deserialize_sources('[{"name": "ES", "base_uri": "%s"}]' % OTHER)[0].tiler_url == ""
    assert deserialize_sources('[{"name": "ES", "base_uri": "%s", "tiler_url": "nonsense"}]' % OTHER)[0].tiler_url == ""


def test_add_source_with_tiler_and_with_tiler_url():
    added = add_source([DEFAULT_SOURCE], "ES", OTHER, replace=False, tiler_url="https://t.example/")
    assert added[1].tiler_url == "https://t.example"
    assert add_source([DEFAULT_SOURCE], "ES", OTHER, replace=False, tiler_url="bad")[1].tiler_url == ""
    changed = with_tiler_url(added, OTHER + "/", "https://other.example")
    assert changed[1].tiler_url == "https://other.example" and changed[0] == DEFAULT_SOURCE
    assert tiler_for(changed, OTHER) == "https://other.example" and tiler_for(changed, DEFAULT_BASE_URI) == ""
    assert with_tiler_url(changed, OTHER, "")[1].tiler_url == ""
    # The built-in source never takes a tile server override.
    assert with_tiler_url([DEFAULT_SOURCE], DEFAULT_BASE_URI, "https://x.example") == [DEFAULT_SOURCE]


def test_data_asset_key():
    it = Item.from_dict({"id": "x", "assets": {"thumbnail": {"href": "t.png"}, "visual": {"href": "v.tif"}}})
    assert it.data_asset_key() == "visual" and Item().data_asset_key() is None


def test_item_json_text_and_html():
    from kentucky_stac.item_json import item_json_text, json_to_html

    raw = {"id": "t1", "properties": {"note": "a <b> & c"}, "assets": {"data": {"href": "https://x.example/a.tif?sig=1&b=2"}}}
    item = Item.from_dict(raw)
    assert '"id": "t1"' in item_json_text(item) and item_json_text(item).startswith("{\n  ")
    page = json_to_html(item_json_text(item))
    assert "a &lt;b&gt; &amp; c" in page  # escaped
    assert '<a href="https://x.example/a.tif?sig=1&amp;b=2">https://x.example/a.tif?sig=1&amp;b=2</a>' in page
    # An Item built by hand (no raw document) still renders from its fields.
    assert '"id": "h"' in item_json_text(Item(id="h"))
