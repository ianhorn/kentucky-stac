import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from kentucky_stac.stac import Asset, Item, SearchQuery, StacClient, StacError
from kentucky_stac.stac.client import format_datetime_range

BASE = "https://example.test"


class FakeTransport:
    """Serves canned JSON by (method, url) and records every request."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, json.loads(body) if body else None, headers))
        key = (method, url)
        if key not in self.responses:
            raise StacError(f"unexpected request {key}", status=404)
        return json.dumps(self.responses[key]).encode()


def item(item_id, **assets):
    return {
        "id": item_id,
        "collection": "c1",
        "bbox": [-85, 37, -84, 38],
        "properties": {"datetime": "2020-01-01T00:00:00Z", "proj:epsg": 3089},
        "assets": {k: {"href": v} for k, v in assets.items()},
    }


def test_collections():
    t = FakeTransport(
        {
            ("GET", f"{BASE}/collections"): {
                "collections": [
                    {
                        "id": "dem-phase1",
                        "title": " ",
                        "extent": {
                            "spatial": {"bbox": [[-89, 36, -81, 39]]},
                            "temporal": {"interval": [["2011-01-01T00:00:00Z", None]]},
                        },
                    }
                ]
            }
        }
    )
    (c,) = StacClient(BASE + "/", t).collections()
    assert c.id == "dem-phase1"
    assert c.title_or_id == "dem-phase1"  # blank title falls back to id
    assert c.bbox == [-89, 36, -81, 39]
    assert c.interval == ["2011-01-01T00:00:00Z", None]


def test_get_search_url():
    t = FakeTransport({})
    client = StacClient(BASE, t)
    q = SearchQuery(
        collections=["a", "b"],
        bbox=[-85.5, 37.0, -84.5, 38.0],
        start=datetime(2020, 1, 2, 3, 4, 5),
        end=None,
        text="lex ington",
        limit=99999,
    )
    url = client._search_url(q)
    parsed = urlparse(url)
    assert parsed.path == "/search"
    params = parse_qs(parsed.query)
    assert params["collections"] == ["a,b"]
    assert params["bbox"] == ["-85.5,37.0,-84.5,38.0"]
    assert params["datetime"] == ["2020-01-02T03:04:05Z/.."]
    assert params["q"] == ["lex ington"]
    assert params["limit"] == ["10000"]  # clamped


def test_intersects_uses_post_and_wins_over_bbox():
    geom = {"type": "Point", "coordinates": [-85, 38]}
    t = FakeTransport({("POST", f"{BASE}/search"): {"features": [item("i1", data="x.tif")]}})
    page = StacClient(BASE, t).search(
        SearchQuery(collections=["c1"], intersects=geom, bbox=[0, 0, 1, 1], limit=10)
    )
    (method, url, body, headers) = t.calls[0]
    assert body == {"collections": ["c1"], "intersects": geom, "limit": 10}
    assert headers["Content-Type"] == "application/json"  # stac-server ignores a geo+json body
    assert [f.id for f in page.features] == ["i1"]


def test_pagination_get_next_and_max_items():
    p1 = {
        "features": [item("a", data="a.tif"), item("b", data="b.tif")],
        "links": [{"rel": "next", "href": f"{BASE}/search?page=2"}],
    }
    p2 = {"features": [item("c", data="c.tif")], "links": []}
    t = FakeTransport(
        {("GET", f"{BASE}/search?limit=2"): p1, ("GET", f"{BASE}/search?page=2"): p2}
    )
    client = StacClient(BASE, t)
    assert [i.id for i in client.iter_items(SearchQuery(limit=2))] == ["a", "b", "c"]
    assert [i.id for i in client.iter_items(SearchQuery(limit=2), max_items=2)] == ["a", "b"]


def test_pagination_post_next_with_merge():
    geom = {"type": "Point", "coordinates": [-85, 38]}
    p1 = {
        "features": [item("a", data="a.tif")],
        "links": [
            {
                "rel": "next",
                "href": f"{BASE}/search",
                "method": "POST",
                "body": {"token": "next:abc"},
                "merge": True,
            }
        ],
    }
    p2 = {"features": [item("b", data="b.tif")]}
    t = FakeTransport({("POST", f"{BASE}/search"): p1})
    client = StacClient(BASE, t)
    query = SearchQuery(intersects=geom, limit=1)
    page = client.search(query)

    t.responses[("POST", f"{BASE}/search")] = p2
    nxt = client.next_page(page, query)
    assert [i.id for i in nxt.features] == ["b"]
    assert t.calls[-1][2] == {"intersects": geom, "limit": 1, "token": "next:abc"}
    assert client.next_page(nxt, query) is None


def test_http_error_carries_body():
    def transport(method, url, body, headers):
        raise StacError("STAC API 400 Bad Request: bad bbox", status=400)

    with pytest.raises(StacError) as exc:
        StacClient(BASE, transport).collections()
    assert exc.value.status == 400
    assert "bad bbox" in str(exc.value)


def test_collections_follow_next_links():
    t = FakeTransport(
        {
            ("GET", f"{BASE}/collections"): {
                "collections": [{"id": "a", "keywords": ["point cloud"], "stac_extensions": ["x/pointcloud/v1"]}],
                "links": [{"rel": "next", "href": f"{BASE}/collections?page=2"}],
            },
            ("GET", f"{BASE}/collections?page=2"): {
                "collections": [{"id": "b", "item_assets": {"visual": {}, "nir": {}}}],
                "links": [],
            },
        }
    )
    cols = StacClient(BASE, t).collections()
    assert [c.id for c in cols] == ["a", "b"]
    assert cols[0].keywords == ["point cloud"] and cols[0].stac_extensions == ["x/pointcloud/v1"]
    assert cols[1].item_asset_keys == ["visual", "nir"]


def test_collections_stop_on_a_next_link_that_loops():
    t = FakeTransport(
        {("GET", f"{BASE}/collections"): {"collections": [{"id": "a"}], "links": [{"rel": "next", "href": f"{BASE}/collections"}]}}
    )
    assert [c.id for c in StacClient(BASE, t).collections()] == ["a"]
    assert len(t.calls) == 1


def test_data_asset_prefers_visual_over_per_band_data_roles():
    # Sentinel-2 style: every band carries the data role; "visual" is the renderable composite.
    it = Item.from_dict(
        {
            "id": "s2",
            "assets": {
                "aot": {"href": "aot.tif", "roles": ["data"]},
                "blue": {"href": "blue.tif", "roles": ["data", "reflectance"]},
                "visual": {"href": "visual.tif", "roles": ["visual"]},
            },
        }
    )
    assert it.data_asset().href == "visual.tif"
    # A plain "data" key still wins outright (the KyFromAbove layout).
    ky = Item.from_dict({"id": "k", "assets": {"visual": {"href": "v.tif"}, "data": {"href": "d.tif"}}})
    assert ky.data_asset().href == "d.tif"


def test_invalid_json_raises_stac_error():
    with pytest.raises(StacError):
        StacClient(BASE, lambda *a: b"<html>").collections()


def test_datetime_range_formatting():
    assert format_datetime_range(None, None) is None
    assert format_datetime_range(None, datetime(2021, 5, 6, 7, 8, 9)) == "../2021-05-06T07:08:09Z"
    est = timezone(timedelta(hours=-5))
    assert (
        format_datetime_range(datetime(2021, 1, 1, 0, 0, tzinfo=est), "2022-01-01")
        == "2021-01-01T05:00:00Z/2022-01-01"
    )


def test_item_asset_helpers():
    it = Item.from_dict(
        {
            "id": "x",
            "assets": {
                "thumbnail": {"href": "t.png", "roles": ["thumbnail"]},
                "metadata": {"href": "m.xml"},
                "cloud": {"href": "https://h/tile.copc.laz"},
            },
        }
    )
    assert it.data_asset().href.endswith(".copc.laz")  # skips thumbnail/metadata
    assert it.thumbnail_asset().href == "t.png"
    assert [a.is_copc for a in it.lidar_assets()] == [True]

    keyed = Item.from_dict({"assets": {"other": {"href": "o.tif"}, "data": {"href": "d.tif"}}})
    assert keyed.data_asset().href == "d.tif"  # explicit "data" key wins
    assert Item().data_asset() is None


def test_asset_flags():
    assert Asset(href="A.COPC.LAZ").is_copc and Asset(href="A.COPC.LAZ").is_lidar
    assert Asset(href="a.las").is_lidar and not Asset(href="a.las").is_copc
    assert not Asset(href="a.tif").is_lidar
