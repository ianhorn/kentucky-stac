"""The text of a result's hover card: the STAC item's JSON, pretty-printed, with each http(s) URL
turned into a link, and a line of action links (download, copy URL, add to map) under each asset's href.
Pure Python -- no Qt -- so it's unit-testable (the card itself is json_card.py)."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .catalog import can_add_to_map
from .stac import Item

ACTION_SCHEME = "kyasset"  # action links look like kyasset:download/<asset key>

# URLs in the JSON stop at a quote, whitespace, a backslash or an angle bracket.
_URL = re.compile(r"https?://[^\s\"\\<>]+", re.IGNORECASE)

# How json.dumps(indent=2) lays an item out: top-level properties at 2 spaces, an asset's key at 4 and
# the fields inside it at 6.
_TOP_KEY = re.compile(r'^  "([^"]*)": ')
_ASSET_KEY = re.compile(r'^    ("(?:[^"\\]|\\.)*"): [{]$')
_ASSET_HREF = re.compile(r'^      "href": ')


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


def _linkify(text: str) -> str:
    parts = []
    pos = 0
    for m in _URL.finditer(text):
        parts.append(html.escape(text[pos : m.start()]))
        url = m.group(0)
        parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>')
        pos = m.end()
    parts.append(html.escape(text[pos:]))
    return "".join(parts)


def action_url(action: str, key: str) -> str:
    return f"{ACTION_SCHEME}:{action}/{urllib.parse.quote(key, safe='')}"


def parse_action_url(url: str) -> Optional[Tuple[str, str]]:
    """(action, asset key) of a link made by action_url, or None for any other link."""
    prefix = ACTION_SCHEME + ":"
    if not url.startswith(prefix):
        return None
    action, _, key = url[len(prefix):].partition("/")
    return (action, urllib.parse.unquote(key)) if action and key else None


def _asset_actions(key: str, item: Item) -> str:
    links = [("download", "download"), ("copy", "copy URL")]
    if can_add_to_map(item.assets[key]):
        links.append(("map", "add to map"))
    anchors = " | ".join(f'<a href="{html.escape(action_url(a, key), quote=True)}">{label}</a>' for a, label in links)
    return f"      [ {anchors} ]"


def json_to_html(text: str, item: Optional[Item] = None) -> str:
    """HTML for a rich-text widget: the text escaped, whitespace preserved, URLs as links. Given the
    `item` the text was made from, each asset also gets action links under its href."""
    if item is None:
        body = _linkify(text)
    else:
        out: List[str] = []
        in_assets, key = False, None
        for line in text.split("\n"):
            out.append(_linkify(line))
            top = _TOP_KEY.match(line)
            if top:
                in_assets, key = top.group(1) == "assets", None
                continue
            if not in_assets:
                continue
            found = _ASSET_KEY.match(line)
            if found:
                try:
                    key = json.loads(found.group(1))
                except ValueError:
                    key = None
            elif key in item.assets and _ASSET_HREF.match(line):
                out.append(_asset_actions(key, item))
        body = "\n".join(out)
    return (
        '<pre style="white-space: pre-wrap; margin: 0; font-family: Consolas, \'Courier New\', monospace; '
        f'font-size: 9pt;">{body}</pre>'
    )
