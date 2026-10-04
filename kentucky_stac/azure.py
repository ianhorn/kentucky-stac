"""Azure Blob Storage URLs (Microsoft Planetary Computer serves its assets from them).

A plain https request to a PC blob answers 409 -- the file needs a SAS token, which the Planetary
Computer's token service hands out for free (see signing.py, which fetches and caches them). This module
is the pure part: recognising such URLs and attaching a token. No QGIS import, so it's unit-testable.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Optional, Tuple

_SUFFIX = ".blob.core.windows.net"


def parse(url: str) -> Optional[Tuple[str, str, str]]:
    """(account, container, blob path) of an https://<account>.blob.core.windows.net/<container>/<path>
    URL, or None for any other URL."""
    parsed = urllib.parse.urlparse(url or "")
    host = parsed.netloc.lower()
    if parsed.scheme not in ("http", "https") or not host.endswith(_SUFFIX):
        return None
    account = host[: -len(_SUFFIX)]
    container, _, path = parsed.path.lstrip("/").partition("/")
    if not account or not container:
        return None
    return account, container, path


def is_signed(url: str) -> bool:
    """Whether the URL already carries a SAS token (a signature), so it must be left alone."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return "sig" in query


def with_token(url: str, token: str) -> str:
    token = token.lstrip("?&")
    return f"{url}{'&' if '?' in url else '?'}{token}"


def expiry_epoch(iso: Optional[str]) -> Optional[float]:
    """Seconds since the epoch for the service's "msft:expiry" timestamp (e.g. 2026-10-02T22:30:51Z)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None
