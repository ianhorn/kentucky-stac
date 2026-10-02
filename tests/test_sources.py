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


def test_lidar_thumbnail_fallback_is_kentucky_only():
    ky = Item(id="t", collection="laz-phase3")
    other = Item(id="t", collection="laz-phase3", source=OTHER)
    assert thumbnail_href(ky, lidar=True) is not None
    assert thumbnail_href(other, lidar=True) is None
