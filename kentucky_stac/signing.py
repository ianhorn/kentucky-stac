"""Turn an asset URL into one QGIS and GDAL can actually fetch: s3:// becomes its https address (see
s3.py) and an Azure blob URL gets a free Planetary Computer SAS token attached (see azure.py).

Tokens are cached per container until shortly before they expire, and a container the token service
doesn't know is remembered for a while rather than asked about again (an Azure account that isn't the
Planetary Computer's simply keeps its plain URL). The lookups are blocking requests, so a first use can
pause for a moment -- on the main thread too -- but later ones are instant."""

from __future__ import annotations

import json
import threading
import time
from typing import Dict, Optional, Tuple

from . import azure, s3
from .qgis_transport import qgis_transport

TOKEN_API = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
_REFRESH_MARGIN = 300  # ask for a new token when fewer than this many seconds remain
_DEFAULT_LIFETIME = 1800
_NEGATIVE_TTL = 600

_lock = threading.Lock()
_cache: Dict[Tuple[str, str], Tuple[Optional[str], float]] = {}  # (account, container) -> (token or None, valid until)


def _fetch_token(account: str, container: str) -> Optional[str]:
    key = (account, container)
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1] > time.time() + _REFRESH_MARGIN * (cached[0] is not None):
            return cached[0]
    try:
        raw = qgis_transport(
            "GET", TOKEN_API.format(account=account, container=container), None, {"Accept": "application/json"}
        )
        data = json.loads(raw)
        token = data["token"]
        expires = azure.expiry_epoch(data.get("msft:expiry")) or time.time() + _DEFAULT_LIFETIME
    except Exception:
        token, expires = None, time.time() + _NEGATIVE_TTL
    with _lock:
        _cache[key] = (token, expires)
    return token


def fetchable_url(url: str) -> str:
    """`url` made fetchable: s3:// -> https, Azure blob -> signed. Anything else comes back unchanged."""
    if s3.is_s3(url):
        return s3.to_https(url)
    parts = azure.parse(url)
    if parts is not None and not azure.is_signed(url):
        token = _fetch_token(parts[0], parts[1])
        if token:
            return azure.with_token(url, token)
    return url


def clear_cache() -> None:
    with _lock:
        _cache.clear()
