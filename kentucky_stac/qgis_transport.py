"""STAC transport built on QGIS's network stack.

Unlike stdlib urllib, this honours the user's QGIS proxy, CA certificate and authentication
settings, and it is safe to call from a QgsTask worker thread. (It also avoids stdlib HTTPS, which
aborts under OSGeo4W's Python with "no OPENSSL_Applink".)
"""

from __future__ import annotations

from typing import Dict, Optional

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .stac import StacError


def _is_ok(error_code) -> bool:
    # QgsBlockingNetworkRequest.NoError is 0; compare by value so this works whether the
    # binding exposes the enum scoped (Qt6/QGIS 4) or unscoped (QGIS 3).
    return int(getattr(error_code, "value", error_code)) == 0


def qgis_transport(method: str, url: str, body: Optional[bytes], headers: Dict[str, str]) -> bytes:
    request = QNetworkRequest(QUrl(url))
    for name, value in headers.items():
        request.setRawHeader(name.encode("ascii"), value.encode("utf-8"))

    blocking = QgsBlockingNetworkRequest()
    method = method.upper()
    if method == "GET":
        error = blocking.get(request, True)
    elif method == "POST":
        error = blocking.post(request, QByteArray(body or b""), True)
    else:
        raise StacError(f"Unsupported HTTP method: {method}")

    reply = blocking.reply()
    content = bytes(reply.content())
    if not _is_ok(error):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        detail = content.decode("utf-8", "replace")[:500].strip()
        message = f"STAC API request failed: {blocking.errorMessage()}"
        if detail:
            message += f" - {detail}"
        raise StacError(message, status=int(status) if status else None)
    return content
