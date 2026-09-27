"""Make GDAL's libcurl trust the same certificate authorities QGIS (Qt) does.

GDAL opens remote COGs with its own libcurl and its own CA bundle (on OSGeo4W, curl-ca-bundle.crt).
On a machine that inspects HTTPS (antivirus "web shield", corporate proxy) the interceptor's root
certificate is in the Windows trust store, which Qt uses, but not in that bundle, so every remote
raster fails with "unable to get local issuer certificate". We write a bundle containing whatever
GDAL was already using plus the CAs Qt trusts, and point GDAL at it. Verification stays on.
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Optional

from qgis.core import QgsApplication
from qgis.PyQt.QtNetwork import QSslConfiguration

_applied: Optional[str] = None


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
    """Windows only. Returns the combined bundle's path once applied (also on repeat calls)."""
    global _applied
    if _applied is not None:
        return _applied
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
    _applied = path
    return path
