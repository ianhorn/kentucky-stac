"""The text of a result's hover card: the STAC item's JSON, pretty-printed, with each http(s) URL
turned into a link, and a line of action links (download, copy URL, add to map) under each asset's href.
Pure Python -- no Qt -- so it's unit-testable (the card itself is json_card.py)."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .catalog import can_add_to_map, format_size
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


# ---- the friendly (HTML) view -------------------------------------------------------------------------

# Properties worth showing first; everything else follows in the catalog's own order.
_PRIORITY = (
    "datetime", "start_datetime", "end_datetime", "eo:cloud_cover", "platform", "constellation", "instruments",
    "gsd", "proj:epsg", "proj:code", "title", "description",
)
_TD = 'style="padding: 2px 8px 2px 0; vertical-align: top;"'
_RULE = "border-bottom: 1px solid #888;"


def _value_text(value: Any) -> str:
    """A property value in a short readable form."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (list, tuple)) and all(isinstance(v, (str, int, float)) for v in value):
        return ", ".join(str(v) for v in value) or "-"
    try:
        compact = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        compact = str(value)
    return compact if len(compact) <= 160 else compact[:157] + "..."


def _actions_html(key: str, item: Item) -> str:
    links = [("download", "download"), ("copy", "copy URL")]
    if can_add_to_map(item.assets[key]):
        links.append(("map", "add to map"))
    return " | ".join(f'<a href="{html.escape(action_url(a, key), quote=True)}">{label}</a>' for a, label in links)


def item_to_friendly_html(item: Item) -> str:
    """The tile as a readable page: a heading, its properties as a table, its assets with their actions and its links."""
    raw: Dict[str, Any] = item.raw or {}
    props: Dict[str, Any] = dict(raw.get("properties") or item.properties or {})
    out: List[str] = []
    title = props.get("title") or item.id
    out.append(f'<h3 style="margin: 2px 0;">{html.escape(str(title))}</h3>')
    sub = " &middot; ".join(
        html.escape(str(x)) for x in (item.collection, str(props.get("datetime") or "")[:10], item.id if title != item.id else "") if x
    )
    if sub:
        out.append(f'<p style="margin: 0 0 6px 0;">{sub}</p>')

    names = [k for k in _PRIORITY if k in props] + [k for k in props if k not in _PRIORITY]
    if names:
        out.append('<h4 style="margin: 8px 0 2px 0;">Properties</h4><table width="100%" cellspacing="0" cellpadding="0">')
        for name in names:
            out.append(
                f'<tr style="{_RULE}"><td {_TD}><b>{html.escape(str(name))}</b></td>'
                f"<td {_TD}>{_linkify(_value_text(props[name]))}</td></tr>"
            )
        out.append("</table>")

    if item.assets:
        out.append('<h4 style="margin: 10px 0 2px 0;">Assets</h4><table width="100%" cellspacing="0" cellpadding="0">')
        for key, asset in item.assets.items():
            size = format_size(asset.file_size) if asset.file_size else ""
            detail = " &middot; ".join(html.escape(x) for x in (asset.type or "", ", ".join(asset.roles), size) if x)
            line = f"<b>{html.escape(asset.title or key)}</b> <small>({html.escape(key)})</small>"
            out.append(
                f'<tr style="{_RULE}"><td {_TD}>{line}'
                + (f"<br><small>{detail}</small>" if detail else "")
                + (f"<br><small>{_linkify(asset.href)}</small>" if asset.href.lower().startswith("http") else "")
                + (f"<br>{_actions_html(key, item)}" if asset.href else "")
                + "</td></tr>"
            )
        out.append("</table>")

    links = raw.get("links") or []
    shown = [link for link in links if isinstance(link, dict) and str(link.get("href", "")).lower().startswith("http")]
    if shown:
        out.append('<h4 style="margin: 10px 0 2px 0;">Links</h4><table width="100%" cellspacing="0" cellpadding="0">')
        for link in shown:
            out.append(
                f'<tr style="{_RULE}"><td {_TD}><b>{html.escape(str(link.get("rel", "")))}</b></td>'
                f"<td {_TD}>{_linkify(str(link['href']))}</td></tr>"
            )
        out.append("</table>")
    return '<div style="font-size: 9pt;">' + "".join(out) + "</div>"
