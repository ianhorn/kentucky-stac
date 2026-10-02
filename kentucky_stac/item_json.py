"""The text of a result's hover card: the STAC item's JSON, pretty-printed, with each http(s) URL
turned into a link. Pure Python -- no Qt -- so it's unit-testable (the card itself is json_card.py)."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Dict

from .stac import Item

# URLs in the JSON stop at a quote, whitespace, a backslash or an angle bracket.
_URL = re.compile(r"https?://[^\s\"\\<>]+", re.IGNORECASE)


def item_json_text(item: Item) -> str:
    """The item as the catalog sent it (pretty-printed). Falls back to the fields the plugin kept when
    the raw document isn't available (an Item built by hand)."""
    doc: Dict[str, Any] = item.raw or {
        "id": item.id,
        "collection": item.collection,
        "bbox": item.bbox,
        "geometry": item.geometry,
        "properties": item.properties,
        "assets": {k: {"href": a.href, "type": a.type, "title": a.title, "roles": a.roles} for k, a in item.assets.items()},
    }
    try:
        return json.dumps(doc, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return f"(could not render item JSON: {e})"


def json_to_html(text: str) -> str:
    """HTML for a rich-text widget: the text escaped, whitespace preserved, URLs as links."""
    parts = []
    pos = 0
    for m in _URL.finditer(text):
        parts.append(html.escape(text[pos : m.start()]))
        url = m.group(0)
        parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>')
        pos = m.end()
    parts.append(html.escape(text[pos:]))
    return (
        '<pre style="white-space: pre-wrap; margin: 0; font-family: Consolas, \'Courier New\', monospace; '
        f'font-size: 9pt;">{"".join(parts)}</pre>'
    )
