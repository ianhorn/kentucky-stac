from kentucky_stac import azure


def test_parse():
    url = "https://sentinel3euwest.blob.core.windows.net/sentinel-3/OLCI/a/b/iwv.nc"
    assert azure.parse(url) == ("sentinel3euwest", "sentinel-3", "OLCI/a/b/iwv.nc")
    assert azure.parse("https://acct.blob.core.windows.net/box") == ("acct", "box", "")
    assert azure.parse("https://example.com/c/k.nc") is None
    assert azure.parse("s3://bucket/key") is None
    assert azure.parse("https://acct.blob.core.windows.net/") is None  # no container
    assert azure.parse("") is None


def test_token_attach_and_detection():
    base = "https://a.blob.core.windows.net/c/k.tif"
    assert azure.with_token(base, "?se=1&sig=abc") == base + "?se=1&sig=abc"
    assert azure.with_token(base + "?x=1", "se=1&sig=abc") == base + "?x=1&se=1&sig=abc"
    assert azure.is_signed(base + "?se=1&sig=abc") and not azure.is_signed(base) and not azure.is_signed(base + "?x=1")


def test_expiry_epoch():
    assert azure.expiry_epoch("2026-10-02T22:30:51Z") == 1790980251.0
    assert azure.expiry_epoch("garbage") is None and azure.expiry_epoch(None) is None
