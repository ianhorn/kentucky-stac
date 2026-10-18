"""STAC transport built on QGIS's network stack.

Unlike stdlib urllib, this honours the user's QGIS proxy, CA certificate and authentication
settings, and it is safe to call from a QgsTask worker thread. (It also avoids stdlib HTTPS, which
aborts under OSGeo4W's Python with "no OPENSSL_Applink".)
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from . import auth
from .stac import StacError


def _is_ok(error_code) -> bool:
    # QgsBlockingNetworkRequest.NoError is 0; compare by value so this works whether the
    # binding exposes the enum scoped (Qt6/QGIS 4) or unscoped (QGIS 3).
    return int(getattr(error_code, "value", error_code)) == 0


_RATE_LIMIT_RETRIES = 3
_MAX_WAIT = 8.0  # seconds; the pause before a retry is never longer than this


def _retry_delay(reply, attempt: int) -> float:
    """How long to wait after a 429: the server's Retry-After (in seconds) if it gave one, else 1, 2, 4..."""
    try:
        wanted = float(bytes(reply.rawHeader(b"Retry-After")).decode("ascii", "ignore").strip())
    except ValueError:
        wanted = float(2**attempt)
    return max(0.5, min(wanted, _MAX_WAIT))


def transport_for(authcfg: str):
    """A STAC transport that sends the QGIS authentication configuration `authcfg` ("" for none)."""
    return lambda method, url, body, headers: qgis_transport(method, url, body, headers, authcfg)


def qgis_transport(method: str, url: str, body: Optional[bytes], headers: Dict[str, str], authcfg: str = "") -> bytes:
    request = QNetworkRequest(QUrl(url))
    for name, value in headers.items():
        request.setRawHeader(name.encode("ascii"), value.encode("utf-8"))

    method = method.upper()
    if method not in ("GET", "POST"):
        raise StacError(f"Unsupported HTTP method: {method}")
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        blocking = QgsBlockingNetworkRequest()
        if authcfg:
            blocking.setAuthCfg(authcfg)
        if method == "GET":
            error = blocking.get(request, True)
        else:
            error = blocking.post(request, QByteArray(body or b""), True)
        reply = blocking.reply()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if not _is_ok(error) and status == 429 and attempt < _RATE_LIMIT_RETRIES:
            time.sleep(_retry_delay(reply, attempt))  # "Too Many Requests": wait and ask again
            continue
        break
    content = bytes(reply.content())
    if not _is_ok(error):
        detail = content.decode("utf-8", "replace")[:500].strip()
        message = f"STAC API request failed: {blocking.errorMessage()}"
        if detail:
            message += f" - {detail}"
        if "Missing Authentication Token" in detail:
            # Amazon API Gateway's reply for an address that has no such route -- not a missing login.
            message += " (this server has no such endpoint, so the address is probably not a STAC API)"
        else:
            message += auth.http_error_hint(int(status) if status else None, authcfg)
        raise StacError(message, status=int(status) if status else None)
    return content
