"""Logins for sources that need them, through QGIS's own Authentication database.

A source stores only the *id* of a QGIS authentication configuration (Settings > Options > Authentication:
Basic, API header, Bearer/OAuth2, AWS S3, ...); the secrets stay in QGIS's encrypted store. This module applies
that configuration

* to the plugin's own web requests (collections, search, thumbnails, file sizes and downloads), through
  QGIS's network layer, and
* to reading a source's files with GDAL, by giving GDAL the matching request headers or AWS keys for just
  that file's host or bucket (GDAL's path-specific options), so a login is never sent anywhere else.

Everything that touches the authentication manager runs on the main thread; the worker threads only read the
source-to-config table kept here.
"""

from __future__ import annotations

import urllib.parse
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsApplication, QgsAuthMethodConfig
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .sources import ApiSource, normalize_url


_by_source: Dict[str, str] = {}  # normalised API base URL -> authentication config id


def set_sources(sources: Iterable[ApiSource]) -> None:
    """Remember each source's config id (call whenever the source list changes)."""
    _by_source.clear()
    for source in sources:
        if source.auth_cfg:
            _by_source[normalize_url(source.base_uri).lower()] = source.auth_cfg


def cfg_for_source(base_uri: str) -> str:
    """The config id for the API at `base_uri`, or "" (safe to call from any thread)."""
    return _by_source.get(normalize_url(base_uri or "").lower(), "")


def _manager():
    return QgsApplication.authManager()


def apply_to_request(request: QNetworkRequest, cfg: str) -> bool:
    """Add the configuration's login to a request. True if there is nothing to add or it was added."""
    if not cfg:
        return True
    return bool(_manager().updateNetworkRequest(request, cfg))


def config_summary(cfg: str) -> str:
    """The configuration's name and method, for showing in the Sources dialog ("" if it can't be found)."""
    config = QgsAuthMethodConfig()
    if not cfg or not _manager().loadAuthenticationConfig(cfg, config, False):
        return ""
    return f"{config.name()} ({config.method()})"


def _headers_for(url: str, cfg: str) -> List[Tuple[str, str]]:
    """The HTTP headers the configuration adds for `url`: an Authorization header, an API key header..."""
    request = QNetworkRequest(QUrl(url))
    if not _manager().updateNetworkRequest(request, cfg):
        return []
    return [(bytes(name).decode("latin-1"), bytes(request.rawHeader(name)).decode("latin-1")) for name in request.rawHeaderList()]


def _aws_keys(cfg: str) -> Optional[Tuple[str, str, str]]:
    """(access key id, secret, region) from an AWS S3 or Basic configuration, or None."""
    config = QgsAuthMethodConfig()
    if not _manager().loadAuthenticationConfig(cfg, config, True):
        return None
    key, secret = config.config("username"), config.config("password")
    if not key or not secret:
        return None
    return key, secret, config.config("region") or ""


def prepare_asset(href: str, cfg: str) -> None:
    """Let GDAL read `href` with the configuration's login. Main thread only; a no-op without a config.

    The headers or keys are registered for that file's host (or bucket) alone, so another site never sees
    them. Call it before the file is opened.
    """
    if not cfg or not href:
        return
    from osgeo import gdal

    parsed = urllib.parse.urlparse(href)
    if parsed.scheme.lower() == "s3":
        keys = _aws_keys(cfg)
        if keys is None:
            return
        prefix = f"/vsis3/{parsed.netloc}/"
        gdal.SetPathSpecificOption(prefix, "AWS_ACCESS_KEY_ID", keys[0])
        gdal.SetPathSpecificOption(prefix, "AWS_SECRET_ACCESS_KEY", keys[1])
        gdal.SetPathSpecificOption(prefix, "AWS_REQUEST_PAYER", "requester")
        gdal.SetPathSpecificOption(prefix, "AWS_REGION", keys[2] or None)  # None clears a region left by another login
        return
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        headers = _headers_for(href, cfg)
        if headers:
            prefix = f"/vsicurl/{parsed.scheme}://{parsed.netloc}/"
            gdal.SetPathSpecificOption(prefix, "GDAL_HTTP_HEADERS", "\n".join(f"{n}: {v}" for n, v in headers))


def http_error_hint(status: Optional[int], cfg: str) -> str:
    """Words to add to a 401/403 error, saying whether a login was in use."""
    if status not in (401, 403):
        return ""
    if cfg:
        return " (a login was sent; check that it is the right one for this source and still valid)"
    return " (this source may need a login: choose an authentication configuration for it under Sources...)"


__all__ = ["set_sources", "cfg_for_source", "apply_to_request", "prepare_asset", "config_summary", "http_error_hint"]
