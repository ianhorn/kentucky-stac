"""Minimal STAC API client and models. Pure Python (stdlib only), no QGIS imports."""

from .client import (
    DEFAULT_BASE_URI,
    SearchQuery,
    StacClient,
    StacError,
    query_variants,
    urllib_transport,
    within_cloud,
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
    "query_variants",
    "within_cloud",
    "urllib_transport",
]
