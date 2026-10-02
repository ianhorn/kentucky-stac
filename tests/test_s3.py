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
