import pytest

from kentucky_stac.bands import (
    available_combinations,
    available_indices,
    check_formula,
    find_band,
    formula_names,
    item_bands,
    to_expression,
)
from kentucky_stac.stac import Item

LANDSAT = {  # Astraea-style: common name at asset level, key nir08
    "id": "LC08",
    "assets": {
        "red": {"href": "https://x/B4.TIF", "type": "image/vnd.stac.geotiff; cloud-optimized=true", "eo:common_name": "red", "title": "Red Band (B4)"},
        "green": {"href": "https://x/B3.TIF", "type": "image/tiff; application=geotiff", "eo:common_name": "green"},
        "blue": {"href": "https://x/B2.TIF", "type": "image/tiff; application=geotiff", "eo:common_name": "blue"},
        "nir08": {"href": "https://x/B5.TIF", "type": "image/tiff; application=geotiff", "eo:common_name": "nir"},
        "swir16": {"href": "https://x/B6.TIF", "type": "image/tiff; application=geotiff", "eo:common_name": "swir16"},
        "MTL.xml": {"href": "https://x/MTL.xml", "type": "application/xml"},
        "thumbnail": {"href": "https://x/t.jpeg", "type": "image/jpeg"},
    },
}
NAIP = {  # one four-band asset
    "id": "naip",
    "assets": {
        "image": {
            "href": "https://x/naip.tif",
            "type": "image/tiff; application=geotiff",
            "eo:bands": [{"name": "Red", "common_name": "red"}, {"name": "Green", "common_name": "green"}, {"name": "Blue", "common_name": "blue"}, {"name": "NIR", "common_name": "nir"}],
        }
    },
}
LC2 = {  # scale/offset from raster:bands, nir08 as the common name (Planetary Computer style)
    "id": "lc2",
    "assets": {
        "red": {"href": "https://x/r.tif", "type": "image/tiff; application=geotiff", "eo:bands": [{"common_name": "red"}], "raster:bands": [{"scale": 0.0000275, "offset": -0.2}]},
        "nir08": {"href": "https://x/n.tif", "type": "image/tiff; application=geotiff", "eo:bands": [{"common_name": "nir08"}], "raster:bands": [{"scale": 0.0000275, "offset": -0.2}]},
    },
}


def bands(raw):
    return item_bands(Item.from_dict(raw))


def test_only_raster_assets_become_bands():
    keys = {b.asset for b in bands(LANDSAT)}
    assert keys == {"red", "green", "blue", "nir08", "swir16"}


def test_find_band_uses_common_names_and_stand_ins():
    b = bands(LANDSAT)
    assert find_band(b, "nir").asset == "nir08"  # common name nir, asset key nir08
    assert find_band(b, "NIR").asset == "nir08"
    assert find_band(b, "swir16").asset == "swir16"
    assert find_band(b, "swir22") is None
    assert find_band(bands(LC2), "nir").asset == "nir08"  # common name nir08 stands in for nir


def test_a_multiband_asset_gives_one_band_each():
    b = bands(NAIP)
    assert [(x.asset, x.index, x.common) for x in b] == [("image", 1, "red"), ("image", 2, "green"), ("image", 3, "blue"), ("image", 4, "nir")]
    assert find_band(b, "nir").index == 4


def test_available_combinations_and_indices():
    b = bands(LANDSAT)
    assert "True color" in available_combinations(b) and "Color infrared (vegetation)" in available_combinations(b)
    assert "Geology" not in available_combinations(b)  # needs swir22
    names = available_indices(b)
    assert "NDVI (vegetation)" in names and "NDMI (moisture)" in names and "NBR (burn ratio)" not in names
    assert "NDVI (vegetation)" in available_indices(bands(NAIP)) and "NDMI (moisture)" not in available_indices(bands(NAIP))
    assert "My ratio" in available_indices(b, {"My ratio": "red / green"})


def test_formula_names_and_checks():
    assert formula_names("(nir - red) / (nir + red)") == ["nir", "red"]
    assert formula_names("sqrt(nir) + abs(red)") == ["nir", "red"]
    assert check_formula("(nir - red) / (nir + red)") is None
    assert check_formula("") is not None
    assert check_formula("nir; import os") is not None
    assert check_formula("(nir - red") is not None
    assert check_formula("2 + 2") is not None  # no band


def test_to_expression_applies_scale_and_offset():
    expr = to_expression("(nir - red) / (nir + red)", lambda band: f'"{band.asset}@{band.index}"', bands(LC2))
    assert '"nir08@1"' in expr and '"red@1"' in expr
    assert "* 2.75e-05 + -0.2" in expr
    plain = to_expression("nir / red", lambda band: f'"{band.asset}@{band.index}"', bands(LANDSAT))
    assert plain == '"nir08@1" / "red@1"'
    with pytest.raises(ValueError):
        to_expression("nir / swir22", lambda band: "x", bands(LANDSAT))
