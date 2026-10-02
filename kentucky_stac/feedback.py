"""Backs the Feedback dialog's two paths, ported from the ArcGIS Pro add-ins (kylidar-addin /
kyfromabove-ext FeedbackService):

  - github_issue_url: a pre-filled "new issue" link opened in the browser. Needs a GitHub account
    (GitHub has no anonymous issue submission).
  - direct_payload: the JSON posted to a Formspree form, so feedback reaches the developer with no
    account and no recipient address living in this plugin's source -- it's configured privately in
    the Formspree dashboard. Sent with the docs site as Referer, since Formspree's spam heuristics
    are tuned around browser submissions, which always carry one.

The environment summary (plugin, QGIS and OS versions) is attached to both, so bug reports arrive
with the three things normally asked for first. Pure Python -- no QGIS import -- so it's testable;
the caller supplies the versions and does the network call.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, Optional

FORMSPREE_ENDPOINT = "https://formspree.io/f/mqpavodn"
DOCS_SITE_URL = "https://ianhorn.github.io/kentucky-stac/"
REPO_URL = "https://github.com/ianhorn/kentucky-stac"
SUBJECT = "Kentucky STAC (QGIS plugin) feedback"


def environment_summary(plugin_version: str, qgis_version: Optional[str], os_description: Optional[str]) -> str:
    """"Kentucky STAC 0.1.0 | QGIS 3.44.4-Solothurn | Windows-11-10.0.26200" -- a part that couldn't be
    determined is left out rather than failing the whole summary."""
    parts = [f"Kentucky STAC {plugin_version or 'unknown'}"]
    if qgis_version:
        parts.append(f"QGIS {qgis_version}")
    if os_description:
        parts.append(os_description.strip())
    return " | ".join(parts)


def github_issue_url(environment: str) -> str:
    body = (
        "**What happened?**\n\n\n**What did you expect?**\n\n\n**Steps to reproduce**\n1. \n\n"
        f"---\nEnvironment: {environment}"
    )
    return f"{REPO_URL}/issues/new?title=&body={urllib.parse.quote(body, safe='')}"


def direct_payload(message: str, reply_to: str, environment: str) -> Dict[str, Any]:
    reply_to = (reply_to or "").strip()
    return {
        "message": f"{message.rstrip()}\n\n---\nEnvironment: {environment}",
        "_subject": SUBJECT,
        "_replyto": reply_to or None,
        "_gotcha": "",  # honeypot: a real browser form leaves this hidden field empty; bots fill it
    }


def summarize_error(body: str) -> str:
    """Just the first "message" from Formspree's JSON error body, if there is one, rather than the
    whole response."""
    try:
        data = json.loads(body)
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors and isinstance(errors[0], dict) and errors[0].get("message"):
            return str(errors[0]["message"])
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except (ValueError, TypeError, IndexError):
        pass
    return body.strip()[:300]
