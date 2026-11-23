from kentucky_stac import s3


def test_parse_and_is_s3():
    assert s3.is_s3("s3://b/k.tif") and s3.is_s3("S3://b/k") and not s3.is_s3("https://b/k")
    assert s3.parse("s3://my-bucket/a/b%20c.tif") == ("my-bucket", "a/b c.tif")
    assert s3.parse("s3://bucket") == ("bucket", "")
    assert s3.parse("https://x/y") is None and s3.parse("s3:///nobucket") is None


def test_to_https():
    assert s3.to_https("s3://naip-visualization/ky/a b.tif") == "https://naip-visualization.s3.amazonaws.com/ky/a%20b.tif"
    # a dotted bucket name can't use the wildcard certificate, so it uses the path style
    assert s3.to_https("s3://my.bucket/k.tif") == "https://s3.amazonaws.com/my.bucket/k.tif"
    assert s3.to_https("https://already/ok.tif") == "https://already/ok.tif"
    assert s3.vsis3_path("s3://b/a/k.tif") == "/vsis3/b/a/k.tif" and s3.vsis3_path("https://x") is None


def test_credentials_available(tmp_path=None):
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        assert not s3.credentials_available({}, home)
        assert s3.credentials_available({"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b"}, home)
        assert not s3.credentials_available({"AWS_ACCESS_KEY_ID": "a"}, home)  # half a pair isn't enough
        os.makedirs(os.path.join(home, ".aws"))
        open(os.path.join(home, ".aws", "credentials"), "w").write("[default]\n")
        assert s3.credentials_available({}, home)
        other = os.path.join(home, "elsewhere")
        assert not s3.credentials_available({"AWS_SHARED_CREDENTIALS_FILE": other}, home)


# ---- S3-compatible services that are not Amazon (Copernicus Data Space) -------------------------------------


def test_the_copernicus_bucket_has_its_own_endpoint():
    assert s3.endpoint_for("eodata") == "eodata.dataspace.copernicus.eu"
    assert s3.endpoint_for("naip-visualization") is None
    assert s3.endpoint_options("eodata.dataspace.copernicus.eu") == {
        "AWS_S3_ENDPOINT": "eodata.dataspace.copernicus.eu",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_HTTPS": "YES",
    }
    assert s3.endpoint_options("http://127.0.0.1:9000")["AWS_HTTPS"] == "NO"
    assert s3.endpoint_options("http://127.0.0.1:9000")["AWS_S3_ENDPOINT"] == "127.0.0.1:9000"


def test_a_copernicus_https_address_is_read_as_its_s3_address():
    safe = "https://eodata.dataspace.copernicus.eu/eodata/Sentinel-2/MSI/L2A/2026/11/22/S2B_X.SAFE"
    assert s3.as_s3(safe) == "s3://eodata/Sentinel-2/MSI/L2A/2026/11/22/S2B_X.SAFE"
    assert s3.as_s3("https://EODATA.dataspace.copernicus.eu/eodata/a/b.jp2") == "s3://eodata/a/b.jp2"
    # anything else is left alone
    assert s3.as_s3("https://download.dataspace.copernicus.eu/odata/v1/Products(1)/$value").startswith("https://download")
    assert s3.as_s3("https://example.com/eodata/x.tif") == "https://example.com/eodata/x.tif"
    assert s3.as_s3("s3://eodata/x.jp2") == "s3://eodata/x.jp2"
    assert s3.as_s3("") == "" and s3.as_s3(None) is None


def test_the_https_fallback_for_a_copernicus_object_uses_its_own_host():
    assert s3.to_https("s3://eodata/a/b.jp2") == "https://eodata.dataspace.copernicus.eu/eodata/a/b.jp2"
    # an ordinary AWS bucket is unchanged
    assert s3.to_https("s3://naip-visualization/k.tif") == "https://naip-visualization.s3.amazonaws.com/k.tif"
