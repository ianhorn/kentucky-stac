"""Export the selected tiles as a script that downloads them outside QGIS: a Python script, a Jupyter
notebook or a shell script. Each is self-contained (Python: standard library only; shell: bash + curl),
turns s3:// addresses into https, and fetches a free Planetary Computer SAS token for an Azure blob file
when it runs, since such a token expires within the hour and so can't be baked into the script.

Pure Python -- no QGIS import -- so it's unit-testable.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, List, Tuple

from . import s3

Entry = Tuple[str, str]  # (file name, url)

KINDS = {  # kind -> (menu label, file extension, file dialog filter)
    "notebook": ("Jupyter notebook (.ipynb)", ".ipynb", "Jupyter notebook (*.ipynb)"),
    "python": ("Python script (.py)", ".py", "Python script (*.py)"),
    "shell": ("Shell script (.sh)", ".sh", "Shell script (*.sh)"),
}

_PY_CONFIG = '''import concurrent.futures
import json
import os
import shutil
import urllib.parse
import urllib.request

# Where the files go, and how many download at once.
DEST = "tiles"
WORKERS = 4
'''

_PY_TILES = '''# (file name, url) of each tile.
TILES = @ENTRIES@
'''

_PY_HELPERS = '''_tokens = {}


def sign(url):
    """An Azure blob URL (Microsoft Planetary Computer) needs a free SAS token; anything else is as-is."""
    parts = urllib.parse.urlparse(url)
    host = parts.netloc.lower()
    if not host.endswith(".blob.core.windows.net") or "sig=" in parts.query:
        return url
    account = host.split(".")[0]
    container = parts.path.lstrip("/").split("/")[0]
    if (account, container) not in _tokens:
        api = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
        with urllib.request.urlopen(api) as response:
            _tokens[(account, container)] = json.load(response)["token"]
    return url + ("&" if parts.query else "?") + _tokens[(account, container)]


def fetch(tile):
    name, url = tile
    path = os.path.join(DEST, name)
    if os.path.exists(path):
        return name, "already there"
    part = path + ".part"
    request = urllib.request.Request(sign(url), headers={"User-Agent": "kentucky-stac-export"})
    with urllib.request.urlopen(request) as response, open(part, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    os.replace(part, path)
    return name, "downloaded"
'''

_PY_RUN = '''def download_all():
    os.makedirs(DEST, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(fetch, tile): tile[0] for tile in TILES}
        for future in concurrent.futures.as_completed(futures):
            try:
                print(*future.result())
            except Exception as error:
                print(futures[future], "FAILED:", error)
'''


def _entries_literal(entries: List[Entry]) -> str:
    rows = "".join(f"    ({json.dumps(name)}, {json.dumps(url)}),\n" for name, url in entries)
    return f"[\n{rows}]"


def entries_for(pairs: Iterable[Entry]) -> List[Entry]:
    """(name, url) pairs with s3:// urls turned into their https address, duplicate names dropped."""
    seen, out = set(), []
    for name, url in pairs:
        if name not in seen:
            seen.add(name)
            out.append((name, s3.to_https(url)))
    return out


def _header(entries: List[Entry]) -> str:
    return f"Downloads {len(entries)} tile{'s' if len(entries) != 1 else ''} chosen in the Kentucky STAC QGIS plugin ({datetime.now():%Y-%m-%d})."


def python_script(entries: List[Entry]) -> str:
    return (
        f'"""{_header(entries)}\n\nStandard library only. A file that is not public (e.g. a private S3 bucket) needs its own '
        'credentials,\nwhich this script does not handle."""\n\n'
        + _PY_CONFIG
        + "\n"
        + _PY_TILES.replace("@ENTRIES@", _entries_literal(entries))
        + "\n\n"
        + _PY_HELPERS
        + "\n\n"
        + _PY_RUN
        + '\n\nif __name__ == "__main__":\n    download_all()\n'
    )


def notebook(entries: List[Entry]) -> str:
    def cell(kind: str, text: str) -> dict:
        lines = text.rstrip("\n").splitlines(keepends=True)
        base = {"cell_type": kind, "metadata": {}, "source": lines}
        return base if kind == "markdown" else {**base, "execution_count": None, "outputs": []}

    cells = [
        cell("markdown", f"# Kentucky STAC tiles\n\n{_header(entries)} Run the cells in order; standard library only."),
        cell("code", _PY_CONFIG),
        cell("code", _PY_TILES.replace("@ENTRIES@", _entries_literal(entries))),
        cell("code", _PY_HELPERS),
        cell("code", _PY_RUN + "\n\ndownload_all()"),
    ]
    nb = {
        "cells": cells,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    for i, c in enumerate(cells):
        c["id"] = f"cell{i}"
    return json.dumps(nb, indent=1) + "\n"


_SH = r'''#!/usr/bin/env bash
# @HEADER@
# Needs bash and curl. Usage: ./script.sh [destination folder]   (JOBS=8 ./script.sh for more at once)
# A file that is not public (e.g. a private S3 bucket) needs its own credentials, which this does not handle.
set -u
export DEST="${1:-tiles}"
JOBS="${JOBS:-4}"
mkdir -p "$DEST"

# An Azure blob URL (Microsoft Planetary Computer) needs a free SAS token; anything else is as-is.
sign() {
  local url="$1"
  case "$url" in
    *.blob.core.windows.net/*)
      case "$url" in *sig=*) echo "$url"; return;; esac
      local rest="${url#https://}" account container token sep="?"
      account="${rest%%.*}"
      container="${rest#*/}"; container="${container%%/*}"
      token=$(curl -fsS "https://planetarycomputer.microsoft.com/api/sas/v1/token/$account/$container" \
        | sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' | sed 's/\\u0026/\&/g')
      case "$url" in *\?*) sep="&";; esac
      echo "${url}${sep}${token}";;
    *) echo "$url";;
  esac
}

# One "file name|url" line per tile.
fetch() {
  local name="${1%%|*}" url="${1#*|}"
  local out="$DEST/$name"
  if [ -e "$out" ]; then echo "already there: $name"; return; fi
  if curl -fL --retry 3 -C - -o "$out.part" "$(sign "$url")"; then
    mv "$out.part" "$out" && echo "downloaded: $name"
  else
    echo "FAILED: $name"
  fi
}
export -f sign fetch

grep -v '^$' <<'TILES' | tr '\n' '\0' | xargs -0 -P "$JOBS" -I{} bash -c 'fetch "$1"' _ {}
@LINES@
TILES
'''


def shell_script(entries: List[Entry]) -> str:
    lines = "\n".join(f"{name}|{url}" for name, url in entries)
    return _SH.replace("@HEADER@", _header(entries)).replace("@LINES@", lines)


def render(kind: str, entries: List[Entry]) -> str:
    return {"notebook": notebook, "python": python_script, "shell": shell_script}[kind](entries)
