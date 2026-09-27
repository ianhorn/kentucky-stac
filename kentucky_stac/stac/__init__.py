"""Minimal STAC API client and models. Pure Python (stdlib only), no QGIS imports."""

from .client import (
    DEFAULT_BASE_URI,
    SearchQuery,
    StacClient,
    StacError,
    urllib_transport,
)
from .models import Asset, Collection, Item, ItemCollection, Link

__all__ = [
    "DEFAULT_BASE_URI",
    "Asset",
    "Collection",
    "Item",
    "ItemCollection",
    "Link",
    "SearchQuery",
    "StacClient",
    "StacError",
    "urllib_transport",
]
