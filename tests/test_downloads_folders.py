"""Downloading a Sentinel-2 product from Copernicus Data Space: a `.SAFE` is a folder on an S3-compatible service,
read with the user's S3 keys and fetched file by file.

A small fake S3 service stands in for Copernicus (path-style addresses, bucket listings, range reads), so the
whole route is tested without real keys: GDAL's /vsis3/ lists the folder, and each file is downloaded.
"""

import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("qgis.core", reason="needs QGIS (qgis.core)")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("osgeo.gdal", reason="needs GDAL's Python bindings")

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402

from kentucky_stac import auth, downloads, s3  # noqa: E402
from kentucky_stac.sources import ApiSource  # noqa: E402
from kentucky_stac.stac import Item  # noqa: E402

PRODUCT = "Sentinel-2/MSI/L2A/2026/11/22/S2B_TEST.SAFE"
OBJECTS = {
    f"{PRODUCT}/MTD_MSIL2A.xml": b"<product/>" * 40,
    f"{PRODUCT}/manifest.safe": b"manifest " * 30,
    f"{PRODUCT}/GRANULE/L2A_T16SFH/IMG_DATA/R10m/T16SFH_B04_10m.jp2": bytes(range(256)) * 300,
    f"{PRODUCT}/GRANULE/L2A_T16SFH/IMG_DATA/R10m/T16SFH_B03_10m.jp2": bytes(reversed(range(256))) * 200,
    f"{PRODUCT}/GRANULE/L2A_T16SFH/MTD_TL.xml": b"<tile/>" * 25,
    "Sentinel-2/other/unrelated.txt": b"not in the product",
}


class _S3(BaseHTTPRequestHandler):
    seen = []  # (method, path, headers) of every request

    def log_message(self, *args):
        pass

    def _split(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.lstrip("/").split("/", 1)
        key = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        return parts[0], key, urllib.parse.parse_qs(parsed.query)

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _listing(self, bucket, query):
        prefix = query.get("prefix", [""])[0]
        delimiter = query.get("delimiter", [""])[0]
        contents, common = [], set()
        for key in sorted(OBJECTS):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                common.add(prefix + rest.split(delimiter)[0] + delimiter)
            else:
                contents.append(key)
        xml = ['<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
        xml.append(f"<Name>{bucket}</Name><Prefix>{prefix}</Prefix><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>")
        for key in contents:
            xml.append(
                f"<Contents><Key>{key}</Key><LastModified>2026-11-22T00:00:00.000Z</LastModified><ETag>e</ETag>"
                f"<Size>{len(OBJECTS[key])}</Size><StorageClass>STANDARD</StorageClass></Contents>"
            )
        for folder in sorted(common):
            xml.append(f"<CommonPrefixes><Prefix>{folder}</Prefix></CommonPrefixes>")
        xml.append("</ListBucketResult>")
        self._send(200, "".join(xml).encode(), {"Content-Type": "application/xml"})

    def _handle(self):
        _S3.seen.append((self.command, self.path, dict(self.headers)))
        bucket, key, query = self._split()
        if bucket != "eodata":
            self._send(404)
            return
        if not key or "prefix" in query or "list-type" in query:
            self._listing(bucket, query)
            return
        body = OBJECTS.get(key)
        if body is None:
            self._send(404, b"<Error><Code>NoSuchKey</Code></Error>", {"Content-Type": "application/xml"})
            return
        headers = {"Accept-Ranges": "bytes", "Last-Modified": "Sun, 22 Nov 2026 00:00:00 GMT", "ETag": "e"}
        requested = self.headers.get("Range")
        if requested and requested.startswith("bytes="):
            start, _, end = requested[6:].partition("-")
            first = int(start or 0)
            last = min(int(end), len(body) - 1) if end else len(body) - 1
            headers["Content-Range"] = f"bytes {first}-{last}/{len(body)}"
            self._send(206, body[first : last + 1], headers)
        else:
            self._send(200, body, headers)

    do_GET = do_HEAD = _handle


@pytest.fixture
def copernicus(qgis_app, monkeypatch):
    """A fake Copernicus S3 service, with the user's keys 'configured' for it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _S3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _S3.seen.clear()
    monkeypatch.setitem(s3.S3_ENDPOINTS, "eodata", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(auth, "_aws_keys", lambda cfg: ("AKIATESTKEY", "test-secret", ""))
    yield server
    server.shutdown()


def _product_url(key=PRODUCT):
    return f"s3://eodata/{key}"


def test_a_product_folder_is_listed_file_by_file(copernicus):
    auth.prepare_asset(_product_url(), "cfg1234")
    listing = downloads.list_folder(_product_url())
    inside = sorted(k[len(PRODUCT) + 1:] for k in OBJECTS if k.startswith(PRODUCT))
    assert sorted(relative for relative, _ in listing) == inside
    assert dict(listing)["GRANULE/L2A_T16SFH/IMG_DATA/R10m/T16SFH_B04_10m.jp2"] == 256 * 300
    assert not any("unrelated" in relative for relative, _ in listing)  # only what is inside the folder


def test_a_single_file_is_not_a_folder(copernicus):
    auth.prepare_asset(_product_url(), "cfg1234")
    assert downloads.list_folder(_product_url(f"{PRODUCT}/manifest.safe")) is None


def test_requests_go_to_the_copernicus_endpoint_signed_with_the_keys_and_not_requester_pays(copernicus):
    auth.prepare_asset(_product_url(), "cfg1234")
    downloads.list_folder(_product_url())
    assert _S3.seen, "GDAL never reached the fake service"
    for _method, path, headers in _S3.seen:
        assert path.startswith("/eodata/"), path  # path style at the endpoint, not bucket.s3.amazonaws.com
        lowered = {k.lower(): v for k, v in headers.items()}
        assert "AKIATESTKEY" in lowered.get("authorization", ""), headers
        assert "x-amz-request-payer" not in lowered, "a requester-pays header is for AWS buckets, not Copernicus"


def test_a_folder_job_becomes_a_job_per_file(copernicus, tmp_path):
    auth.prepare_asset(_product_url(), "cfg1234")
    folder_job = downloads.DownloadJob("item", _product_url(), str(tmp_path / "S2B_TEST.SAFE"), authcfg="cfg1234")
    other = downloads.DownloadJob("two", "https://x/y.tif", str(tmp_path / "y.tif"))
    listing = downloads.list_folder(_product_url())
    jobs, sizes = downloads.expand_folders([folder_job, other], {_product_url(): listing})
    inside = [k for k in OBJECTS if k.startswith(PRODUCT)]
    assert jobs[-1] is other and len(jobs) == len(inside) + 1
    expected = str(tmp_path / "S2B_TEST.SAFE" / "GRANULE" / "L2A_T16SFH" / "MTD_TL.xml")
    assert any(j.dest == expected and j.url.endswith("/GRANULE/L2A_T16SFH/MTD_TL.xml") and j.authcfg == "cfg1234" for j in jobs)
    assert sizes[f"{_product_url()}/manifest.safe"] == len(OBJECTS[f"{PRODUCT}/manifest.safe"])


def test_a_listing_is_written_under_the_folder(tmp_path):
    job = downloads.DownloadJob("item", "s3://eodata/p.SAFE", str(tmp_path / "p.SAFE"))
    jobs, _ = downloads.expand_folders([job], {"s3://eodata/p.SAFE": [("ok/a.xml", 1)]})
    assert jobs[0].dest == str(tmp_path / "p.SAFE" / "ok" / "a.xml")


def test_unsafe_paths_in_a_listing_are_dropped(monkeypatch):
    """Nothing in a listing can make a file land outside the folder it is downloaded into."""

    class _File:
        size = 1

        @staticmethod
        def IsDirectory():
            return False

    class _Folder(_File):
        @staticmethod
        def IsDirectory():
            return True

    from osgeo import gdal

    tree = {"/vsis3/eodata/p.SAFE": ["a.xml", "..", "x/../y.xml", "sub", "/abs.xml", "..\\win.xml"], "/vsis3/eodata/p.SAFE/sub": ["b.xml"]}
    monkeypatch.setattr(gdal, "ReadDir", lambda path: tree.get(path))
    monkeypatch.setattr(gdal, "VSIStatL", lambda path, flags=0: _Folder() if path.endswith("/sub") else _File())
    # ".." and names holding a separator are dropped; a leading slash is just trimmed, leaving a plain name
    assert sorted(downloads.list_tree("/vsis3/eodata/p.SAFE")) == [("a.xml", 1), ("abs.xml", 1), ("sub/b.xml", 1)]


def test_a_local_folder_is_listed_with_its_sizes(tmp_path):
    """list_tree works on any GDAL virtual path, so the plain file system tests the walking."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "top.xml").write_bytes(b"12345")
    (tmp_path / "a" / "mid.xml").write_bytes(b"123")
    (tmp_path / "a" / "b" / "deep.jp2").write_bytes(b"1")
    assert sorted(downloads.list_tree(str(tmp_path))) == [("a/b/deep.jp2", 1), ("a/mid.xml", 3), ("top.xml", 5)]
    assert downloads.list_tree(str(tmp_path / "top.xml")) is None  # a file is not a folder
    assert downloads.list_tree(str(tmp_path / "missing")) is None


def test_the_https_product_address_is_downloaded_as_s3_when_a_login_is_set(qgis_app):
    """Semablu's item names the SAFE folder by its https address on Copernicus's S3 service."""
    https = f"https://eodata.dataspace.copernicus.eu/eodata/{PRODUCT}"
    asset = {"href": https, "type": "application/octet-stream", "roles": ["data"]}
    item = Item.from_dict({"id": "65f9", "assets": {"product": asset}})
    item.source = "https://console.semablu.com/py/stac"
    auth.set_sources([ApiSource("Semablu", item.source, auth_cfg="cfg1234")])
    try:
        jobs, missing = downloads.plan_downloads([item], False, "D:/dl")
    finally:
        auth.set_sources([])
    assert not missing and jobs[0].url == f"s3://eodata/{PRODUCT}"
    assert os.path.basename(jobs[0].dest) == "S2B_TEST.SAFE"
    # without a login the address is left as it is (and the failure message says why)
    jobs, _ = downloads.plan_downloads([item], False, "D:/dl")
    assert jobs[0].url == https


def test_a_whole_product_is_downloaded_with_the_keys(copernicus, tmp_path):
    """End to end: list the folder, expand it, download every file through DownloadManager, and compare the bytes."""
    auth.prepare_asset(_product_url(), "cfg1234")
    folder_job = downloads.DownloadJob("item", _product_url(), str(tmp_path / "S2B_TEST.SAFE"), authcfg="cfg1234")
    jobs, sizes = downloads.expand_folders([folder_job], {_product_url(): downloads.list_folder(_product_url())})
    results = []
    manager = downloads.DownloadManager(jobs, sizes, max_parallel=3)
    manager.finished.connect(lambda done, failed, cancelled: results.append((done, failed, cancelled)))
    manager.start()
    end = time.time() + 60
    while not results and time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    assert results, "the download never finished"
    done, failed, cancelled = results[0]
    assert not failed and not cancelled and len(done) == len(jobs)
    for key, body in OBJECTS.items():
        if key.startswith(PRODUCT):
            path = tmp_path / "S2B_TEST.SAFE" / key[len(PRODUCT) + 1:]
            assert path.read_bytes() == body, key
    assert not list(tmp_path.rglob("*.part"))
    assert not (tmp_path / "S2B_TEST.SAFE" / "unrelated.txt").exists()
