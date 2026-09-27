"""GDAL HTTP setup: certificate trust and connection tuning for reading remote COGs.

GDAL opens remote COGs with its own libcurl, separate from Qt's network stack (which QGIS's other
network code, and the QGIS point cloud provider, use instead -- this module has no effect on those).
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Optional

from qgis.core import QgsApplication
from qgis.PyQt.QtNetwork import QSslConfiguration

_ca_bundle_applied: Optional[str] = None
_http_tuning_applied = False


def _gdal():
    try:
        from osgeo import gdal

        return gdal
    except ImportError:
        return None


def _existing_bundle(gdal) -> Optional[str]:
    for path in (gdal.GetConfigOption("CURL_CA_BUNDLE"), os.environ.get("CURL_CA_BUNDLE"), os.environ.get("SSL_CERT_FILE")):
        if path and os.path.isfile(path):
            return path
    return None


def ensure_ca_bundle() -> Optional[str]:
    """Make GDAL's libcurl trust the same certificate authorities QGIS (Qt) does.

    On a machine that inspects HTTPS (antivirus "web shield", corporate proxy) the interceptor's
    root certificate is in the Windows trust store, which Qt uses, but not in GDAL's own bundle (on
    OSGeo4W, curl-ca-bundle.crt), so every remote raster fails with "unable to get local issuer
    certificate". We write a bundle containing whatever GDAL was already using plus the CAs Qt
    trusts, and point GDAL at it. Verification stays on. Windows only. Returns the combined bundle's
    path once applied (also on repeat calls).
    """
    global _ca_bundle_applied
    if _ca_bundle_applied is not None:
        return _ca_bundle_applied
    if sys.platform != "win32":
        return None
    gdal = _gdal()
    if gdal is None:
        return None
    certificates = QSslConfiguration.systemCaCertificates()
    if not certificates:
        return None

    directory = os.path.join(QgsApplication.qgisSettingsDirPath() or tempfile.gettempdir(), "kentucky_stac")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "ca-bundle.pem")
    tmp = path + ".tmp"
    existing = _existing_bundle(gdal)
    with open(tmp, "wb") as out:
        if existing:
            with open(existing, "rb") as f:
                out.write(f.read())
                out.write(b"\n")
        for certificate in certificates:
            out.write(bytes(certificate.toPem()))
    os.replace(tmp, path)
    gdal.SetConfigOption("CURL_CA_BUNDLE", path)
    _ca_bundle_applied = path
    return path


def ensure_http_tuning() -> bool:
    """Trade some of a COG's "fetch only what you need" precision for fewer HTTP requests: opening
    a tile and reading one screen's worth of pixels pulled 1.12 MB over 4 requests at GDAL's 16 KB
    default chunk size, versus 3.07 MB over 3 requests at the 1 MB size set here (captured via
    CPL_CURL_VERBOSE on this project's own traffic). That's a real cost against the point of a COG.

    The win is real and reproducible -- cold process per run, alternating, same tile, so not
    connection-pool warm-up: opening a 20000x20000 3-inch COG went from ~5.2-5.8s to ~1.2-1.5s -- but
    its cause is NOT pinned down, and it is likely NOT antivirus/proxy inspection overhead as first
    assumed: live A/B testing on the user's machine (Norton's Smart Firewall disabled, then
    separately Auto-Protect disabled, each for several minutes) changed neither the untuned-raster
    nor the point-cloud timings at all. So the win from fewer/larger requests may just be ordinary
    network round-trip latency, which would make it a plain win with no antivirus story attached.
    GDAL_HTTP_VERSION=2 does NOT multiplex requests in practice either way -- traced against the
    kyfromabove S3 bucket (the only host GDAL ever reads in this plugin; the titiler tile server is
    read through QGIS's own XYZ/WMS provider instead, never through GDAL), the connection negotiated
    plain HTTP/1.1 regardless of this option. Left set in case a future GDAL source does serve
    HTTP/2, but it is not what is making this faster.

    Confirmed to have no effect on the point cloud (COPC) provider, which doesn't use GDAL's curl
    layer -- that path isn't touched here, and its "open" step was already fast (~1.1-1.3s) with no
    tuning; whatever point-cloud slowness is seen in practice is more likely in sustained panning, or
    in QGIS's own rendering, than in this open step.
    """
    global _http_tuning_applied
    if _http_tuning_applied:
        return True
    gdal = _gdal()
    if gdal is None:
        return False
    gdal.SetConfigOption("GDAL_HTTP_VERSION", "2")  # multiplexes many requests over one connection
    gdal.SetConfigOption("GDAL_HTTP_MULTIPLEX", "YES")
    gdal.SetConfigOption("CPL_VSIL_CURL_CHUNK_SIZE", "1048576")  # 1 MB reads instead of the 16 KB default
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")  # skip probing the "directory" around each tile
    _http_tuning_applied = True
    return True


def setup_gdal() -> None:
    """Apply both fixes. Safe to call repeatedly (each part applies once) and from a worker thread
    (GDAL config options are process-global, not per-thread)."""
    ensure_ca_bundle()
    ensure_http_tuning()
